"""Multi-PDF upload, lease-checked job finish, chunk upsert, database row limits, job pruning."""
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.core.exceptions import AppError
from app.repositories.conversations import ConversationRepository
from app.repositories.documents import DocumentRepository
from app.repositories.jobs import JobQueue, JobReader
from app.services.maintenance import Maintenance
from app.workers.runner import IngestHandler, ThreadJobRunner
from tests.pro.conftest_helpers import A, World
from tests.pro.test_documents_api import api, env  # noqa: F401  (fixtures)


def test_eight_pdfs_at_once_are_all_accepted(api):  # noqa: F811
    docs = [api.upload(A, name=f"{i}.pdf") for i in range(8)]
    replies = [api.register(d, name=f"{i}.pdf") for i, d in enumerate(docs)]
    assert [r.status_code for r in replies] == [201] * 8
    assert [r.json()["status"] for r in replies] == ["queued"] * 5 + ["uploaded"] * 3
    assert api.c.post(f"/api/v1/documents/{docs[5]}/process").status_code == 429   # no free slot yet
    runner = ThreadJobRunner(JobQueue(api.w.db), IngestHandler(api.w.service))
    while runner.run_once():
        pass
    for d in docs[5:]:
        assert api.c.post(f"/api/v1/documents/{d}/process").status_code == 202
    while runner.run_once():
        pass
    items = api.c.get("/api/v1/documents?limit=100").json()["items"]
    assert len(items) == 8 and {i["status"] for i in items} == {"completed"}


def test_reusing_a_document_id_for_another_file_conflicts(api):  # noqa: F811
    doc = api.upload(A)
    assert api.register(doc).status_code == 201
    api.upload(A, doc, name="second.pdf")
    r = api.register(doc, name="second.pdf")
    assert r.status_code == 409 and r.json()["error"]["code"] == "conflict"
    assert [d["file_name"] for d in api.w.tables["documents"]] == ["b.pdf"]


def test_a_worker_that_lost_the_lease_cannot_finish_the_job():
    w = World()
    doc = w.add_doc(A)
    q = JobQueue(w.db)
    q.enqueue(doc, A)
    job = q.claim("stale", 10)
    w.db.now = 100
    assert q.claim("fresh", 10)["id"] == job["id"]
    assert q.finish(job["id"], "stale", False, "boom", 30) is False
    assert q.finish(job["id"], "stale", True) is False
    row = w.tables["processing_jobs"][0]
    assert row["status"] == "running" and row["locked_by"] == "fresh" and row["error_message"] is None
    assert q.finish(job["id"], "fresh", True) is True and row["status"] == "completed"


def test_replacing_chunks_keeps_ids_and_trims_the_tail():
    w = World()
    doc = str(uuid.uuid4())
    rows = [{"id": f"c{i}", "chunk_index": i, "page_number": 1} for i in range(4)]
    w.chunks.replace(doc, A, rows)
    before = {r["id"]: r["created_at"] for r in w.tables["document_chunks"]}
    w.chunks.replace(doc, A, [{**r, "page_number": 2} for r in rows[:2]], batch=1)
    kept = w.tables["document_chunks"]
    assert [r["id"] for r in kept] == ["c0", "c1"] and {r["page_number"] for r in kept} == {2}
    assert all(r["created_at"] == before[r["id"]] for r in kept)   # updated in place, not reinserted


class _FullDb:
    """A client whose inserts hit the database row limit."""

    def table(self, _name):
        return self

    def select(self, *_a):
        return self

    def eq(self, *_a):
        return self

    def limit(self, _n):
        return self

    def insert(self, _row):
        self.inserting = True
        return self

    def execute(self):
        if getattr(self, "inserting", False):
            raise Exception("{'code': 'P0001', 'message': 'quota_exceeded'}")
        return type("R", (), {"data": []})()


@pytest.mark.parametrize("create", [
    lambda: DocumentRepository(_FullDb()).create(A, str(uuid.uuid4()), "t", "b.pdf", 10),
    lambda: ConversationRepository(_FullDb()).create(A, "all_documents", None, None),
])
def test_database_row_limit_becomes_429(create):
    with pytest.raises(AppError) as e:
        create()
    assert e.value.code == "quota_exceeded" and e.value.status_code == 429


def test_a_pruning_failure_does_not_raise():
    class Broken:
        def prune(self, days):
            raise ConnectionError("db down")

    assert Maintenance(None, Broken(), None, None).prune_jobs(30) == 0
    w = World()
    assert Maintenance(w.docs, JobQueue(w.db), w.store, w.service).prune_jobs(30) == 0


def test_parallel_registrations_cannot_pass_the_active_job_cap(api, monkeypatch):  # noqa: F811
    real = JobReader.active_count

    def slow_count(self):
        n = real(self)
        time.sleep(0.05)  # widen the gap between counting and enqueueing
        return n

    monkeypatch.setattr(JobReader, "active_count", slow_count)
    docs = [api.upload(A, name=f"{i}.pdf") for i in range(8)]
    with ThreadPoolExecutor(8) as pool:
        replies = list(pool.map(lambda i: api.register(docs[i], name=f"{i}.pdf"), range(8)))
    assert [r.status_code for r in replies] == [201] * 8
    assert sorted(r.json()["status"] for r in replies) == ["queued"] * 5 + ["uploaded"] * 3
