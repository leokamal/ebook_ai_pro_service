from typing import Optional

from app.core.exceptions import Conflict, NotFound

JOB_COLUMNS = "id,document_id,kind,status,attempts,max_attempts,progress,error_message,started_at,completed_at,created_at"


class JobReader:
    """User-scoped, read-only (RLS: owner select only)."""

    def __init__(self, db):
        self.db = db

    def active_count(self) -> int:
        return len(self.db.table("processing_jobs").select("id").in_("status", ["queued", "running"])
                   .limit(1000).execute().data)

    def latest_for_document(self, document_id: str) -> Optional[dict]:
        rows = (self.db.table("processing_jobs").select(JOB_COLUMNS).eq("document_id", document_id)
                .order("created_at", desc=True).limit(1).execute().data)
        return rows[0] if rows else None


class JobQueue:
    """Service-role wrapper over the job RPCs (execute is granted to service_role only)."""

    def __init__(self, service_db):
        self.db = service_db

    def enqueue(self, document_id: str, user_id: str, kind: str = "ingest") -> str:
        try:
            return self.db.rpc("enqueue_processing_job", {"p_document_id": document_id, "p_user_id": user_id,
                                                          "p_kind": kind}).execute().data
        except Exception as e:
            msg = str(e)
            if "document_not_found" in msg:
                raise NotFound("Document not found") from None
            if "23505" in msg or "processing_jobs_active_uidx" in msg:
                raise Conflict("Document is already queued or processing") from None
            raise

    def has_running(self) -> bool:
        return bool(self.db.table("processing_jobs").select("id").eq("status", "running").limit(1).execute().data)

    def claim(self, worker_id: str, lease_seconds: int = 300) -> Optional[dict]:
        data = self.db.rpc("claim_processing_job", {"p_worker": worker_id, "p_lease_seconds": lease_seconds}).execute().data
        if isinstance(data, list):
            data = data[0] if data else None
        return data or None

    def heartbeat(self, job_id: str, worker_id: str, progress: int, lease_seconds: int = 300) -> bool:
        return bool(self.db.rpc("heartbeat_job", {"p_job_id": job_id, "p_worker": worker_id, "p_progress": progress,
                                                  "p_lease_seconds": lease_seconds}).execute().data)

    def finish(self, job_id: str, ok: bool, error: Optional[str] = None, retry_after_seconds: Optional[int] = None) -> None:
        self.db.rpc("finish_job", {"p_job_id": job_id, "p_ok": ok, "p_error": error,
                                   "p_retry_after_seconds": retry_after_seconds}).execute()
