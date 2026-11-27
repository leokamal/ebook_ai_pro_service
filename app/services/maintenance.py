"""Operator-level maintenance: janitor, reconcile (Chroma vs Postgres) and reindex (model change).

These are deliberately cross-user (admin token / worker only). Each row is handled with ITS OWN user_id.
"""
import logging
import threading

from app.core.exceptions import Conflict, NotFound
from app.repositories.documents import DocumentAdminRepository, content_key
from app.repositories.jobs import JobQueue

logger = logging.getLogger("uvicorn.error")
PAGE = 200
# `reindex_all` and the stale sweep must not overlap (one process): the sweep would queue work before the reset.
_REBUILD = threading.Lock()


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

    def clean_orphans(self, repo, max_age_hours: int, limit: int = 50) -> int:
        """Remove uploads that no document row refers to (never registered, or left by a failed delete)."""
        removed = 0
        storage, bucket = self.ingestion.storage, self.ingestion.cfg.bucket
        for name in repo.list_orphan_objects(max(max_age_hours, 1), limit):
            try:
                if repo.path_registered(name):  # registered since it was listed: keep it
                    continue
                storage.remove_object(bucket, name)
                removed += 1
            except Exception:
                logger.exception("orphan cleanup failed for one object")
        if removed:
            logger.info("orphan cleanup removed %d objects", removed)
        return removed

    def prune_jobs(self, retention_days: int) -> int:
        """Delete old finished jobs. Never raises: it must not stop the rest of the hourly cleanup."""
        try:
            return self.queue.prune(max(retention_days, 1))
        except Exception:
            logger.exception("job pruning failed")
            return 0

    def _enqueue_reindex(self, holders: list) -> bool:
        """One job per content, on any holder; its success updates all of them."""
        content_id, _ = content_key(holders[0])
        if any(h["status"] in ("queued", "processing") for h in self.docs.list_holders(content_id)):
            return False  # a job is already rebuilding it
        for h in holders:
            try:
                self.queue.enqueue(h["id"], h["user_id"], "reindex")
                return True
            except Conflict:  # already queued/running
                return False
            except NotFound:  # deleted meanwhile: try the next holder
                continue
        return False

    def _completed_by_content(self) -> list:
        groups: dict = {}
        for d in self._all_completed():
            groups.setdefault(content_key(d)[0], []).append(d)
        return list(groups.values())

    def _all_completed(self) -> list:
        """Snapshot first: enqueueing flips status, which would shift offset paging."""
        out, offset = [], 0
        while True:
            rows = self.docs.list_indexed(PAGE, offset)
            out.extend(rows)
            if len(rows) < PAGE:
                return out
            offset += PAGE

    def resume_rebuilds(self) -> int:
        """Shared content can be left waiting: the holder carrying its rebuild job was deleted, or that job
        failed (which marks only its own row). Queue a new job, or pass the failure on to the waiting holders.
        Postgres only, unless something is actually waiting."""
        if not _REBUILD.acquire(blocking=False):
            return 0
        try:
            waiting: dict = {}
            for d in self.docs.list_unindexed():
                waiting.setdefault(content_key(d)[0], []).append(d)
            if not waiting or self.store.health().get("state") != "ready":
                return 0
            queued = 0
            for content_id, holders in waiting.items():
                everyone = self.docs.list_holders(content_id)
                if any(h["status"] in ("queued", "processing") for h in everyone):
                    continue
                failed = next((h for h in everyone if h["status"] == "failed" and h["index_state"] != "indexed"), None)
                if failed is None:
                    queued += self._enqueue_reindex(holders)
                    continue
                for h in holders:  # they can retry; a successful retry restores every holder
                    try:
                        self.docs.update_owned(h["id"], h["user_id"], {
                            "status": "failed", "error_message": failed.get("error_message"),
                            "error_code": failed.get("error_code")}, only_if_status="completed")
                    except NotFound:
                        pass
            return queued
        finally:
            _REBUILD.release()

    def reconcile(self) -> dict:
        """Completed+indexed content whose vectors are missing/partial -> `missing` + one reindex job."""
        checked = flagged = 0
        for holders in self._completed_by_content():
            if any(d["index_state"] in ("stale", "missing") for d in holders):  # e.g. a reindex that crashed
                flagged += self._enqueue_reindex(holders)
                continue
            checked += 1
            content_id, owner = content_key(holders[0])
            if self.store.count(content_id, owner) != holders[0]["chunk_count"]:  # raises if index unusable
                for d in holders:
                    self.docs.update_owned(d["id"], d["user_id"], {"index_state": "missing"})
                self._enqueue_reindex(holders)
                flagged += 1
        return {"checked": checked, "reindex_queued": flagged}

    def reindex_all(self) -> dict:
        """Embedding model changed: recreate the collection and rebuild every completed document."""
        with _REBUILD:
            return self._reindex_all()

    def _reindex_all(self) -> dict:
        if self.queue.has_running():  # a running ingest would write into the collection we are about to drop
            raise Conflict("Jobs are running; retry reindex when the queue is idle")
        groups = self._completed_by_content()
        for holders in groups:  # 1) flag first: documents become unsearchable before anything is destroyed
            for d in holders:
                self.docs.update_owned(d["id"], d["user_id"], {"index_state": "stale"})
        self.store.reset_collection()  # 2) fails closed; afterwards the store matches the CURRENT model
        queued = 0
        for holders in groups:  # 3) queue rebuilds (reconcile re-queues any that a crash skipped)
            queued += self._enqueue_reindex(holders)
        return {"reindex_queued": queued}
