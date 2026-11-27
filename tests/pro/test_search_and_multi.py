
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import router
from app.core import dependencies as deps
from app.core.exceptions import install_error_handlers
from app.core.security import AuthUser
from app.services.rag_parts import ContextBuilder
from app.services.vector_store import SearchHit
from tests.pro.fakedb import FakeDB
from tests.pro.test_rag import A, B, FakeLLM, Rig


class SearchApi:
    def __init__(self, monkeypatch):
        self.rig = Rig()
        self.user = A
        monkeypatch.setenv("SUPABASE_URL", "https://p.supabase.co")
        monkeypatch.setenv("SUPABASE_ANON_KEY", "anon")
        app = FastAPI()
        install_error_handlers(app)
        app.include_router(router, prefix="/api/v1")
        app.dependency_overrides[deps.get_current_user] = lambda: AuthUser(self.user, "tok")
        app.dependency_overrides[deps.get_user_db] = lambda: FakeDB(self.rig.tables, self.user)
        app.dependency_overrides[deps.get_embedding_service] = lambda: self.rig.embedder
        app.dependency_overrides[deps.get_vector_store] = lambda: self.rig.store
        self.c = TestClient(app)

    def search(self, **body):
        body.setdefault("query", "cat")
        return self.c.post("/api/v1/search", json=body)


@pytest.fixture
def api(monkeypatch):
    return SearchApi(monkeypatch)


# ---------------------------------------------------------------- /search
def test_search_all_documents_returns_only_own_ready_documents(api):
    d1 = api.rig.add_doc(A, "Cats", [(2, "cat one"), (5, "cat two")])
    d2 = api.rig.add_doc(A, "Dogs", [(9, "dog and cat together")])
    api.rig.add_doc(A, "Draft", [(1, "cat draft")], status="processing", index_state="none")
    api.rig.add_doc(B, "Secret", [(1, "cat secret of B")])
    r = api.search(mode="all_documents")
    body = r.json()
    assert r.status_code == 200 and body["searched_documents"] == 2
    assert {x["document_id"] for x in body["results"]} == {d1, d2}
    assert all("secret of B" not in x["text"] and "draft" not in x["text"] for x in body["results"])
    assert body["results"] == sorted(body["results"], key=lambda x: -x["score"])
    first = body["results"][0]
    assert {"chunk_id", "document_title", "page_number", "score", "text"} <= set(first)


def test_search_single_document_is_scoped_and_validated(api):
    d1 = api.rig.add_doc(A, "Cats", [(2, "cat one")])
    api.rig.add_doc(A, "Dogs", [(9, "dog and cat together")])
    r = api.search(mode="single_document", document_id=d1)
    assert [x["document_id"] for x in r.json()["results"]] == [d1] and r.json()["searched_documents"] == 1
    assert api.search(mode="single_document").status_code == 422                  # document_id required
    assert api.search(mode="all_documents", document_id=d1).status_code == 422    # forbidden
    assert api.search(mode="all_documents", user_id=B).status_code == 422         # unknown field
    assert api.search(mode="all_documents", top_k=0).status_code == 422
    assert api.search(mode="all_documents", top_k=21).status_code == 422
    assert api.search(mode="all_documents", query="").status_code == 422


def test_search_cannot_reach_another_users_document(api):
    db = api.rig.add_doc(B, "Secret", [(1, "cat secret of B")])
    r = api.search(mode="single_document", document_id=db)                        # A forces B's document id
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    assert "secret" not in r.text
    assert api.search(mode="all_documents").json() == {"results": [], "searched_documents": 0}


def test_search_unindexed_document_is_409_and_empty_library_is_empty(api):
    d = api.rig.add_doc(A, "Draft", [(1, "cat")], status="processing", index_state="none")
    r = api.search(mode="single_document", document_id=d)
    assert r.status_code == 409 and r.json()["error"]["code"] == "document_not_indexed"
    assert api.search(mode="all_documents").json()["results"] == []


def test_search_per_document_cap_keeps_results_diverse(api):
    big = api.rig.add_doc(A, "Big", [(i, f"cat fact number {i} " + "word" * i) for i in range(1, 9)])
    small = api.rig.add_doc(A, "Small", [(1, "a cat in the other book")])
    ids = [x["document_id"] for x in api.search(mode="all_documents", top_k=6).json()["results"]]
    assert ids.count(big) <= 3 and small in ids


