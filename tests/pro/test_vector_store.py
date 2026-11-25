import uuid

import pytest

from app.core.exceptions import Conflict, NotFound, ServiceUnavailable
from app.repositories.documents import DocumentRepository
from app.services.embedding import EmbeddingService
from app.services.retrieval import RetrievalService, resolve_searchable_documents
from app.services.vector_store import ChromaVectorStore, ChunkRecord, chunk_id
from tests.pro.fakechroma import FakeChromaClient
from tests.pro.fakedb import FakeDB

A, B = str(uuid.uuid4()), str(uuid.uuid4())
MODEL, DIM = "fake-model", 4


class FakeModel:
    """Deterministic: texts mentioning a topic word point along that axis."""
    AXES = ["cat", "dog", "car", "sun"]

    def __init__(self):
        self.calls = []

    def get_sentence_embedding_dimension(self):
        return DIM

    def encode(self, texts, batch_size, normalize_embeddings, show_progress_bar):
        self.calls.append(list(texts))
        return [[1.0 if ax in t.lower() else 0.01 for ax in self.AXES] for t in texts]


def _embedder(model=None):
    m = model or FakeModel()
    return EmbeddingService(MODEL, DIM, batch_size=2, model_factory=lambda _n: m), m


def _store(client=None, model=MODEL, dim=DIM):
    return ChromaVectorStore("ignored", "col", model, dim, batch_size=2, client_factory=lambda _p: client)


def _chunks(emb, user, doc, texts, run="r1", title="Doc"):
    vecs = emb.embed_documents(texts)
    return [ChunkRecord(doc, user, title, i, i + 1, t, v, run) for i, (t, v) in enumerate(zip(texts, vecs))]


# ---------------------------------------------------------------- embedding service
def test_embedding_batches_and_dimension_guard():
    emb, model = _embedder()
    out = emb.embed_documents(["cat", "dog", "car", "sun", "cat dog"])
    assert len(out) == 5 and all(len(v) == DIM for v in out)
    assert [len(c) for c in model.calls] == [2, 2, 1]  # bounded batches
    assert not _embedder()[0].loaded
    bad = EmbeddingService(MODEL, 999, model_factory=lambda _n: FakeModel())
    with pytest.raises(ServiceUnavailable):
        bad.embed_query("x")


def test_e5_prefixes():
    m = FakeModel()
    e = EmbeddingService("intfloat/multilingual-e5-small", DIM, model_factory=lambda _n: m)
    e.embed_query("cat")
    e.embed_documents(["dog"])
    assert m.calls[0] == ["query: cat"] and m.calls[1] == ["passage: dog"]


# ---------------------------------------------------------------- store: write / idempotency / delete
def test_upsert_is_idempotent_and_ids_deterministic():
    emb, _ = _embedder()
    doc = str(uuid.uuid4())
    store = _store(FakeChromaClient())
    store.upsert_chunks(_chunks(emb, A, doc, ["cat a", "dog b", "car c"]))
    store.upsert_chunks(_chunks(emb, A, doc, ["cat a", "dog b", "car c"], run="r2"))
    assert store.count(doc, A) == 3
    assert chunk_id(doc, 1) == chunk_id(doc, 1) != chunk_id(doc, 2)


def test_delete_stale_keeps_new_run_only():
    emb, _ = _embedder()
    doc = str(uuid.uuid4())
    store = _store(FakeChromaClient())
    store.upsert_chunks(_chunks(emb, A, doc, ["cat", "dog", "car"], run="old"))
    store.upsert_chunks(_chunks(emb, A, doc, ["cat new", "dog new"], run="new"))  # ids 0,1 overwritten
    assert store.count(doc, A) == 3
    store.delete_stale(doc, A, "new")
    assert store.count(doc, A) == 2  # chunk 2 (old run) removed


