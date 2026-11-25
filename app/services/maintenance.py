"""Operator-level maintenance: janitor, reconcile (Chroma vs Postgres) and reindex (model change).

These are deliberately cross-user (admin token / worker only). Each row is handled with ITS OWN user_id.
"""
import logging

from app.core.exceptions import AppError, Conflict
from app.repositories.documents import DocumentAdminRepository
from app.repositories.jobs import JobQueue

logger = logging.getLogger("uvicorn.error")
PAGE = 200


class Maintenance:
    def __init__(self, docs: DocumentAdminRepository, queue: JobQueue, store, ingestion):
        self.docs, self.queue, self.store, self.ingestion = docs, queue, store, ingestion

    def janitor(self) -> int:
        """Finish deletions that were interrupted (documents stuck in `deleting`)."""
        done = 0
        for doc in self.docs.list_deleting(10):
            try:
                self.ingestion.delete_document(doc)
                done += 1
            except Exception:
                logger.exception("janitor: could not finish deleting %s", doc["id"])
                try:  # touch the row (updated_at trigger) so one stuck document cannot starve the others
                    self.docs.update_owned(doc["id"], doc["user_id"], {"error_message": "Deletion pending"})
                except Exception:
                    pass
        return done

    def _enqueue_reindex(self, doc_id: str, user_id: str) -> bool:
        try:
            self.queue.enqueue(doc_id, user_id, "reindex")
            return True
        except Conflict:  # already queued/running
            return False

    def _all_completed(self) -> list:
        """Snapshot first: enqueueing flips status, which would shift offset paging."""
        out, offset = [], 0
        while True:
            rows = self.docs.list_indexed(PAGE, offset)
            out.extend(rows)
            if len(rows) < PAGE:
                return out
            offset += PAGE

    def reconcile(self) -> dict:
        """Completed+indexed documents whose vectors are missing/partial -> `missing` + reindex job."""
        checked = flagged = 0
        for d in self._all_completed():
            if d["index_state"] in ("stale", "missing"):  # e.g. a reindex that crashed before queueing
                flagged += self._enqueue_reindex(d["id"], d["user_id"])
                continue
            checked += 1
            if self.store.count(d["id"], d["user_id"]) != d["chunk_count"]:  # raises if index unusable
                self.docs.update_owned(d["id"], d["user_id"], {"index_state": "missing"})
                self._enqueue_reindex(d["id"], d["user_id"])
                flagged += 1
        return {"checked": checked, "reindex_queued": flagged}

    def reindex_all(self) -> dict:
        """Embedding model changed: recreate the collection and rebuild every completed document."""
        if self.queue.has_running():  # a running ingest would write into the collection we are about to drop
            raise Conflict("Jobs are running; retry reindex when the queue is idle")
        docs = self._all_completed()
        for d in docs:  # 1) flag first: documents become unsearchable before anything is destroyed
            self.docs.update_owned(d["id"], d["user_id"], {"index_state": "stale"})
        self.store.reset_collection()  # 2) fails closed; afterwards the store matches the CURRENT model
        queued = 0
        for d in docs:  # 3) queue rebuilds (reconcile re-queues any that a crash skipped)
            queued += self._enqueue_reindex(d["id"], d["user_id"])
        return {"reindex_queued": queued}
