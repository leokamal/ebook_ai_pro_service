"""Phase 8: end-to-end cross-user isolation. User A's library is built through the real pipeline
(register -> queue -> worker -> Chroma), then user B attacks every endpoint with A's identifiers."""
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import documents as documents_module
from app.api.v1 import router
from app.core import dependencies as deps
from app.core.exceptions import install_error_handlers
from app.core.security import AuthUser
from app.repositories.jobs import JobQueue
from app.workers.runner import IngestHandler, ThreadJobRunner
from tests.pro.conftest_helpers import A, B, PDF, World
from tests.pro.fakedb import FakeDB
from tests.pro.test_rag import FakeLLM, Rig

SECRET_TITLE = "A-Secret-Title-7731"
SECRET_TEXT = "cat secret plans of user A"


class Full:
    def __init__(self, monkeypatch):
        self.w = World(pages=[SECRET_TEXT + " page one", "cat page two"])
        self.rig = Rig()
        self.rig.tables, self.rig.client, self.rig.store = self.w.tables, self.w.client, self.w.store
        self.rig.embedder = self.w.service.embedder
        for t in ("conversations", "messages", "message_sources"):
            self.w.tables.setdefault(t, [])
        self.llm = FakeLLM()
        self.user = A
        orig_embedder, orig_store = deps.get_embedding_service, deps.get_vector_store  # the keys routes depend on
        monkeypatch.setenv("SUPABASE_URL", "https://p.supabase.co")
        monkeypatch.setenv("SUPABASE_ANON_KEY", "anon")
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc")
        monkeypatch.setenv("CHAT_RATE_PER_MINUTE", "0")
        monkeypatch.setenv("SEARCH_RATE_PER_MINUTE", "0")
        monkeypatch.setattr(deps, "get_embedding_service", lambda: self.rig.embedder)
        monkeypatch.setattr(deps, "get_vector_store", lambda: self.w.store)
        self.deleted = []
        monkeypatch.setattr(documents_module, "_finish_delete", lambda d: self.deleted.append(d))
        app = FastAPI()
        install_error_handlers(app)
        app.include_router(router, prefix="/api/v1")
        o = app.dependency_overrides
        o[deps.get_current_user] = lambda: AuthUser(self.user, "tok")
        o[deps.get_user_db] = lambda: FakeDB(self.w.tables, self.user)
        o[deps.get_service_db] = lambda: self.w.db
        o[deps.get_storage] = lambda: self.w.storage
        o[deps.get_llm] = lambda: self.llm
        o[orig_embedder] = lambda: self.rig.embedder
        o[orig_store] = lambda: self.w.store
        self.c = TestClient(app)

    def work(self):
        """Run the worker until the queue is empty."""
        r = ThreadJobRunner(JobQueue(self.w.db), IngestHandler(self.w.service))
        while r.run_once():
            pass

    def upload_and_register(self, name="b.pdf", title=SECRET_TITLE):
        doc = str(uuid.uuid4())
        self.w.storage.put(self.user, doc, name, PDF)
        r = self.c.post("/api/v1/documents", json={"document_id": doc, "title": title, "file_name": name})
        assert r.status_code == 201, r.text
        return doc


@pytest.fixture
def full(monkeypatch):
    f = Full(monkeypatch)
    f.doc = f.upload_and_register()
    f.work()
    f.conv = f.c.post("/api/v1/conversations", json={"mode": "single_document", "document_id": f.doc}).json()["id"]
    f.llm.replies = ["The cat has secret plans [S1]."]
    assert f.c.post("/api/v1/chat", json={"conversation_id": f.conv, "message": "cat plans?"}).status_code == 200
    f.multi = f.c.post("/api/v1/conversations", json={"mode": "all_documents"}).json()["id"]
    return f


def _snapshot(f):
    return (f.w.store.count(f.doc, A), len(f.w.tables["documents"]), len(f.w.tables["messages"]),
            len(f.w.tables["document_chunks"]), len(f.w.tables["processing_jobs"]))


def test_alice_pipeline_really_produced_a_searchable_library(full):
    row = full.w.tables["documents"][0]
    assert row["status"] == "completed" and row["index_state"] == "indexed" and row["chunk_count"] > 0
    assert full.c.get(f"/api/v1/documents/{full.doc}/status").json()["status"] == "completed"
    hits = full.c.post("/api/v1/search", json={"query": "cat", "mode": "all_documents"}).json()["results"]
    assert hits and all(h["document_id"] == full.doc for h in hits)


