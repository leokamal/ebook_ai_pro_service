"""RAGOrchestrator: the single path behind /chat and /chat/stream.

Scope comes ONLY from the conversation (mode + document_id), never from the request. Ownership and
readiness are established through Postgres (user-scoped client, RLS) before anything is retrieved.

Flow: `prepare()` (all checks + retrieval + context; raises proper HTTP errors) -> either `answer()` (one
shot) or `stream()` (SSE events). A turn is persisted only after a complete, valid answer.
Blocking code: call from `def` routes / threads.
"""
import logging
import time
from dataclasses import dataclass, field
from typing import Iterator

from app.core.exceptions import AppError
from app.prompts.rag import (CONDENSE_SYSTEM, NO_ANSWER_SENTINEL, SUMMARY_RETRIEVAL_QUERY, SYSTEM_PROMPT, user_turn)
from app.services.llm import LLMProvider, LLMUnavailable
from app.services.rag_parts import (CitationService, ContextBuilder, ConversationMemoryService, RerankingService,
                                    approx_tokens, not_found_message)
from app.services.retrieval import RetrievalService, resolve_searchable_documents

logger = logging.getLogger("uvicorn.error")
DEFAULT_TITLE = "New conversation"
MAX_SUMMARY_CHUNKS = 12


@dataclass
class RAGResult:
    message_id: str
    answer: str
    sources: list = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    refused: bool = False


@dataclass
class Prepared:
    """Everything validated and retrieved; nothing generated or persisted yet."""
    conv: dict
    message: str
    summarize: bool
    history: list
    used: list = field(default_factory=list)       # chunks really placed in the prompt ([S1] = used[0])
    sources_block: str = ""
    refuse: bool = False                           # nothing to ground an answer on: no LLM call


