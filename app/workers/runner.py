"""DB-backed job runner (MVP dispatcher): a daemon thread polling `claim_processing_job`.

Reliability comes from Postgres, not from this thread: jobs are rows (transactional outbox), claims use
leases, so a crash, restart or free-tier sleep just lets another claim take over after the lease expires.
Swap `ThreadJobRunner` for a Celery/Redis consumer later: only `JobRunner`/`JobHandler` are contracts.
"""
import logging
import socket
import threading
import uuid
from typing import Callable, Optional, Protocol

from app.core.exceptions import NotFound, ServiceUnavailable
from app.services.ingestion import LeaseLost
from app.services.pdf_extraction import IngestError

logger = logging.getLogger("uvicorn.error")


class JobHandler(Protocol):
    def handle(self, job: dict, heartbeat: Callable[[int], bool]) -> None: ...


class JobRunner(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...


def backoff_seconds(attempts: int) -> int:
    return min(30 * 2 ** max(attempts - 1, 0), 900)


class IngestHandler:
    def __init__(self, ingestion):
        self.ingestion = ingestion

    def handle(self, job, heartbeat):
        self.ingestion.process(job, heartbeat)


class ThreadJobRunner:
    def __init__(self, queue, handler: JobHandler, janitor: Optional[Callable[[], None]] = None,
                 on_start: Optional[Callable[[], None]] = None, poll_seconds: float = 5, lease_seconds: int = 300):
        self.queue, self.handler, self.janitor, self.on_start = queue, handler, janitor, on_start
        self.poll, self.lease = poll_seconds, lease_seconds
        self.worker_id = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.alive = False

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="pro-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    # ---- one iteration (public for tests): True if a job was handled
    def run_once(self) -> bool:
        job = self.queue.claim(self.worker_id, self.lease)
        if not job:
            return False
        jid = job["id"]

        def heartbeat(pct: int) -> bool:
            return self.queue.heartbeat(jid, self.worker_id, pct, self.lease)

        def finish(ok: bool, error: Optional[str] = None, retry_after: Optional[int] = None):
            if not self.queue.finish(jid, self.worker_id, ok, error, retry_after):
                logger.warning("job %s: not finished by us (lease taken over, or the job was removed)", jid)

        try:
            self.handler.handle(job, heartbeat)
            finish(True)
        except LeaseLost:
            logger.warning("job %s: lease lost, abandoning", jid)
        except IngestError as e:  # permanent: user-safe message, no retry
            logger.info("job %s failed permanently: %s", jid, e.code)
            finish(False, e.message)
        except NotFound:  # document deleted while queued: nothing to retry
            finish(False, "The document no longer exists")
        except ServiceUnavailable as e:  # e.g. index needs reindex: retrying will not fix it
            logger.error("job %s: %s", jid, e.message)
            finish(False, "The search index is unavailable")
        except Exception:  # transient (network, 5xx, ...): details only in logs
            logger.exception("job %s failed, will retry if attempts remain", jid)
            finish(False, "Temporary processing error", backoff_seconds(job.get("attempts", 1)))
        return True

    def _loop(self):
        self.alive = True
        if self.on_start:
            try:
                self.on_start()
            except Exception:
                logger.exception("startup reconcile failed (run POST /api/v1/admin/reconcile later)")
        while not self._stop.is_set():
            try:
                busy = self.run_once()
                if self.janitor:
                    self.janitor()
            except Exception:
                logger.exception("worker iteration failed")
                busy = False
            if not busy:
                self._stop.wait(self.poll)
        self.alive = False
