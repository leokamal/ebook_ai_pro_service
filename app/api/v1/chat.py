import json
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.core.dependencies import CurrentUserDep, get_orchestrator
from starlette.background import BackgroundTask

from app.core.ratelimit import chat_limiter, stream_limiter
from app.schemas.conversations import ChatRequest, ChatResponse
from app.services.orchestrator import RAGOrchestrator, _source_dict

router = APIRouter(tags=["chat"])
OrchDep = Annotated[RAGOrchestrator, Depends(get_orchestrator)]
_COMMON = {404: {"description": "Conversation/document not found (or not yours)"},
           409: {"description": "Document not indexed yet (`document_not_indexed`)"},
           503: {"description": "AI/index unavailable"}}

_SSE_EXAMPLE = (
    'event: meta\ndata: {"conversation_id": "..."}\n\n'
    'event: token\ndata: {"text": "Cats sleep "}\n\n'
    'event: token\ndata: {"text": "a lot [S1]."}\n\n'
    'event: sources\ndata: {"sources": [{"document_id": "...", "document_title": "Pets", "page_number": 4, '
    '"chunk_id": "...", "relevance_score": 0.81, "snippet": "..."}]}\n\n'
    'event: done\ndata: {"message_id": "...", "answer": "Cats sleep a lot [S1].", "refused": false, '
    '"usage": {"output_tokens": 7}}\n\n')


@router.post("/chat", response_model=ChatResponse, summary="Ask a question in a conversation", responses=_COMMON)
def chat(body: ChatRequest, user: CurrentUserDep, rag: OrchDep):
    """Personal RAG (`single_document`) or Multi-RAG (`all_documents`) depending on the conversation.
    Plain `def`: retrieval and the LLM call are blocking, so FastAPI runs it in the threadpool."""
    chat_limiter().check(user.id)  # per-user throttle (CHAT_RATE_PER_MINUTE, 0 disables)
    r = rag.answer(str(body.conversation_id), body.message, body.summarize)
    return {"message_id": r.message_id, "answer": r.answer, "usage": r.usage,
            "sources": [_source_dict(c) for c in r.sources]}


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/chat/stream", summary="Ask a question and stream the answer (Server-Sent Events)",
             responses={**_COMMON, 200: {"description": "`text/event-stream`: meta, token*, sources, done | error",
                                         "content": {"text/event-stream": {"schema": {"type": "string"},
                                                                          "example": _SSE_EXAMPLE}}}})
def chat_stream(body: ChatRequest, user: CurrentUserDep, rag: OrchDep):
    """Same request and rules as `/chat`. Validation, ownership, readiness and retrieval happen BEFORE the stream
    starts, so those failures are normal HTTP errors (404/409/429/503). Once streaming, a failure is sent as an
    `error` event and nothing is stored. `done.answer` is authoritative: show it instead of the concatenated
    tokens if they differ (refusals, stripped citation markers)."""
    chat_limiter().check(user.id)
    limiter = stream_limiter()
    token = limiter.acquire(user.id)  # at most CHAT_MAX_CONCURRENT_STREAMS in flight per user (self-healing)
    try:
        prepared = rag.prepare(str(body.conversation_id), body.message, body.summarize)
    except BaseException:
        limiter.release(user.id, token)
        raise

    def events():
        try:
            for name, data in rag.stream(prepared):
                yield _sse(name, data)
        finally:  # runs on completion, error and client disconnect (generator close)
            limiter.release(user.id, token)
    # the background task is a second release path for streams that never start; release is idempotent
    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                             background=BackgroundTask(limiter.release, user.id, token))
