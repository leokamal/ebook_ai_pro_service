"""Document ingestion pipeline: PDF in storage -> pages -> chunks -> embeddings -> Chroma -> Postgres.

Commit-last ordering: new chunks are upserted first,
stale chunks of older runs are removed and the document is marked `completed` only after every
batch succeeded. A failed re-run therefore never destroys the previously good index.
Vectors and file belong to the "content" (original document + uploader), which several users' documents may
share: they are written under the content's ids and removed only when no other document needs them.
Blocking code: runs in the worker thread.
"""
import logging
import os
import tempfile
from dataclasses import dataclass
from typing import Callable

from app.core.exceptions import NotFound
from app.repositories.documents import ChunkRepository, DocumentAdminRepository, content_key
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


def safe_storage_path(doc: dict) -> str:
    """The path must be exactly {content_owner_id}/{content_id}/{file_name}; anything else is refused."""
    path = doc["file_path"]
    parts = path.split("/")
    content_id, owner = content_key(doc)
    if (len(parts) != 3 or parts[0] != owner or parts[1] != content_id or parts[2] != doc["file_name"]
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
        path = safe_storage_path(doc)
        try:
            self._index(job, doc, path, heartbeat)
        except BaseException:  # incl. LeaseLost: when an account is deleted the job row vanishes mid-run
            self._discard_if_gone(doc)
            raise

    def _discard_if_unused(self, doc: dict) -> None:
        content_id, owner = content_key(doc)
        if not self.docs.live_holders(content_id, excluding=doc["id"]):
            self.store.delete_document(content_id, owner)

    def _discard_if_gone(self, doc: dict) -> None:
        """Never leave vectors behind for a document that was deleted while we were writing them."""
        try:
            try:
                if self.docs.get_owned(doc["id"], doc["user_id"])["status"] != "deleting":
                    return
            except NotFound:
                pass
            self._discard_if_unused(doc)
        except Exception:
            logger.exception("could not discard vectors of document %s", doc["id"])

    def _index(self, job: dict, doc: dict, path: str, heartbeat: Callable[[int], bool]) -> None:
        user_id, doc_id, run = job["user_id"], job["document_id"], job["id"]

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
                            self._flush(batch, doc, run, meta)
                            batch = []
                    if not heartbeat(int(90 * page_no / max(page_count, 1))):
                        raise LeaseLost()
                if batch:
                    self._flush(batch, doc, run, meta)
            finally:
                pdf_doc.close()

        if not meta:
            raise pdf.IngestError("no_extractable_text",
                                  "No text found in this PDF (scanned or image-only PDFs are not supported)")

        # ---- commit (everything above succeeded). Re-check the lease: a stalled worker must not commit
        # next to the worker that took its job over.
        if not heartbeat(95):
            raise LeaseLost()
        content_id, owner = content_key(doc)
        fields = {"status": "completed", "index_state": "indexed", "chunk_count": len(meta),
                  "page_count": page_count, "embedding_model": self.embedder.model_name, "error_message": None,
                  "error_code": None}
        try:
            current = self.docs.get_owned(doc_id, user_id)
            if current["status"] == "deleting":  # deleted while we worked: clean up and stop
                self._discard_if_unused(doc)
                return
            self.store.delete_stale(content_id, owner, run)
            self.chunks.replace(doc_id, user_id, self._chunk_rows(doc_id, meta))
            if self.store.count(content_id, owner) != len(meta):
                raise RuntimeError("index verification failed")  # transient: retried with backoff
            # the other holders first: if one fails, the whole job is retried
            self._commit_other_holders(content_id, doc_id, meta, fields)
            # compare-and-set: only a document still `processing` may become `completed`
            self.docs.update_owned(doc_id, user_id, fields, only_if_status="processing")
        except NotFound:  # row deleted / flipped to `deleting` meanwhile: drop what we wrote, do not retry
            self._discard_if_unused(doc)
            return
        heartbeat(100)
        logger.info("ingested document=%s chunks=%d pages=%d", doc_id, len(meta), page_count)

    @staticmethod
    def _chunk_rows(document_id: str, meta: list) -> list:
        return [{"id": chunk_id(document_id, i), "chunk_index": i, "page_number": p} for i, p in meta]

    def _commit_other_holders(self, content_id: str, doc_id: str, meta: list, fields: dict) -> None:
        """A rebuild of shared content updates every holder (each has its own chunk rows)."""
        for h in self.docs.list_holders(content_id):
            if h["id"] == doc_id or h["status"] not in ("completed", "failed"):
                continue  # `deleting` is left alone; queued/processing holders have their own job
            try:
                self.chunks.replace(h["id"], h["user_id"], self._chunk_rows(h["id"], meta))
            except Exception:
                if any(x["id"] == h["id"] and x["status"] != "deleting" for x in self.docs.list_holders(content_id)):
                    raise
                continue  # that holder was deleted while we committed: no reason to embed the book again
            try:
                self.docs.update_owned(h["id"], h["user_id"], fields, only_if_status=h["status"])
            except NotFound:
                pass

    def _flush(self, batch, doc, run, meta):
        content_id, owner = content_key(doc)
        vectors = self.embedder.embed_documents([t for _, _, t in batch])
        self.store.upsert_chunks([
            ChunkRecord(document_id=content_id, user_id=owner, document_title=doc["title"], chunk_index=i,
                        page_number=p, text=t, embedding=v, index_run=run, language=doc.get("language"))
            for (i, p, t), v in zip(batch, vectors)])
        meta.extend((i, p) for i, p, _ in batch)

    def delete_document(self, doc: dict) -> None:
        """Finish a deletion (idempotent; safe to repeat): vectors and storage object unless another user's
        document still uses them, then the row."""
        user_id = doc["user_id"]
        try:
            row = self.docs.get_owned(doc["id"], user_id)
        except NotFound:
            return
        content_id, owner = content_key(row)
        if not self.docs.live_holders(content_id, excluding=row["id"]):
            self.store.delete_document(content_id, owner)
            try:
                self.storage.remove_object(self.cfg.bucket, safe_storage_path(row))
            except pdf.IngestError:
                logger.warning("skipping storage removal: unexpected path for document %s", row["id"])
        self.docs.delete_owned(row["id"], user_id)