def test_bob_cannot_reach_alices_resources_on_any_endpoint(full):
    before = _snapshot(full)
    full.user = B
    d, c = full.doc, full.conv
    attempts = [
        ("get", f"/api/v1/documents/{d}", None), ("get", f"/api/v1/documents/{d}/status", None),
        ("delete", f"/api/v1/documents/{d}", None), ("post", f"/api/v1/documents/{d}/process", None),
        ("post", f"/api/v1/documents/{d}/retry", None),
        ("get", f"/api/v1/conversations/{c}", None), ("patch", f"/api/v1/conversations/{c}", {"title": "pwn"}),
        ("delete", f"/api/v1/conversations/{c}", None), ("get", f"/api/v1/conversations/{c}/messages", None),
        ("post", "/api/v1/chat", {"conversation_id": c, "message": "cat plans?"}),
        ("post", "/api/v1/chat/stream", {"conversation_id": c, "message": "cat plans?"}),
        ("post", "/api/v1/search", {"query": "cat", "mode": "single_document", "document_id": d}),
        ("post", "/api/v1/conversations", {"mode": "single_document", "document_id": d}),
    ]
    for method, path, body in attempts:
        r = getattr(full.c, method)(path, **({"json": body} if body else {}))
        assert r.status_code == 404, (method, path, r.status_code, r.text)
        assert r.json()["error"]["code"] == "not_found"
        for secret in (SECRET_TITLE, SECRET_TEXT, "b.pdf", A):
            assert secret not in r.text, (method, path)
    assert _snapshot(full) == before                      # nothing of A's was changed, deleted or queued
    assert full.deleted == []
    assert full.c.get("/api/v1/documents").json()["items"] == []
    assert full.c.get("/api/v1/conversations").json()["items"] == []


def test_bobs_multi_search_and_chat_never_touch_alices_vectors(full):
    full.user = B
    assert full.c.post("/api/v1/search", json={"query": "cat secret", "mode": "all_documents"}).json() == {
        "results": [], "searched_documents": 0}
    cid = full.c.post("/api/v1/conversations", json={"mode": "all_documents"}).json()["id"]
    llm_calls = len(full.llm.calls)
    r = full.c.post("/api/v1/chat", json={"conversation_id": cid, "message": "cat secret plans?"})
    assert r.status_code == 200 and r.json()["sources"] == [] and SECRET_TEXT not in r.text
    assert len(full.llm.calls) == llm_calls               # empty library: the LLM was never even called

    # Bob now has his own document: results contain only Bob's chunks, and A's stay invisible
    bob_doc = full.upload_and_register("bob.pdf", "Bobs book")
    full.work()
    full.llm.replies = ["cat plans", "Bob [S1]."]  # condense (history exists), then the answer
    r = full.c.post("/api/v1/chat", json={"conversation_id": cid, "message": "cat plans?"})
    assert {s["document_id"] for s in r.json()["sources"]} == {bob_doc}
    sent = full.llm.calls[-1][1][-1]["content"]
    assert "Bobs book" in sent and SECRET_TITLE not in sent      # only Bob's own documents reach the prompt
    hits = full.c.post("/api/v1/search", json={"query": "cat", "mode": "all_documents"}).json()["results"]
    assert {h["document_id"] for h in hits} == {bob_doc}


def test_alice_is_unaffected_by_bobs_activity(full):
    before = _snapshot(full)
    full.user = B
    full.upload_and_register("bob.pdf", "Bobs book")
    full.work()
    full.user = A
    assert full.w.store.count(full.doc, A) == before[0]
    assert [d["title"] for d in full.c.get("/api/v1/documents").json()["items"]] == [SECRET_TITLE]
    msgs = full.c.get(f"/api/v1/conversations/{full.conv}/messages").json()["items"]
    assert len(msgs) == 2 and msgs[0]["sources"]


def test_bob_cannot_register_over_alices_document_id_or_path(full):
    full.user = B
    full.w.storage.put(B, full.doc, "b.pdf", PDF)         # Bob uploads under HIS folder but reuses A's id
    r = full.c.post("/api/v1/documents", json={"document_id": full.doc, "title": "x", "file_name": "b.pdf"})
    assert r.status_code == 409
    r = full.c.post("/api/v1/documents", json={"document_id": str(uuid.uuid4()), "title": "x",
                                              "file_name": f"../{A}/{full.doc}/b.pdf"})
    assert r.status_code == 422                            # path traversal in the file name
    assert full.w.tables["documents"][0]["user_id"] == A


def test_forged_job_for_another_users_document_is_refused_by_the_worker(full):
    """Defense in depth: even a mis-routed job row cannot make the worker act on someone else's document."""
    from app.core.exceptions import NotFound
    with pytest.raises(NotFound):
        full.w.service.process({"id": "j", "user_id": B, "document_id": full.doc})
    assert full.w.store.count(full.doc, A) > 0 and full.w.store.count(full.doc, B) == 0


# ---------------------------------------------------------------- authentication matrix
def test_every_user_route_requires_authentication_and_admin_routes_the_token(monkeypatch):
    import main
    monkeypatch.setenv("ADMIN_TOKEN", "admintoken")
    c = TestClient(main.app, raise_server_exceptions=False)
    uid = str(uuid.uuid4())
    spec = c.get("/openapi.json").json()
    checked = 0
    for path, ops in spec["paths"].items():
        if not path.startswith("/api/v1") or path == "/api/v1/health":
            continue
        url = path.replace("{document_id}", uid).replace("{conversation_id}", uid)
        for method in ops:
            kw = {"json": {}} if method in ("post", "patch") else {}
            r = getattr(c, method)(url, **kw)
            if path.startswith("/api/v1/admin"):
                assert r.status_code in (403, 503), (method, path, r.status_code)
            else:
                assert r.status_code == 401, (method, path, r.status_code)
                assert r.json()["error"]["code"] == "unauthorized" and r.headers["www-authenticate"] == "Bearer"
            checked += 1
    assert checked >= 17
    r = c.get("/api/v1/documents", headers={"Authorization": "Bearer not.a.jwt"})
    assert r.status_code == 401 and "not.a.jwt" not in r.text
