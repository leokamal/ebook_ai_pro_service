"""Query embedding + scoped retrieval. Knows nothing about Gemini or conversations (that is the
RAGOrchestrator's job in Phase 5), and nothing about Chroma beyond the VectorStore protocol."""
from typing import Optional

from app.core.exceptions import Conflict
from app.repositories.documents import DocumentRepository
from app.services.embedding import Embedder
from app.services.vector_store import SearchHit, VectorStore

MAX_TOP_K = 50


def resolve_searchable_documents(docs: DocumentRepository, document_id: Optional[str] = None) -> list[dict]:
    """Ownership + readiness check through Postgres (user-scoped client => RLS).

    single document: 404 if not the caller's, 409 `document_not_indexed` unless completed + indexed.
    all documents: every completed + indexed document of the caller (possibly none).
    """
    if document_id:
        doc = docs.get(document_id)  # NotFound for foreign/missing ids
        if doc["status"] != "completed" or doc["index_state"] != "indexed":
            raise Conflict("Document is not indexed yet", code="document_not_indexed")
        return [doc]
    return docs.list_searchable()


class RetrievalService:
    def __init__(self, embedder: Embedder, store: VectorStore):
        self.embedder, self.store = embedder, store

    def search(self, user_id: str, documents: list[dict], query: str, top_k: int = 6) -> list[SearchHit]:
        """`documents` MUST come from resolve_searchable_documents (validated for `user_id`)."""
        top_k = max(1, min(top_k, MAX_TOP_K))
        ids = [d["id"] for d in documents]
        if not ids:
            return []
        hits = self.store.query(self.embedder.embed_query(query), user_id, ids, top_k)
        return sorted(hits, key=lambda h: (-h.score, h.chunk_id))  # deterministic order