def test_search_requires_auth_and_is_rate_limited(api, monkeypatch):
    api.rig.add_doc(A, "Cats", [(1, "cat")])
    monkeypatch.setenv("SEARCH_RATE_PER_MINUTE", "2")
    assert api.search(mode="all_documents").status_code == 200
    assert api.search(mode="all_documents").status_code == 200
    r = api.search(mode="all_documents")
    assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limited"
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(router, prefix="/api/v1")
    assert TestClient(app).post("/api/v1/search", json={"query": "x", "mode": "all_documents"}).status_code == 401


# ---------------------------------------------------------------- multi-document context + citations
def _hit(i, doc, score, idx, text="t"):
    return SearchHit(f"c{i}", doc, f"Doc {doc}", idx + 1, idx, score, f"{text} {i}")


def test_context_is_grouped_by_document_in_reading_order_and_labels_follow():
    hits = [_hit(1, "a", 0.9, 5), _hit(2, "b", 0.8, 1), _hit(3, "a", 0.7, 2), _hit(4, "b", 0.6, 0)]
    block, used = ContextBuilder().build(hits, group=True)
    assert [h.chunk_id for h in used] == ["c3", "c1", "c4", "c2"]       # doc a (best 0.9) then b; chunk order inside
    assert block.index('[S1] (document: "Doc a"') < block.index('[S3] (document: "Doc b"')
    flat, used_flat = ContextBuilder().build(hits)
    assert [h.chunk_id for h in used_flat] == ["c1", "c2", "c3", "c4"]  # single-document keeps score order


def test_budget_keeps_the_best_chunks_before_grouping():
    hits = [_hit(1, "a", 0.9, 0, "x" * 800), _hit(2, "b", 0.8, 0, "y" * 800), _hit(3, "c", 0.1, 0, "z" * 800)]
    _, used = ContextBuilder(max_chars=1900).build(hits, group=True)
    assert [h.chunk_id for h in used] == ["c1", "c2"]


def test_multi_rag_cross_document_citations_map_to_the_right_documents():
    rig = Rig()
    d1 = rig.add_doc(A, "Cats", [(3, "cat in book one")])
    d2 = rig.add_doc(A, "Dogs", [(8, "cat in book two")])
    rig.add_doc(B, "Secret", [(1, "cat in book three")])
    c = rig.conv(A, mode="all_documents")
    llm = FakeLLM("Book one says X [S1]. Book two says Y [S2].")
    r = rig.orch(A, llm).answer(c, "cat?")
    block = llm.calls[0][1][-1]["content"]
    assert block.count("[S1]") == 1 and "book three" not in block
    by_doc = {s.document_id: s for s in r.sources}
    assert set(by_doc) == {d1, d2} and by_doc[d1].page_number == 3 and by_doc[d2].page_number == 8
    label_doc = {"S1": block.split("[S1] (document: ")[1].split('"')[1], "S2": block.split("[S2] (document: ")[1].split('"')[1]}
    assert {by_doc[d1].document_title, by_doc[d2].document_title} == set(label_doc.values())
    rows = rig.tables["message_sources"]
    assert {r["document_id"] for r in rows} == {d1, d2} and all(r["chunk_id"] for r in rows)


def test_multi_rag_large_library_stays_within_context_budget():
    rig = Rig()
    for n in range(12):
        rig.add_doc(A, f"Book{n}", [(p, f"cat paragraph {n}-{p} " + "filler " * 60) for p in range(1, 5)])
    c = rig.conv(A, mode="all_documents")
    llm = FakeLLM("ok [S1]")
    rig.orch(A, llm, rag_context_chunks=6).answer(c, "cat?")
    block = llm.calls[0][1][-1]["content"]
    assert block.count("\n[S") + 1 <= 6 or block.count("[S") <= 7
    titles = [seg.split('"')[1] for seg in block.split("(document: ")[1:]]
    assert max(titles.count(t) for t in set(titles)) <= 3                      # per-document cap