def test_delete_document_is_scoped_to_owner():
    emb, _ = _embedder()
    doc = str(uuid.uuid4())
    store = _store(FakeChromaClient())
    store.upsert_chunks(_chunks(emb, A, doc, ["cat", "dog"]))
    store.delete_document(doc, B)  # wrong owner: no effect
    assert store.count(doc, A) == 2
    store.delete_document(doc, A)
    assert store.count(doc, A) == 0


def test_wrong_dimension_rejected():
    store = _store(FakeChromaClient())
    with pytest.raises(ValueError):
        store.upsert_chunks([ChunkRecord(str(uuid.uuid4()), A, "t", 0, 1, "x", [1.0], "r")])


# ---------------------------------------------------------------- store: isolation
def test_user_scoped_and_document_scoped_query():
    emb, _ = _embedder()
    da1, da2, db1 = (str(uuid.uuid4()) for _ in range(3))
    store = _store(FakeChromaClient())
    store.upsert_chunks(_chunks(emb, A, da1, ["cat in a tree"], title="A1"))
    store.upsert_chunks(_chunks(emb, A, da2, ["cat on a mat"], title="A2"))
    store.upsert_chunks(_chunks(emb, B, db1, ["cat secret of user B"], title="B1"))
    q = emb.embed_query("cat")
    both = store.query(q, A, [da1, da2], 10)
    assert {h.document_id for h in both} == {da1, da2}
    assert [h.document_id for h in store.query(q, A, [da1], 10)] == [da1]
    # forcing B's document id while authenticated as A returns nothing
    assert store.query(q, A, [db1], 10) == []
    assert store.query(q, A, [], 10) == []
    hit = both[0]
    assert hit.page_number == 1 and hit.document_title in ("A1", "A2") and 0 < hit.score <= 1


def test_post_filter_drops_foreign_chunks_even_if_filter_is_broken():
    emb, _ = _embedder()
    da, db = str(uuid.uuid4()), str(uuid.uuid4())
    client = FakeChromaClient()
    store = _store(client)
    store.upsert_chunks(_chunks(emb, A, da, ["cat mine"]))
    store.upsert_chunks(_chunks(emb, B, db, ["cat theirs"]))
    client.cols["col"].ignore_where_on_query = True  # simulate a tampered/ignored metadata filter
    hits = store.query(emb.embed_query("cat"), A, [da], 10)
    assert [h.document_id for h in hits] == [da]


# ---------------------------------------------------------------- store: model guard / health / reset
def test_embedding_model_change_requires_reindex_then_reset_recovers():
    emb, _ = _embedder()
    doc = str(uuid.uuid4())
    client = FakeChromaClient()
    _store(client).upsert_chunks(_chunks(emb, A, doc, ["cat"]))
    changed = _store(client, model="other-model")  # "restart" with a different EMBEDDING_MODEL
    with pytest.raises(ServiceUnavailable):
        changed.upsert_chunks(_chunks(emb, A, doc, ["cat"]))
    with pytest.raises(ServiceUnavailable):
        changed.query(emb.embed_query("cat"), A, [doc], 3)
    assert changed.health()["state"] == "needs_reindex"
    changed.reset_collection()
    assert changed.health()["state"] == "ready" and changed.health()["chunks"] == 0
    changed.upsert_chunks(_chunks(emb, A, doc, ["cat again"]))
    assert changed.count(doc, A) == 1


def test_dimension_change_also_requires_reindex():
    client = FakeChromaClient()
    _store(client).health()
    assert _store(client, dim=8).health()["state"] == "needs_reindex"


def test_open_failure_is_reported_not_leaked():
    def boom(_p):
        raise RuntimeError("disk exploded at /secret/path")
    store = ChromaVectorStore("p", "c", MODEL, DIM, client_factory=boom)
    with pytest.raises(ServiceUnavailable) as e:
        store.count(str(uuid.uuid4()), A)
    assert "secret" not in str(e.value)
    h = store.health()
    assert h["state"] == "error" and "secret" not in str(h)


