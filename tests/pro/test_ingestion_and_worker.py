import os
import tempfile

import pytest

from app.core.exceptions import Conflict, NotFound
from app.repositories.jobs import JobQueue
from app.services.ingestion import LeaseLost
from app.services.maintenance import Maintenance
from app.services.pdf_extraction import IngestError, normalize_text
from app.workers.runner import IngestHandler, ThreadJobRunner
from tests.pro.conftest_helpers import A, B, FakeModel, World


def _job(w, doc, user=A):
    """Enqueue + claim through the (fake) RPCs, exactly like the worker does."""
    q = JobQueue(w.db)
    q.enqueue(doc, user)
    return q.claim("w1", 300)


def _chroma_rows(w):
    return w.client.cols["col"].rows


@pytest.fixture(autouse=True)
def _tmp_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------- text normalization
def test_normalize_text():
    assert normalize_text("a\x00b    c\n\n\n\nd") == "ab c\n\nd"
    assert normalize_text("inter-\nnational") == "international"      # Latin de-hyphenation
    assert normalize_text("كتاب-\nجديد") == "كتاب-\nجديد"            # Arabic untouched
    assert normalize_text("ﬁx") == "fix"                         # NFKC ligature


# ---------------------------------------------------------------- pipeline
def test_happy_path_pages_chunks_and_cleanup(_tmp_dir):
    w = World()
    doc = w.add_doc()
    w.service.process(_job(w, doc))
    row = w.tables["documents"][0]
    assert row["status"] == "completed" and row["index_state"] == "indexed" and row["page_count"] == 2
    rows = _chroma_rows(w)
    assert row["chunk_count"] == len(rows) == len(w.tables["document_chunks"]) > 2
    pages = {m["page_number"] for _, _, m in rows.values()}
    assert pages == {1, 2}
    assert {r["id"] for r in w.tables["document_chunks"]} == set(rows)  # PG ids == Chroma ids
    assert all(m["user_id"] == A and m["document_id"] == doc for _, _, m in rows.values())
    assert not [p for p in os.listdir(_tmp_dir) if p.startswith("ingest_")]  # temp dir removed


def test_rerun_is_idempotent_and_drops_stale_chunks():
    w = World()
    doc = w.add_doc()
    w.service.process(_job(w, doc))
    first = set(_chroma_rows(w))
    w.tables["processing_jobs"][0]["status"] = "completed"
    w.service = w.make_service(pages=["cat short."])  # new run, fewer chunks
    w.service.process(_job(w, doc))
    assert len(_chroma_rows(w)) == 1 and set(_chroma_rows(w)) <= first
    assert w.tables["documents"][0]["chunk_count"] == 1 == len(w.tables["document_chunks"])


def test_failed_rerun_keeps_previous_index():
    w = World()
    doc = w.add_doc()
    w.service.process(_job(w, doc))
    before = set(_chroma_rows(w))
    w.tables["processing_jobs"][0]["status"] = "completed"
    flaky = w.make_service(model=FakeModel(fail_after=1))
    with pytest.raises(ConnectionError):
        flaky.process(_job(w, doc))
    assert set(_chroma_rows(w)) >= before and w.tables["documents"][0]["index_state"] == "indexed"


@pytest.mark.parametrize("mutate, code", [
    (lambda r: r.update(file_size=999), "size_mismatch"),
    (lambda r: r.update(file_path=r["file_path"].replace("/b.pdf", "/../b.pdf")), "invalid_path"),
    (lambda r: r.update(file_path="other/" + r["file_path"]), "invalid_path"),
])
def test_integrity_checks(mutate, code):
    w = World()
    doc = w.add_doc()
    mutate(w.tables["documents"][0])
    with pytest.raises(IngestError) as e:
        w.service.process(_job(w, doc))
    assert e.value.code == code


def test_not_a_pdf_and_empty_text_and_corrupt():
    w = World()
    doc = w.add_doc()
    w.storage.files[f"{A}/{doc}/b.pdf"] = b"NOPE" + b"x" * 10
    w.tables["documents"][0]["file_size"] = 14
    with pytest.raises(IngestError) as e:
        w.service.process(_job(w, doc))
    assert e.value.code == "corrupt_pdf"

    w2 = World(pages=["", "   "])
    d2 = w2.add_doc()
    with pytest.raises(IngestError) as e:
        w2.service.process(_job(w2, d2))
    assert e.value.code == "no_extractable_text" and not w2.client.cols

    w3 = World()
    d3 = w3.add_doc()

    def boom(path, max_pages):
        raise IngestError("corrupt_pdf", "The PDF is corrupted or unreadable")
    w3.service._open_pdf = boom
    with pytest.raises(IngestError):
        w3.service.process(_job(w3, d3))