class RAGOrchestrator:
    def __init__(self, user_id: str, conversations, documents, retrieval: RetrievalService,
                 memory: ConversationMemoryService, reranker: RerankingService, context: ContextBuilder,
                 citations: CitationService, llm: LLMProvider, writer, settings):
        self.user_id, self.conversations, self.documents = user_id, conversations, documents
        self.retrieval, self.memory, self.reranker = retrieval, memory, reranker
        self.context, self.citations, self.llm, self.writer, self.cfg = context, citations, llm, writer, settings

    # ------------------------------------------------------------------ prepare
    def _condense(self, history: list, message: str) -> str:
        """Standalone retrieval query for follow-ups; any failure falls back to the raw message."""
        if not history or not self.cfg.rag_condense:
            return message
        try:
            res = self.llm.generate(CONDENSE_SYSTEM, history + [{"role": "user", "content": message}])
            return res.text.strip()[:500] or message
        except Exception:
            logger.warning("condense step failed; using the raw message")
            return message

    def prepare(self, conversation_id: str, message: str, summarize: bool = False) -> Prepared:
        conv = self.conversations.get(conversation_id)  # 404 for missing/foreign (RLS)
        docs = resolve_searchable_documents(self.documents, conv["document_id"])  # 404 / 409 / [] (multi)
        history = self.memory.select(self.conversations.recent_messages(conv["id"], self.cfg.history_messages))
        p = Prepared(conv, message, summarize, history)
        if not docs:
            p.refuse = True
            return p
        query = SUMMARY_RETRIEVAL_QUERY if summarize else self._condense(history, message)
        hits = self.retrieval.search(self.user_id, docs, query, self.cfg.rag_candidates)
        top_n = min(self.cfg.rag_context_chunks * (2 if summarize else 1), MAX_SUMMARY_CHUNKS)
        multi = conv["mode"] == "all_documents"
        ranked = self.reranker.rerank(hits, top_n, per_doc_cap=multi)  # per-document cap only for multi scope
        p.sources_block, p.used = self.context.build(ranked, group=multi)
        p.refuse = not p.used
        return p

    def _llm_messages(self, p: Prepared) -> list:
        return p.history + [{"role": "user", "content": user_turn(p.message, p.sources_block, p.summarize)}]

    # ------------------------------------------------------------------ persist
    def _finish(self, p: Prepared, answer: str, cites: list, usage: dict, refused: bool) -> RAGResult:
        """Persist the turn only now: a failed generation leaves no orphan user message, so history never
        has consecutive user turns and a retry does not duplicate the question."""
        conv_id = p.conv["id"]
        self.writer.add_message(conv_id, self.user_id, "user", p.message)
        msg = self.writer.add_message(conv_id, self.user_id, "assistant", answer, int(usage.get("output_tokens", 0)))
        rows = [{"document_id": c.document_id, "chunk_id": c.chunk_id, "page_number": c.page_number,
                 "relevance_score": c.relevance_score, "document_title": c.document_title} for c in cites]
        try:
            self.writer.add_sources(msg["id"], conv_id, self.user_id, rows)
        except Exception:  # never keep an assistant answer whose citations were lost
            self.writer.delete_message(msg["id"], conv_id, self.user_id)
            raise
        if p.conv["title"] == DEFAULT_TITLE:
            try:  # via the service-role writer (ownership-filtered): the user-scoped client may be closed by now
                self.writer.set_default_title(conv_id, self.user_id, p.message.strip()[:60] or DEFAULT_TITLE,
                                              DEFAULT_TITLE)
            except Exception:
                logger.warning("could not auto-title conversation")
        return RAGResult(msg["id"], answer, cites, usage, refused)

    def _complete(self, p: Prepared, raw: str, usage: dict) -> RAGResult:
        """Model text -> validated, cited, persisted answer."""
        text = (raw or "").strip()
        if not text:
            raise LLMUnavailable()
        logger.info("rag usage conversation=%s mode=%s %s", p.conv["id"], p.conv["mode"], usage)
        if text.upper().startswith(NO_ANSWER_SENTINEL):  # the model says the sources do not contain the answer
            reply = text[len(NO_ANSWER_SENTINEL):].lstrip(" :\n").strip() or not_found_message(p.message)
            return self._finish(p, reply, [], usage, True)
        text, cites = self.citations.process(text, p.used)
        if not cites:  # no valid [Sn] marker => not grounded in the provided sources: do not present it as an answer
            logger.warning("answer without valid citations replaced by a refusal (conversation=%s)", p.conv["id"])
            return self._finish(p, not_found_message(p.message), [], usage, True)
        return self._finish(p, text, cites, usage, False)

    # ------------------------------------------------------------------ one shot
    def answer(self, conversation_id: str, message: str, summarize: bool = False) -> RAGResult:
        p = self.prepare(conversation_id, message, summarize)
        if p.refuse:
            return self._finish(p, not_found_message(message), [], {}, True)
        result = self.llm.generate(SYSTEM_PROMPT, self._llm_messages(p))
        return self._complete(p, result.text, result.usage)

    # ------------------------------------------------------------------ streaming
    def stream(self, p: Prepared) -> Iterator[tuple]:
        """Yields (event, data): meta, token*, sources, done | error.

        Tokens are the model's raw text (with [Sn] markers). The authoritative result is `done.answer`: the
        client must show it instead of the streamed text when they differ (refusal, stripped markers).
        Nothing is persisted unless the stream completes; a client disconnect closes this generator."""
        yield "meta", {"conversation_id": p.conv["id"]}
        try:
            if p.refuse:
                r = self._finish(p, not_found_message(p.message), [], {}, True)
                yield "token", {"text": r.answer}
            else:
                r = yield from self._stream_generation(p)
            yield "sources", {"sources": [_source_dict(c) for c in r.sources]}
            yield "done", {"message_id": r.message_id, "answer": r.answer, "refused": r.refused, "usage": r.usage}
        except AppError as e:
            logger.info("stream failed: %s", e.code)
            yield "error", {"code": e.code, "message": e.message}
        except Exception:
            logger.exception("stream failed")
            yield "error", {"code": "internal_error", "message": "Internal server error"}

    def _stream_generation(self, p: Prepared) -> Iterator[tuple]:
        parts, usage = [], {}
        buf, decided, sentinel = "", False, False  # hold back the first chars: a refusal must not leak the sentinel
        deadline = time.monotonic() + getattr(self.cfg, "llm_stream_deadline", 120.0)
        for chunk in self.llm.stream(SYSTEM_PROMPT, self._llm_messages(p)):
            if time.monotonic() > deadline:  # a slow upstream must not hold a worker thread forever
                logger.warning("stream deadline exceeded (conversation=%s)", p.conv["id"])
                raise LLMUnavailable()
            if isinstance(chunk, dict):  # provider reports usage as a final dict
                usage = chunk
                continue
            parts.append(chunk)
            if decided:
                if not sentinel:
                    yield "token", {"text": chunk}
                continue
            buf += chunk
            probe = buf.lstrip().upper()
            if len(probe) >= len(NO_ANSWER_SENTINEL) or not NO_ANSWER_SENTINEL.startswith(probe):
                decided, sentinel = True, probe.startswith(NO_ANSWER_SENTINEL)
                if not sentinel:
                    yield "token", {"text": buf}
        if not decided and buf:  # very short answer that is only a prefix of the sentinel
            yield "token", {"text": buf}
        text = "".join(parts)
        r = self._complete(p, text, usage or {"output_tokens": approx_tokens(text)})
        if sentinel:  # nothing was streamed: send the (localized) refusal as the only token
            yield "token", {"text": r.answer}
        return r


def _source_dict(c) -> dict:
    return {"document_id": c.document_id, "document_title": c.document_title, "page_number": c.page_number,
            "chunk_id": c.chunk_id, "relevance_score": c.relevance_score, "snippet": c.snippet}
