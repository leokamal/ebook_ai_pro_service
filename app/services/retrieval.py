"""Query embedding + scoped retrieval. Knows nothing about Gemini or conversations (that is the
RAGOrchestrator's job in Phase 5), and nothing about Chroma beyond the VectorStore protocol."""
from dataclasses import replace
from typing import Optional

from app.core.exceptions import Conflict
from app.repositories.documents import DocumentRepository, content_key
from app.services.embedding import Embedder
from app.services.vector_store import SearchHit, VectorStore, chunk_id

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
        # content id -> the caller's document that holds it (one per content)
        holders = {content_key(d)[0]: d for d in documents if d.get("user_id") in (None, user_id)}
        if not holders:
            return []
        content = {cid: content_key({"user_id": user_id, **d})[1] for cid, d in holders.items()}
        hits = self.store.query(self.embedder.embed_query(query), content, top_k)
        return sorted((self._as_held(h, holders[h.document_id]) for h in hits),
                      key=lambda h: (-h.score, h.chunk_id))  # deterministic order

    @staticmethod
    def _as_held(hit: SearchHit, doc: dict) -> SearchHit:
        """The vectors may be shared: answer with the caller's own document, chunk and title."""
        own = hit.chunk_id if doc["id"] == hit.document_id else chunk_id(doc["id"], hit.chunk_index)
        return replace(hit, document_id=doc["id"], chunk_id=own, document_title=doc.get("title") or hit.document_title)
