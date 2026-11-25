from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.dependencies import (CurrentUserDep, get_documents_repo, get_embedding_service, get_vector_store)
from app.core.ratelimit import search_limiter
from app.repositories.documents import DocumentRepository
from app.schemas.search import SearchRequest, SearchResponse
from app.services.rag_parts import RerankingService
from app.services.retrieval import RetrievalService, resolve_searchable_documents

router = APIRouter(tags=["search"])
DocsDep = Annotated[DocumentRepository, Depends(get_documents_repo)]


@router.post("/search", response_model=SearchResponse, summary="Semantic search over your documents (no LLM)",
             responses={404: {"description": "Document not found (or not yours)"},
                        409: {"description": "Document not indexed yet"},
                        503: {"description": "Index unavailable"}})
def search(body: SearchRequest, user: CurrentUserDep, docs: DocsDep, embedder=Depends(get_embedding_service),
           store=Depends(get_vector_store)):
    """Same scope rules as chat: ownership and readiness come from Postgres (RLS) first, then Chroma is
    queried only for those document ids, and every hit is re-checked. Plain `def` (blocking work)."""
    search_limiter().check(user.id)
    documents = resolve_searchable_documents(docs, str(body.document_id) if body.document_id else None)
    hits = RetrievalService(embedder, store).search(
        user.id, documents, body.query, max(body.top_k * 3, 10))
    multi = body.mode == "all_documents"
    ranked = RerankingService(max_per_doc=max(body.top_k // 2, 3)).rerank(hits, body.top_k, per_doc_cap=multi)
    return {"searched_documents": len(documents),
            "results": [{"chunk_id": h.chunk_id, "document_id": h.document_id, "document_title": h.document_title,
                         "page_number": h.page_number, "score": round(h.score, 4), "text": h.text} for h in ranked]}