def test_job_for_foreign_document_is_refused():
    w = World()
    doc = w.add_doc(A)
    job = {"id": "j", "user_id": B, "document_id": doc}  # a forged/misrouted job
    with pytest.raises(NotFound):
        w.service.process(job)
    assert not w.client.cols  # nothing was even opened


def test_lease_lost_and_deleted_while_processing():
    w = World()
    doc = w.add_doc()
    with pytest.raises(LeaseLost):
        w.service.process(_job(w, doc), heartbeat=lambda pct: False)
    assert w.tables["documents"][0]["status"] == "processing"  # untouched, not "completed"

    w2 = World()
    d2 = w2.add_doc()
    job = _job(w2, d2)

    def hb(pct):
        w2.tables["documents"][0]["status"] = "deleting"
        return True
    w2.service.process(job, heartbeat=hb)
    assert w2.tables["documents"][0]["status"] == "deleting" and not _chroma_rows(w2)


def test_delete_document_removes_everything():
    w = World()
    doc = w.add_doc()
    w.service.process(_job(w, doc))
    w.tables["documents"][0]["status"] = "deleting"
    w.service.delete_document({"id": doc, "user_id": A, "file_path": f"{A}/{doc}/b.pdf"})
    assert not _chroma_rows(w) and not w.tables["documents"] and w.storage.removed == [f"{A}/{doc}/b.pdf"]


# ---------------------------------------------------------------- runner: retry, failure, lease takeover
def _runner(w, service=None):
    return ThreadJobRunner(JobQueue(w.db), IngestHandler(service or w.service))


def test_runner_success_marks_job_and_document():
    w = World()
    doc = w.add_doc()
    JobQueue(w.db).enqueue(doc, A)
    r = _runner(w)
    assert r.run_once() and not r.run_once()
    assert w.tables["processing_jobs"][0]["status"] == "completed"
    assert w.tables["documents"][0]["status"] == "completed"


def test_runner_permanent_failure_is_not_retried_and_message_is_user_safe():
    w = World(pages=["", ""])
    doc = w.add_doc()
    JobQueue(w.db).enqueue(doc, A)
    r = _runner(w)
    r.run_once()
    job, row = w.tables["processing_jobs"][0], w.tables["documents"][0]
    assert job["status"] == "failed" and job["attempts"] == 1
    assert row["status"] == "failed" and "scanned" in row["error_message"]


def test_runner_transient_failure_retries_with_backoff_then_fails():
    w = World()
    doc = w.add_doc()
    w.storage.fail_download = True
    JobQueue(w.db).enqueue(doc, A)
    r = _runner(w)
    r.run_once()
    job = w.tables["processing_jobs"][0]
    assert job["status"] == "queued" and job["run_after"] == 30 and job["error_message"] == "Temporary processing error"
    assert not r.run_once()                      # backoff not elapsed
    w.db.now = 31
    assert r.run_once() and job["attempts"] == 2 and job["run_after"] == 31 + 60
    w.db.now = 200
    r.run_once()                                 # third attempt exhausts max_attempts
    assert job["status"] == "failed" and w.tables["documents"][0]["status"] == "failed"
    assert "details" not in w.tables["documents"][0]["error_message"].lower()


def test_lease_expiry_lets_another_worker_take_over_and_duplicate_enqueue_conflicts():
    w = World()
    doc = w.add_doc()
    q = JobQueue(w.db)
    q.enqueue(doc, A)
    with pytest.raises(Conflict):
        q.enqueue(doc, A)                         # duplicate prevention
    first = q.claim("w1", 300)
    assert q.claim("w2", 300) is None             # leased
    w.db.now = 301                                # worker 1 died / free tier slept
    second = q.claim("w2", 300)
    assert second["id"] == first["id"] and second["attempts"] == 2
    assert not q.heartbeat(first["id"], "w1", 10)  # the old owner lost the lease


# ---------------------------------------------------------------- maintenance: reconcile / reindex / janitor
def test_reconcile_detects_lost_chroma_and_reindex_recovers():
    w = World()
    doc = w.add_doc()
    w.service.process(_job(w, doc))
    w.tables["processing_jobs"][0]["status"] = "completed"
    m = Maintenance(w.docs, JobQueue(w.db), w.store, w.service)
    assert m.reconcile() == {"checked": 1, "reindex_queued": 0}

    w.client.cols.clear()                          # Chroma storage lost ...
    w.store.close()                                # ... and the service restarted
    assert m.reconcile() == {"checked": 1, "reindex_queued": 1}
    row = w.tables["documents"][0]
    assert row["index_state"] == "missing" and row["status"] == "queued"

    _runner(w).run_once()                          # worker rebuilds from the stored PDF
    assert row["status"] == "completed" and row["index_state"] == "indexed"
    assert w.store.count(doc, A) == row["chunk_count"] > 0
    assert m.reconcile()["reindex_queued"] == 0


