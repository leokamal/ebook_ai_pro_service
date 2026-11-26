"""Builds and owns the single in-process worker (started from main.py's lifespan)."""
import logging
import os
import time
from typing import Optional

from app.core.config import get_settings

logger = logging.getLogger("uvicorn.error")
_runner = None


def get_runner():
    return _runner


def _throttle(fn, every: float):
    last = [0.0]

    def run():
        if time.monotonic() - last[0] >= every:
            last[0] = time.monotonic()
            fn()
    return run


def start_worker() -> Optional[object]:
    """Start the job runner unless disabled/unconfigured. Cheap: loads no model, opens no Chroma."""
    global _runner
    s = get_settings()
    if os.environ.get("WORKER_ENABLED", "true").lower() in ("0", "false", "no") or not (
            s.configured and s.service_role_key):
        logger.info("Pro worker not started (disabled or unconfigured)")
        return None
    from app.core.dependencies import (build_ingestion, get_account_service, get_service_db, get_storage,
                                       get_vector_store)
    from app.services.account import process_deletion_requests
    from app.repositories.documents import DocumentAdminRepository
    from app.repositories.account import AccountRepository
    from app.repositories.jobs import JobQueue
    from app.services.maintenance import Maintenance
    from app.workers.runner import IngestHandler, ThreadJobRunner

    db = get_service_db()
    ingestion = build_ingestion(s)
    queue = JobQueue(db)
    maint = Maintenance(DocumentAdminRepository(db), queue, get_vector_store(), ingestion)
    accounts = AccountRepository(db)
    finish_deletions = _throttle(maint.janitor, 15)

    def hourly():
        maint.prune_jobs(s.job_retention_days)
        maint.clean_orphans(accounts, s.orphan_max_age_hours)
    clean_up = _throttle(hourly, 3600)

    def delete_accounts():
        if accounts.list_deletion_requests():
            process_deletion_requests(accounts, get_account_service(db, get_storage(), get_vector_store()))
    requested_deletions = _throttle(delete_accounts, 30)

    def janitor():
        finish_deletions()
        requested_deletions()
        clean_up()

    _runner = ThreadJobRunner(
        queue, IngestHandler(ingestion), janitor=janitor,
        on_start=lambda: logger.info("startup reconcile: %s", maint.reconcile()),
        poll_seconds=float(os.environ.get("WORKER_POLL_SECONDS", 5)),
        lease_seconds=int(os.environ.get("JOB_LEASE_SECONDS", 300)))
    _runner.start()
    return _runner


def stop_worker():
    global _runner
    if _runner is not None:
        _runner.stop()
        _runner = None
