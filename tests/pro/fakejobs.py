"""FakeServiceDB: FakeDB + the job RPCs, mirroring the SQL semantics (outbox, lease, retry)."""
import itertools

from tests.pro.fakedb import FakeDB, _Result


class _Rpc:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return _Result(self.fn())


class FakeServiceDB(FakeDB):
    def __init__(self, tables=None):
        super().__init__(tables, user_id=None)
        self.now = 0.0
        self._seq = itertools.count(1)
        self.storage = None  # a FakeStorage, for the storage-listing RPCs

    def _jobs(self):
        return self.tables.setdefault("processing_jobs", [])

    def _doc(self, doc_id):
        return next((d for d in self.tables.get("documents", []) if d["id"] == doc_id), None)

    def rpc(self, name, params):
        return _Rpc(lambda: getattr(self, "_" + name)(**params))

    def _enqueue_processing_job(self, p_document_id, p_user_id, p_kind="ingest"):
        doc = self._doc(p_document_id)
        if not doc or doc["user_id"] != p_user_id or doc["status"] == "deleting":
            raise Exception("document_not_found")
        if any(j["document_id"] == p_document_id and j["status"] in ("queued", "running") for j in self._jobs()):
            raise Exception("23505 processing_jobs_active_uidx")
        n = next(self._seq)
        job = {"id": f"00000000-0000-0000-0000-{n:012d}", "user_id": p_user_id, "document_id": p_document_id,
               "kind": p_kind, "status": "queued", "attempts": 0, "max_attempts": 3, "progress": 0,
               "run_after": 0.0, "locked_by": None, "locked_until": None, "error_message": None, "error_code": None,
               "created_at": f"2026-01-01T00:00:{n:02d}+00:00"}
        self._jobs().append(job)
        doc.update(status="queued", error_message=None, error_code=None)
        return job["id"]

    def _claim_processing_job(self, p_worker, p_lease_seconds=300):
        for j in sorted(self._jobs(), key=lambda j: (j["run_after"], j["created_at"])):
            claimable = (j["status"] == "queued" and j["run_after"] <= self.now) or (
                j["status"] == "running" and j["locked_until"] < self.now)
            if not claimable:
                continue
            doc = self._doc(j["document_id"])
            if j["attempts"] >= j["max_attempts"]:
                j.update(status="failed", locked_by=None, error_message=j["error_message"] or "Too many attempts",
                         error_code=j["error_code"] or "attempts_exhausted")
                if doc and doc["status"] != "deleting":
                    doc.update(status="failed", error_message=j["error_message"], error_code=j["error_code"])
                continue
            j.update(status="running", attempts=j["attempts"] + 1, locked_by=p_worker,
                     locked_until=self.now + p_lease_seconds)
            if doc and doc["status"] != "deleting":
                doc["status"] = "processing"
            return [dict(j)]
        return []

    def _heartbeat_job(self, p_job_id, p_worker, p_progress, p_lease_seconds=300):
        j = next((j for j in self._jobs() if j["id"] == p_job_id), None)
        if not j or j["locked_by"] != p_worker or j["status"] != "running":
            return False
        j.update(locked_until=self.now + p_lease_seconds, progress=max(0, min(100, p_progress)))
        return True

    def _finish_job(self, p_job_id, p_ok, p_worker, p_error=None, p_retry_after_seconds=None, p_error_code=None):
        j = next((j for j in self._jobs() if j["id"] == p_job_id), None)
        if not j or j["locked_by"] != p_worker or j["status"] != "running":
            return False
        if p_ok:
            j.update(status="completed", progress=100, locked_by=None)
        elif p_retry_after_seconds is not None and j["attempts"] < j["max_attempts"]:
            j.update(status="queued", locked_by=None, error_message=p_error, error_code=p_error_code, run_after=self.now + p_retry_after_seconds)
        else:
            j.update(status="failed", locked_by=None, error_message=p_error, error_code=p_error_code)
            doc = self._doc(j["document_id"])
            if doc and doc["status"] != "deleting":
                doc.update(status="failed", error_message=p_error, error_code=p_error_code)
        return True

    def _prune_processing_jobs(self, p_older_than, p_limit=500):
        return 0  # age-based deletion is covered by the SQL tests

    # ---- storage space RPCs (20261126000000_user_space.sql)
    def _user_storage_usage(self, p_user):
        mine = [v for k, v in self.storage.files.items() if k.startswith(p_user + "/")]
        return [{"bytes": sum(len(v) for v in mine), "objects": len(mine)}]

    def _list_user_objects(self, p_user):
        return sorted(k for k in self.storage.files if k.startswith(p_user + "/"))

    def _list_orphan_objects(self, p_older_than, p_limit=50):
        hours = max(int(p_older_than.split()[0]), 1)
        registered = {d["file_path"] for d in self.tables.get("documents", [])}
        return [k for k in sorted(self.storage.files)
                if self.storage.ages.get(k, 0) >= hours and k not in registered][:p_limit]
