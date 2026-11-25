"""Vector store abstraction + ChromaDB implementation (the only vector DB; in-process, never public).

Security model: the metadata filter is NOT the boundary. Callers must pass document ids already
validated against Postgres (RLS), and every hit is re-checked in code against that validated set.
Chroma calls are blocking: use from `def` routes / the worker thread.
"""
import logging
import os
import threading
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol, Sequence

from app.core.exceptions import ServiceUnavailable

logger = logging.getLogger("uvicorn.error")
SECURITY = logging.getLogger("uvicorn.error.security")


def chunk_id(document_id: str, chunk_index: int) -> str:
    """Deterministic id => re-ingesting a document upserts instead of duplicating."""
    return str(uuid.uuid5(uuid.UUID(document_id), str(chunk_index)))


@dataclass
class ChunkRecord:
    document_id: str
    user_id: str
    document_title: str
    chunk_index: int
    page_number: int
    text: str
    embedding: list
    index_run: str
    language: Optional[str] = None

    @property
    def id(self) -> str:
        return chunk_id(self.document_id, self.chunk_index)


@dataclass
class SearchHit:
    chunk_id: str
    document_id: str
    document_title: str
    page_number: int
    chunk_index: int
    score: float  # cosine similarity, higher is better
    text: str
    language: Optional[str] = None
    extra: dict = field(default_factory=dict)


class VectorStore(Protocol):
    def upsert_chunks(self, chunks: Sequence[ChunkRecord]) -> None: ...
    def delete_document(self, document_id: str, user_id: str) -> None: ...
    def delete_stale(self, document_id: str, user_id: str, keep_run: str) -> None: ...
    def count(self, document_id: str, user_id: str) -> int: ...
    def query(self, embedding: list, user_id: str, document_ids: Sequence[str], top_k: int) -> list[SearchHit]: ...
    def health(self) -> dict: ...
    def reset_collection(self) -> None: ...