# ---------------------------------------------------------------- retrieval + ownership via Postgres
def _doc_row(user, doc, status="completed", index_state="indexed", title="T"):
    return {"id": doc, "user_id": user, "title": title, "language": None, "status": status,
            "index_state": index_state, "created_at": "2026-01-01T00:00:00+00:00"}


def test_resolve_scope_and_search_end_to_end():
    emb, _ = _embedder()
    d1, d2, d3, dfail, db = (str(uuid.uuid4()) for _ in range(5))
    tables = {"documents": [_doc_row(A, d1), _doc_row(A, d2), _doc_row(A, d3, "processing", "none"),
                            _doc_row(A, dfail, "completed", "missing"), _doc_row(B, db)]}
    repo_a = DocumentRepository(FakeDB(tables, A))
    store = _store(FakeChromaClient())
    for user, doc, text in ((A, d1, "cat one"), (A, d2, "cat two"), (B, db, "cat B")):
        store.upsert_chunks(_chunks(emb, user, doc, [text]))
    svc = RetrievalService(emb, store)

    multi = resolve_searchable_documents(repo_a)
    assert {d["id"] for d in multi} == {d1, d2}  # processing / missing / foreign excluded
    assert {h.document_id for h in svc.search(A, multi, "cat", 5)} == {d1, d2}

    single = resolve_searchable_documents(repo_a, d1)
    assert [h.document_id for h in svc.search(A, single, "cat", 5)] == [d1]

    with pytest.raises(NotFound):
        resolve_searchable_documents(repo_a, db)  # B's document
    with pytest.raises(Conflict) as e:
        resolve_searchable_documents(repo_a, d3)
    assert e.value.code == "document_not_indexed"
    with pytest.raises(Conflict):
        resolve_searchable_documents(repo_a, dfail)
    assert svc.search(A, [], "cat", 5) == []


# ---------------------------------------------------------------- real chromadb (skipped when not installed)
def test_real_chroma_roundtrip(tmp_path):
    pytest.importorskip("chromadb")
    emb, _ = _embedder()
    doc = str(uuid.uuid4())
    store = ChromaVectorStore(str(tmp_path / "chroma"), "col", MODEL, DIM, batch_size=2)
    store.upsert_chunks(_chunks(emb, A, doc, ["cat one", "dog two", "car three"]))
    assert store.count(doc, A) == 3
    hits = store.query(emb.embed_query("cat"), A, [doc], 2)
    assert hits[0].text == "cat one" and store.query(emb.embed_query("cat"), B, [doc], 2) == []
    store.delete_document(doc, A)
    assert store.count(doc, A) == 0
    store.close()


def test_reset_fails_closed_when_old_collection_cannot_be_dropped():
    emb, _ = _embedder()
    doc = str(uuid.uuid4())
    client = FakeChromaClient()
    _store(client).upsert_chunks(_chunks(emb, A, doc, ["cat"]))
    changed = _store(client, model="other-model")
    client.fail_delete = True
    with pytest.raises(ServiceUnavailable):
        changed.reset_collection()
    with pytest.raises(ServiceUnavailable):  # still refuses to mix models
        changed.upsert_chunks(_chunks(emb, A, doc, ["cat"]))


def test_reset_on_fresh_store_and_reopen_after_close():
    emb, _ = _embedder()
    doc = str(uuid.uuid4())
    client = FakeChromaClient()
    store = _store(client)
    store.reset_collection()  # no existing collection: not an error
    store.upsert_chunks(_chunks(emb, A, doc, ["cat"]))
    store.close()
    assert store.health()["state"] == "ready" and store.count(doc, A) == 1  # reopened lazily


def test_embedding_raises_short_max_seq_length():
    class Short(FakeModel):
        max_seq_length = 128
    m = Short()
    EmbeddingService(MODEL, DIM, model_factory=lambda _n: m).embed_query("cat")
    assert m.max_seq_length == 512
