"""One file and one set of vectors, several holders (offline). The SQL side is in integration/test_rls_sql.py."""
import uuid

import pytest

from app.repositories.account import AccountRepository
from app.repositories.documents import DocumentRepository
from app.repositories.jobs import JobQueue
from app.schemas.documents import DocumentOut
from app.services.account import AccountService
from app.services.maintenance import Maintenance
from app.services.retrieval import RetrievalService, resolve_searchable_documents
from app.services.vector_store import chunk_id
from app.workers.runner import IngestHandler, ThreadJobRunner
from tests.pro.conftest_helpers import A, B, DIM, MODEL, World
from tests.pro.fakedb import FakeDB

C = str(uuid.uuid4())


def _run(w, user, doc):
    JobQueue(w.db).enqueue(doc, user)
    assert ThreadJobRunner(JobQueue(w.db), IngestHandler(w.service)).run_once()


def _row(w, doc):
    return next((d for d in w.tables["documents"] if d["id"] == doc), None)


def _accept(w, source, user, title="My copy"):
    """What `accept_shared_document` does in SQL."""
    new = str(uuid.uuid4())
    w.tables["documents"].append({**_row(w, source), "id": new, "user_id": user, "title": title})
    w.tables["document_chunks"] += [
        {**c, "id": chunk_id(new, c["chunk_index"]), "document_id": new, "user_id": user}
        for c in list(w.tables["document_chunks"]) if c["document_id"] == source]
    return new


def _shared():
    w = World()
    source = w.add_doc(A)
    _run(w, A, source)
    return w, source, _accept(w, source, B)


def _search(w, user, query="cat"):
    docs = resolve_searchable_documents(DocumentRepository(FakeDB(w.tables, user)))
    from app.services.embedding import HuggingFaceEmbedder
    embedder = HuggingFaceEmbedder(MODEL, DIM, model_factory=lambda _n: w.model)
    return RetrievalService(embedder, w.store).search(user, docs, query, 10)


def _vectors(w):
    return len(w.client.cols["col"].rows)


def _delete(w, user, doc):
    _row(w, doc)["status"] = "deleting"
    w.service.delete_document({"id": doc, "user_id": user, "file_path": ""})


def _account(w):
    return AccountService(AccountRepository(w.db), w.store, w.storage, "user-documents", lambda _u: None)


def _drop_account(w, user):
    _account(w).delete(user)
    for table in ("documents", "document_chunks"):  # the auth user's `on delete cascade`
        w.tables[table] = [r for r in w.tables[table] if r["user_id"] != user]


def test_recipient_searches_the_same_vectors_under_their_own_ids():
    w, source, copy = _shared()
    before = _vectors(w)
    hits = _search(w, B)
    assert hits and {h.document_id for h in hits} == {copy} and {h.document_title for h in hits} == {"My copy"}
    mine = {c["id"] for c in w.tables["document_chunks"] if c["document_id"] == copy}
    assert {h.chunk_id for h in hits} <= mine                      # citations satisfy the (chunk, document) FK
    assert {h.document_id for h in _search(w, A)} == {source}
    assert _vectors(w) == before and _search(w, C) == [] and len(w.storage.files) == 1


def test_foreign_chunks_are_dropped_even_if_the_filter_is_broken():
    w, _, _ = _shared()
    other = w.add_doc(C)
    _run(w, C, other)
    w.client.cols["col"].ignore_where_on_query = True
    assert all(h.document_id != other for h in _search(w, B))


@pytest.mark.parametrize("first", ["uploader", "recipient"])
def test_content_survives_until_the_last_holder_deletes(first):
    w, source, copy = _shared()
    total = _vectors(w)
    order = [(A, source), (B, copy)] if first == "uploader" else [(B, copy), (A, source)]
    (u1, d1), (u2, d2) = order
    _delete(w, u1, d1)
    assert _row(w, d1) is None and _vectors(w) == total and w.storage.removed == [] and _search(w, u2)
    _delete(w, u2, d2)
    assert _vectors(w) == 0 and w.storage.files == {} and w.tables["documents"] == []


