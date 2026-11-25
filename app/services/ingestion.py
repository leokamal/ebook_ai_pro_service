"""Document ingestion pipeline: PDF in storage -> pages -> chunks -> embeddings -> Chroma -> Postgres.

Commit-last ordering (mirrors the legacy "VERSION.json last" rule): new chunks are upserted first,
stale chunks of older runs are removed and the document is marked `completed` only after every
batch succeeded. A failed re-run therefore never destroys the previously good index.
Blocking code: runs in the worker thread.
"""
import logging
import os
import tempfile
from dataclasses import dataclass
from typing import Callable, Optional

from app.core.exceptions import NotFound
from app.repositories.documents import ChunkRepository, DocumentAdminRepository
from app.services import pdf_extraction as pdf
from app.services.vector_store import ChunkRecord, chunk_id

logger = logging.getLogger("uvicorn.error")


class LeaseLost(Exception):
    """The job lease was taken over by another worker; stop without touching state."""


@dataclass
class IngestConfig:
    bucket: str
    max_pdf_bytes: int = 50 * 1024 * 1024
    max_pdf_pages: int = 1000
    embed_batch: int = 32


def safe_storage_path(doc: dict, user_id: str) -> str:
    """The path must be exactly {user_id}/{document_id}/{file_name}; anything else is refused."""
    path = doc["file_path"]
    parts = path.split("/")
    if (len(parts) != 3 or parts[0] != user_id or parts[1] != doc["id"] or parts[2] != doc["file_name"]
            or any(p in ("", ".", "..") for p in parts)):
        raise pdf.IngestError("invalid_path", "Invalid storage path")
    return path


class IngestionService:
    def __init__(self, docs: DocumentAdminRepository, chunks: ChunkRepository, storage, embedder, store,
                 config: IngestConfig, open_pdf: Callable = pdf.open_pdf, splitter: Callable = pdf.split_page,
                 iter_pages: Callable = pdf.iter_pages):
        self.docs, self.chunks, self.storage = docs, chunks, storage
        self.embedder, self.store, self.cfg = embedder, store, config
        self._open_pdf, self._split, self._iter_pages = open_pdf, splitter, iter_pages

    def process(self, job: dict, heartbeat: Callable[[int], bool] = lambda pct: True) -> None:
        """Raises IngestError (permanent), LeaseLost, or any other exception (treated as transient)."""
        user_id, doc_id, run = job["user_id"], job["document_id"], job["id"]
        doc = self.docs.get_owned(doc_id, user_id)  # ownership: the service role bypasses RLS
        if doc["status"] == "deleting":
            return
        path = safe_storage_path(doc, user_id)

        with tempfile.TemporaryDirectory(prefix="ingest_") as tmp:  # removed on every exit path
            local = os.path.join(tmp, "document.pdf")  # never derived from the user's file name
            try:
                size = self.storage.download_to_path(self.cfg.bucket, path, local, self.cfg.max_pdf_bytes)
            except ValueError:
                raise pdf.IngestError("file_too_large", "The PDF is larger than the allowed size") from None
            if size != doc["file_size"]:  # file_size is client-supplied at registration: verify the real object
                raise pdf.IngestError("size_mismatch", "The uploaded file does not match its registered size")
            with open(local, "rb") as f:
                if f.read(5) != b"%PDF-":
                    raise pdf.IngestError("corrupt_pdf", "The file is not a valid PDF")

            pdf_doc, page_count = self._open_pdf(local, self.cfg.max_pdf_pages)
            meta, batch, index = [], [], 0
            try:
                for page_no, text in self._iter_pages(pdf_doc):
                    for piece in self._split(text):
                        batch.append((index, page_no, piece))
                        index += 1
                        if len(batch) >= self.cfg.embed_batch:
                            self._flush(batch, doc, user_id, run, meta)
                            batch = []
                    if not heartbeat(int(90 * page_no / max(page_count, 1))):
                        raise LeaseLost()
                if batch:
                    self._flush(batch, doc, user_id, run, meta)
            finally:
                pdf_doc.close()

        if not meta:
            raise pdf.IngestError("no_extractable_text",
                                  "No text found in this PDF (scanned PDFs are not supported yet)")

        # ---- commit (everything above succeeded). Re-check the lease: a stalled worker must not commit
        # next to the worker that took its job over.
        if not heartbeat(95):
            raise LeaseLost()
        try:
            current = self.docs.get_owned(doc_id, user_id)
            if current["status"] == "deleting":  # deleted while we worked: clean up and stop
                self.store.delete_document(doc_id, user_id)
                return
            self.store.delete_stale(doc_id, user_id, run)
            self.chunks.replace(doc_id, user_id, meta)
            if self.store.count(doc_id, user_id) != len(meta):
                raise RuntimeError("index verification failed")  # transient: retried with backoff
            # compare-and-set: only a document still `processing` may become `completed`
            self.docs.update_owned(doc_id, user_id, {
                "status": "completed", "index_state": "indexed", "chunk_count": len(meta),
                "page_count": page_count, "embedding_model": self.embedder.model_name,
                "error_message": None}, only_if_status="processing")
        except NotFound:  # row deleted / flipped to `deleting` meanwhile: drop what we wrote, do not retry
            self.store.delete_document(doc_id, user_id)
            return
        heartbeat(100)
        logger.info("ingested document=%s chunks=%d pages=%d", doc_id, len(meta), page_count)

    def _flush(self, batch, doc, user_id, run, meta):
        vectors = self.embedder.embed_documents([t for _, _, t in batch])
        self.store.upsert_chunks([
            ChunkRecord(document_id=doc["id"], user_id=user_id, document_title=doc["title"], chunk_index=i,
                        page_number=p, text=t, embedding=v, index_run=run, language=doc.get("language"))
            for (i, p, t), v in zip(batch, vectors)])
        meta.extend({"id": chunk_id(doc["id"], i), "chunk_index": i, "page_number": p} for i, p, _ in batch)

    def delete_document(self, doc: dict) -> None:
        """Finish a deletion (idempotent; safe to repeat): vectors, storage object, then the row."""
        user_id = doc["user_id"]
        self.store.delete_document(doc["id"], user_id)
        try:
            self.storage.remove_object(self.cfg.bucket, safe_storage_path(
                {"id": doc["id"], "file_path": doc["file_path"], "file_name": doc["file_path"].split("/")[-1]}, user_id))
        except pdf.IngestError:
            logger.warning("skipping storage removal: unexpected path for document %s", doc["id"])
        self.docs.delete_owned(doc["id"], user_id)