def test_reindex_all_after_model_change():
    w = World()
    doc = w.add_doc()
    w.service.process(_job(w, doc))
    w.tables["processing_jobs"][0]["status"] = "completed"
    from app.services.vector_store import ChromaVectorStore
    from tests.pro.conftest_helpers import DIM
    w.store = ChromaVectorStore("p", "col", "new-model", DIM, client_factory=lambda _p: w.client)
    w.service = w.make_service()
    w.service.store = w.store
    assert w.store.health()["state"] == "needs_reindex"
    m = Maintenance(w.docs, JobQueue(w.db), w.store, w.service)
    assert m.reindex_all() == {"reindex_queued": 1}
    assert w.tables["documents"][0]["index_state"] == "stale"
    _runner(w, w.service).run_once()
    assert w.tables["documents"][0]["index_state"] == "indexed" and w.store.health()["state"] == "ready"


def test_janitor_finishes_interrupted_deletions():
    w = World()
    doc = w.add_doc()
    w.service.process(_job(w, doc))
    w.tables["documents"][0]["status"] = "deleting"
    m = Maintenance(w.docs, JobQueue(w.db), w.store, w.service)
    assert m.janitor() == 1 and not w.tables["documents"] and not _chroma_rows(w)


# ---------------------------------------------------------------- review fixes: races, lease, reindex ordering
def test_delete_between_check_and_commit_is_not_overwritten():
    """DELETE lands after the commit-phase read: the compare-and-set must refuse to resurrect the document."""
    w = World()
    doc = w.add_doc()
    job = _job(w, doc)
    original = w.chunks.replace

    def delete_then_replace(*a, **k):
        w.tables["documents"][0]["status"] = "deleting"  # concurrent DELETE
        return original(*a, **k)
    w.chunks.replace = delete_then_replace
    w.service.process(job)  # NotFound from the CAS is handled: vectors dropped, no retry
    assert w.tables["documents"][0]["status"] == "deleting"
    assert not _chroma_rows(w)


def test_runner_does_not_retry_when_document_vanished():
    w = World()
    doc = w.add_doc()
    JobQueue(w.db).enqueue(doc, A)
    w.tables["documents"].clear()
    r = _runner(w)
    r.run_once()
    assert w.tables["processing_jobs"][0]["status"] == "failed"


def test_commit_phase_checks_the_lease():
    w = World()
    doc = w.add_doc()
    calls = []

    def hb(pct):
        calls.append(pct)
        return pct != 95  # lease is lost exactly at the commit check
    with pytest.raises(LeaseLost):
        w.service.process(_job(w, doc), heartbeat=hb)
    assert 95 in calls and w.tables["documents"][0]["status"] == "processing" and not w.tables.get("document_chunks")


def test_reindex_refused_while_jobs_run_and_flags_before_reset():
    w = World()
    doc = w.add_doc()
    w.service.process(_job(w, doc))
    w.tables["processing_jobs"][0]["status"] = "completed"
    w.add_doc()
    JobQueue(w.db).enqueue(w.tables["documents"][1]["id"], A)
    JobQueue(w.db).claim("w", 300)  # a job is running
    m = Maintenance(w.docs, JobQueue(w.db), w.store, w.service)
    with pytest.raises(Conflict):
        m.reindex_all()
    assert w.tables["documents"][0]["index_state"] == "indexed"  # nothing touched

    w.tables["processing_jobs"][1]["status"] = "completed"
    order = []
    orig_reset = w.store.reset_collection
    w.store.reset_collection = lambda: (order.append(w.tables["documents"][0]["index_state"]), orig_reset())
    m.reindex_all()
    assert order == ["stale"]  # documents were flagged BEFORE the collection was dropped


def test_reconcile_requeues_stale_documents_a_crashed_reindex_skipped():
    w = World()
    doc = w.add_doc()
    w.service.process(_job(w, doc))
    w.tables["processing_jobs"][0]["status"] = "completed"
    w.tables["documents"][0].update(index_state="stale", status="completed")
    m = Maintenance(w.docs, JobQueue(w.db), w.store, w.service)
    assert m.reconcile()["reindex_queued"] == 1
