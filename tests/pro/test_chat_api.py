import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import router
from app.core import dependencies as deps
from app.core.exceptions import install_error_handlers
from app.core.security import AuthUser
from app.services.llm import LLMQuotaError
from tests.pro.fakedb import FakeDB
from tests.pro.test_rag import FakeLLM, Rig, A, B


class ChatApi:
    def __init__(self, monkeypatch):
        self.rig = Rig()
        self.user = A
        self.llm = FakeLLM()
        monkeypatch.setenv("SUPABASE_URL", "https://p.supabase.co")
        monkeypatch.setenv("SUPABASE_ANON_KEY", "anon")
        monkeypatch.setattr(deps, "get_embedding_service", lambda: self.rig.embedder)
        monkeypatch.setattr(deps, "get_vector_store", lambda: self.rig.store)
        app = FastAPI()
        install_error_handlers(app)
        app.include_router(router, prefix="/api/v1")
        app.dependency_overrides[deps.get_current_user] = lambda: AuthUser(self.user, "tok")
        app.dependency_overrides[deps.get_user_db] = lambda: FakeDB(self.rig.tables, self.user)
        app.dependency_overrides[deps.get_service_db] = lambda: FakeDB(self.rig.tables)
        app.dependency_overrides[deps.get_llm] = lambda: self.llm
        self.c = TestClient(app)

    def create_conv(self, **body):
        return self.c.post("/api/v1/conversations", json=body)


@pytest.fixture
def api(monkeypatch):
    return ChatApi(monkeypatch)


def test_conversation_create_validation_and_isolation(api):
    d = api.rig.add_doc(A, "Pets", [(1, "cat facts")])
    assert api.create_conv(mode="single_document").status_code == 422          # document_id required
    assert api.create_conv(mode="all_documents", document_id=d).status_code == 422
    assert api.create_conv(mode="all_documents", user_id=B).status_code == 422  # unknown field rejected
    assert api.create_conv(mode="single_document", document_id=str(uuid.uuid4())).status_code == 404
    r = api.create_conv(mode="single_document", document_id=d, title="Mine")
    assert r.status_code == 201
    cid = r.json()["id"]

    api.user = B
    assert api.create_conv(mode="single_document", document_id=d).status_code == 404   # A's document
    for method, path in (("get", f"/{cid}"), ("patch", f"/{cid}"), ("delete", f"/{cid}"),
                         ("get", f"/{cid}/messages")):
        kw = {"json": {"title": "x"}} if method == "patch" else {}
        r = getattr(api.c, method)(f"/api/v1/conversations{path}", **kw)
        assert r.status_code == 404 and r.json()["error"]["code"] == "not_found", path
    assert api.c.get("/api/v1/conversations").json()["items"] == []

    api.user = A
    assert api.c.patch(f"/api/v1/conversations/{cid}", json={"title": "Renamed"}).json()["title"] == "Renamed"
    assert api.c.delete(f"/api/v1/conversations/{cid}").status_code == 204
    assert api.c.get(f"/api/v1/conversations/{cid}").status_code == 404


def test_chat_end_to_end_with_sources_and_history(api):
    d = api.rig.add_doc(A, "Pets", [(4, "the cat sleeps all day")])
    cid = api.create_conv(mode="single_document", document_id=d).json()["id"]
    api.llm.replies = ["Cats sleep a lot [S1]."]
    r = api.c.post("/api/v1/chat", json={"conversation_id": cid, "message": "What do cats do?"})
    body = r.json()
    assert r.status_code == 200 and body["answer"] == "Cats sleep a lot [S1]."
    assert body["sources"][0]["page_number"] == 4 and body["sources"][0]["document_id"] == d
    assert body["usage"]["output_tokens"] == 7

    msgs = api.c.get(f"/api/v1/conversations/{cid}/messages").json()["items"]
    assert [m["role"] for m in msgs] == ["assistant", "user"]                  # newest first
    assert msgs[0]["sources"][0]["page_number"] == 4 and msgs[1]["sources"] == []
    assert api.c.get(f"/api/v1/conversations/{cid}").json()["title"] == "What do cats do?"


def test_chat_rejects_foreign_conversation_and_client_supplied_scope(api):
    d = api.rig.add_doc(A, "Pets", [(1, "cat")])
    cid = api.create_conv(mode="single_document", document_id=d).json()["id"]
    api.user = B
    r = api.c.post("/api/v1/chat", json={"conversation_id": cid, "message": "cat?"})
    assert r.status_code == 404
    api.user = A
    for extra in ({"document_id": d}, {"user_id": B}):
        r = api.c.post("/api/v1/chat", json={"conversation_id": cid, "message": "cat?", **extra})
        assert r.status_code == 422
    assert api.c.post("/api/v1/chat", json={"conversation_id": cid, "message": ""}).status_code == 422
    assert api.c.post("/api/v1/chat", json={"conversation_id": cid, "message": "x" * 4001}).status_code == 422
    assert api.rig.tables["messages"] == []


def test_chat_quota_error_envelope_and_no_assistant_message(api):
    d = api.rig.add_doc(A, "Pets", [(1, "cat")])
    cid = api.create_conv(mode="single_document", document_id=d).json()["id"]
    api.llm.replies = [LLMQuotaError()]
    r = api.c.post("/api/v1/chat", json={"conversation_id": cid, "message": "cat?"})
    assert r.status_code == 429 and r.json()["error"]["code"] == "llm_quota_exceeded"
    assert r.headers["retry-after"] == "60"
    assert api.rig.tables["messages"] == []          # nothing stored on failure: a retry cannot duplicate turns


def test_chat_rate_limit_per_user(api, monkeypatch):
    monkeypatch.setenv("CHAT_RATE_PER_MINUTE", "2")
    d = api.rig.add_doc(A, "Pets", [(1, "cat")])
    cid = api.create_conv(mode="single_document", document_id=d).json()["id"]
    api.llm.replies = ["a [S1]"] * 5
    body = {"conversation_id": cid, "message": "cat?"}
    assert api.c.post("/api/v1/chat", json=body).status_code == 200
    assert api.c.post("/api/v1/chat", json=body).status_code == 200
    r = api.c.post("/api/v1/chat", json=body)
    assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limited" and "retry-after" in r.headers
    api.user = B                                      # another user is unaffected
    assert api.c.post("/api/v1/chat", json=body).status_code == 404


def test_chat_requires_auth(monkeypatch):
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(router, prefix="/api/v1")
    c = TestClient(app)
    assert c.post("/api/v1/chat", json={"conversation_id": str(uuid.uuid4()), "message": "hi"}).status_code == 401
    assert c.get("/api/v1/conversations").status_code == 401