class ChromaVectorStore:
    def __init__(self, path: str, collection_name: str, embedding_model: str, dimension: int,
                 batch_size: int = 32, client_factory: Optional[Callable[[str], object]] = None):
        self.path, self.collection_name = path, collection_name
        self.embedding_model, self.dimension, self.batch_size = embedding_model, dimension, batch_size
        self._client_factory = client_factory
        self._client = self._col = None
        self._state = "closed"  # closed | ready | needs_reindex | error
        self._error = None
        # One re-entrant lock for open/close/reset and all writes (no lock-order deadlocks).
        # Reads are lock-free: they grab a local reference to the collection.
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ lifecycle
    @property
    def _meta(self):
        return {"hnsw:space": "cosine", "embedding_model": self.embedding_model,
                "embedding_dimension": self.dimension}

    def _matches_config(self, col) -> bool:
        meta = col.metadata or {}
        return (meta.get("embedding_model") == self.embedding_model
                and meta.get("embedding_dimension") == self.dimension
                and meta.get("hnsw:space", "cosine") == "cosine")  # score = 1 - cosine distance

    def _get_or_create(self):
        """Never get_or_create_collection(metadata=...): how it treats existing metadata varies by
        chromadb version. Fetch first, create only when missing."""
        try:
            return self._client.get_collection(self.collection_name)
        except Exception:
            return self._client.create_collection(self.collection_name, metadata=self._meta)

    def _open(self):
        if self._state in ("ready", "needs_reindex"):
            return
        with self._lock:
            if self._state in ("ready", "needs_reindex"):
                return
            try:
                if self._client is None:
                    if self._client_factory:
                        self._client = self._client_factory(self.path)
                    else:
                        os.makedirs(self.path, exist_ok=True)
                        import chromadb  # lazy; pysqlite3 swap already happened in main.py
                        self._client = chromadb.PersistentClient(path=self.path)
                self._col = self._get_or_create()
                if self._matches_config(self._col):
                    self._state = "ready"
                else:
                    logger.error("Chroma collection was built with a different embedding model/dimension: "
                                 "reindex required")
                    self._state = "needs_reindex"
            except Exception:
                self._state, self._error = "error", "open_failed"
                logger.exception("Chroma open failed")
                raise ServiceUnavailable("Vector index unavailable") from None

    def _ready(self):
        self._open()
        if self._state != "ready":
            raise ServiceUnavailable("Vector index needs reindexing" if self._state == "needs_reindex"
                                     else "Vector index unavailable")
        return self._col

    def close(self):
        with self._lock:
            try:
                if self._client is not None and hasattr(self._client, "_system"):
                    self._client._system.stop()
                    from chromadb.api.shared_system_client import SharedSystemClient
                    SharedSystemClient.clear_system_cache()  # so the same path can be reopened
            except Exception:
                pass
            self._client = self._col = None
            self._state = "closed"

    @property
    def opened(self) -> bool:
        return self._state != "closed"

    # ------------------------------------------------------------------ writes
    def upsert_chunks(self, chunks: Sequence[ChunkRecord]) -> None:
        with self._lock:
            col = self._ready()
            for i in range(0, len(chunks), self.batch_size):
                batch = chunks[i:i + self.batch_size]
                for c in batch:
                    if len(c.embedding) != self.dimension:
                        raise ValueError("Embedding dimension does not match the collection")
                col.upsert(
                    ids=[c.id for c in batch],
                    embeddings=[list(c.embedding) for c in batch],
                    documents=[c.text for c in batch],
                    metadatas=[{k: v for k, v in {
                        "user_id": c.user_id, "document_id": c.document_id, "document_title": c.document_title,
                        "page_number": c.page_number, "chunk_index": c.chunk_index, "language": c.language,
                        "index_run": c.index_run, "embedding_model": self.embedding_model,
                        "char_count": len(c.text)}.items() if v is not None} for c in batch])

    @staticmethod
    def _doc_where(document_id, user_id, *extra):
        return {"$and": [{"document_id": document_id}, {"user_id": user_id}, *extra]}

    def delete_document(self, document_id: str, user_id: str) -> None:
        with self._lock:
            self._ready().delete(where=self._doc_where(document_id, user_id))

    def delete_stale(self, document_id: str, user_id: str, keep_run: str) -> None:
        """After a successful (re)index: drop chunks of this document written by older runs."""
        with self._lock:
            self._ready().delete(where=self._doc_where(document_id, user_id, {"index_run": {"$ne": keep_run}}))

    def reset_collection(self) -> None:
        """Drop and recreate the collection with the CURRENT model metadata (admin reindex).
        Fails closed: if the old collection cannot be dropped, the store stays unusable."""
        with self._lock:
            try:
                self._open()
            except ServiceUnavailable:
                if self._client is None:
                    raise
            try:
                self._client.delete_collection(self.collection_name)
            except Exception as e:
                if "not exist" not in str(e).lower() and "not found" not in str(e).lower():
                    self._state = "error"
                    logger.exception("delete_collection failed")
                    raise ServiceUnavailable("Vector index reset failed") from None
            col = self._client.create_collection(self.collection_name, metadata=self._meta)
            if not self._matches_config(col):
                self._state = "error"
                raise ServiceUnavailable("Vector index reset failed")
            self._col, self._state, self._error = col, "ready", None

    # ------------------------------------------------------------------ reads
    def count(self, document_id: str, user_id: str) -> int:
        col = self._ready()
        return len(col.get(where=self._doc_where(document_id, user_id), include=[])["ids"])

    def query(self, embedding: list, user_id: str, document_ids: Sequence[str], top_k: int) -> list[SearchHit]:
        if not document_ids or top_k < 1:
            return []
        if len(embedding) != self.dimension:
            raise ValueError("Query embedding dimension does not match the collection")
        col = self._ready()
        allowed = set(document_ids)
        res = col.query(query_embeddings=[list(embedding)], n_results=top_k,
                        where={"$and": [{"user_id": user_id}, {"document_id": {"$in": list(allowed)}}]},
                        include=["documents", "metadatas", "distances"])
        hits = []
        for cid, text, meta, dist in zip(res["ids"][0], res["documents"][0], res["metadatas"][0], res["distances"][0]):
            # defense in depth: the metadata filter is not trusted as the security boundary
            if meta.get("user_id") != user_id or meta.get("document_id") not in allowed:
                SECURITY.error("vector store returned a foreign chunk; dropped (chunk=%s)", cid)
                continue
            hits.append(SearchHit(chunk_id=cid, document_id=meta["document_id"],
                                  document_title=meta.get("document_title", ""),
                                  page_number=int(meta.get("page_number", 0)),
                                  chunk_index=int(meta.get("chunk_index", 0)), score=1.0 - float(dist),
                                  text=text, language=meta.get("language")))
        return hits

    def health(self) -> dict:
        try:
            self._open()
        except ServiceUnavailable:
            pass
        out = {"state": self._state, "collection": self.collection_name, "embedding_model": self.embedding_model}
        if self._state in ("ready", "needs_reindex") and self._col is not None:
            try:
                out["chunks"] = self._col.count()
            except Exception:
                out["state"] = "error"
        if self._error:
            out["error"] = self._error
        return out