def test_uploader_account_deletion_keeps_what_others_hold():
    w, _, _ = _shared()
    private = w.add_doc(A, name="private.pdf")
    _run(w, A, private)
    shared_vectors = _vectors(w) // 2
    _drop_account(w, A)
    assert _vectors(w) == shared_vectors and len(w.storage.files) == 1 and _search(w, B)
    _drop_account(w, B)                                            # the last holder takes the content with them
    assert _vectors(w) == 0 and w.storage.files == {}


def test_recipient_account_deletion_leaves_the_uploader_alone():
    w, _, _ = _shared()
    total = _vectors(w)
    _drop_account(w, B)
    assert _vectors(w) == total and len(w.storage.files) == 1 and _search(w, A)


@pytest.mark.parametrize("uploader_gone", [False, True])
def test_reindex_embeds_once_and_updates_every_holder(uploader_gone):
    w, source, copy = _shared()
    if uploader_gone:
        _delete(w, A, source)
        third = _accept(w, copy, C)
        holders = [(B, copy), (C, third)]
    else:
        holders = [(A, source), (B, copy)]
    total = _vectors(w)
    m = Maintenance(w.docs, JobQueue(w.db), w.store, w.service)
    assert m.reindex_all() == {"reindex_queued": 1}
    runner = ThreadJobRunner(JobQueue(w.db), IngestHandler(w.service))
    assert runner.run_once() and not runner.run_once()
    for user, doc in holders:
        assert (_row(w, doc)["status"], _row(w, doc)["index_state"]) == ("completed", "indexed")
        assert {h.document_id for h in _search(w, user)} == {doc}
    assert _vectors(w) == total
    assert m.reconcile() == {"checked": 1, "reindex_queued": 0}


def test_api_marks_shared_copies_without_exposing_content_ids():
    w, source, copy = _shared()
    assert DocumentOut.model_validate(_row(w, source)).shared is False
    out = DocumentOut.model_validate(_row(w, copy)).model_dump()
    assert out["shared"] is True and "content_id" not in out and "content_owner_id" not in out


def test_an_id_still_used_by_others_cannot_be_registered_again():
    w, source, _ = _shared()
    _delete(w, A, source)
    with pytest.raises(Exception, match="already in use"):
        DocumentRepository(FakeDB(w.tables, C)).create(C, source, "T", "b.pdf", 10)


def _rebuild_queued():
    """Both holders stale, one job queued; returns (world, maintenance, job holder, waiting holder)."""
    w, source, copy = _shared()
    m = Maintenance(w.docs, JobQueue(w.db), w.store, w.service)
    assert m.reindex_all() == {"reindex_queued": 1}
    holder = next(j["document_id"] for j in w.tables["processing_jobs"] if j["status"] == "queued")
    other = copy if holder == source else source
    return w, m, holder, other


def test_deleting_the_holder_that_carries_the_rebuild_queues_another():
    w, m, holder, other = _rebuild_queued()
    assert m.resume_rebuilds() == 0                                # a job is already on it
    _delete(w, _row(w, holder)["user_id"], holder)
    w.tables["processing_jobs"] = [j for j in w.tables["processing_jobs"] if j["document_id"] != holder]  # cascade
    assert m.resume_rebuilds() == 1
    assert ThreadJobRunner(JobQueue(w.db), IngestHandler(w.service)).run_once()
    assert _row(w, other)["index_state"] == "indexed"


def test_a_failed_rebuild_reaches_every_waiting_holder_once():
    w, m, holder, other = _rebuild_queued()
    queue = JobQueue(w.db)
    job = queue.claim("w", 300)
    assert queue.finish(job["id"], "w", False, "No text", None, "no_extractable_text")
    jobs = len(w.tables["processing_jobs"])
    assert m.resume_rebuilds() == 0 and len(w.tables["processing_jobs"]) == jobs   # no second embedding run
    assert (_row(w, other)["status"], _row(w, other)["error_code"]) == ("failed", "no_extractable_text")
    _run(w, _row(w, other)["user_id"], other)                      # either holder's retry restores both
    for doc in (holder, other):
        assert (_row(w, doc)["status"], _row(w, doc)["index_state"], _row(w, doc)["error_code"]) == (
            "completed", "indexed", None)


def test_holder_lists_are_read_in_pages():
    from app.repositories.base import fetch_all
    rows = [{"id": str(i)} for i in range(7)]
    db = FakeDB({"t": rows})
    assert len(fetch_all(lambda: db.table("t").select("id").order("id"), page=3)) == 7
