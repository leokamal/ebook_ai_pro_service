import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import router
from app.api.v1 import documents as documents_module
from app.core import dependencies as deps
from app.core.exceptions import install_error_handlers
from app.core.security import AuthUser
from tests.pro.conftest_helpers import A, B, PDF, World
from tests.pro.fakedb import FakeDB


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://p.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc")
    monkeypatch.setenv("ADMIN_TOKEN", "admintoken")


class Api:
    """One app, switchable current user; user-scoped DB mimics RLS, service DB sees everything."""

    def __init__(self):
        self.w = World()
        self.user = A
        self.deleted = []
        app = FastAPI()
        install_error_handlers(app)
        app.include_router(router, prefix="/api/v1")
        app.dependency_overrides[deps.get_current_user] = lambda: AuthUser(self.user, "tok")
        app.dependency_overrides[deps.get_user_db] = lambda: FakeDB(self.w.tables, self.user)
        app.dependency_overrides[deps.get_service_db] = lambda: self.w.db
        app.dependency_overrides[deps.get_storage] = lambda: self.w.storage
        self.maint_calls = []
        app.dependency_overrides[deps.get_maintenance] = lambda: self
        self.c = TestClient(app)

    # fake Maintenance
    def reindex_all(self):
        self.maint_calls.append("reindex")
        return {"reindex_queued": 3}

    def reconcile(self):
        self.maint_calls.append("reconcile")
        return {"checked": 5, "reindex_queued": 1}

    def as_user(self, u):
        self.user = u
        return self.c

    def upload(self, user, doc=None, name="b.pdf", data=PDF):
        doc = doc or str(uuid.uuid4())
        self.w.storage.put(user, doc, name, data)
        return doc

    def register(self, doc, name="b.pdf", **extra):
        return self.c.post("/api/v1/documents", json={"document_id": doc, "title": "Book", "file_name": name, **extra})


@pytest.fixture
def api(env, monkeypatch):
    a = Api()
    monkeypatch.setattr(documents_module, "_finish_delete", lambda d: a.deleted.append(d))
    return a


def test_register_requires_upload_and_validates_file(api):
    doc = str(uuid.uuid4())
    r = api.register(doc)
    assert r.status_code == 422 and r.json()["error"]["code"] == "file_not_found"
    api.upload(A, doc, data=b"NOTAPDF!!!")
    assert api.register(doc).json()["error"]["code"] == "invalid_file"
    d2 = api.upload(A, data=PDF + b"x" * 100)
    import os
    os.environ["MAX_PDF_BYTES"] = "50"
    try:
        assert api.register(d2).status_code == 413
    finally:
        del os.environ["MAX_PDF_BYTES"]


def test_register_ok_is_idempotent_and_auto_queues(api):
    doc = api.upload(A)
    r = api.register(doc)
    body = r.json()
    assert r.status_code == 201 and body["status"] == "queued" and body["file_path"] == f"{A}/{doc}/b.pdf"
    assert api.register(doc).status_code == 200                      # idempotent for the owner
    assert len(api.w.tables["processing_jobs"]) == 1                 # no second job
    d2 = api.upload(A)
    assert api.register(d2, auto_process=False).json()["status"] == "uploaded"


def test_register_rejects_client_supplied_identity_and_bad_names(api):
    doc = api.upload(A)
    r = api.c.post("/api/v1/documents", json={"document_id": doc, "title": "t", "file_name": "b.pdf",
                                              "user_id": B, "file_path": "x/y/z.pdf"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
    for bad in ("../x.pdf", "a/b.pdf", "a\\b.pdf", ".."):
        assert api.register(doc, name=bad).status_code == 422
    assert api.c.post("/api/v1/documents", json={"document_id": "nope", "title": "t", "file_name": "b.pdf"}).status_code == 422


def test_user_b_cannot_touch_user_a_documents(api):
    doc = api.upload(A)
    api.register(doc, auto_process=False)
    api.as_user(B)
    for method, path in (("get", f"/api/v1/documents/{doc}"), ("get", f"/api/v1/documents/{doc}/status"),
                         ("delete", f"/api/v1/documents/{doc}"), ("post", f"/api/v1/documents/{doc}/process"),
                         ("post", f"/api/v1/documents/{doc}/retry")):
        r = getattr(api.c, method)(path)
        assert r.status_code == 404 and r.json()["error"]["code"] == "not_found", path
    assert api.c.get("/api/v1/documents").json()["items"] == []
    assert api.w.tables["documents"][0]["status"] == "uploaded"       # B's DELETE did not mark it
    assert api.deleted == [] and api.w.tables["processing_jobs"] == [] if "processing_jobs" in api.w.tables else True


def test_registering_with_another_users_document_id_conflicts(api):
    doc = api.upload(A)
    api.register(doc)
    api.upload(B, doc)                       # B uploads under their own folder, but reuses A's document id
    api.as_user(B)
    r = api.register(doc)
    assert r.status_code == 409 and "already in use" in r.json()["error"]["message"]


def test_process_and_retry_state_rules(api):
    doc = api.upload(A)
    api.register(doc, auto_process=False)
    r = api.c.post(f"/api/v1/documents/{doc}/process")
    assert r.status_code == 202 and r.json()["status"] == "queued" and r.json()["job_id"]
    assert api.c.post(f"/api/v1/documents/{doc}/process").status_code == 409   # not 'uploaded' any more
    assert api.c.post(f"/api/v1/documents/{doc}/retry").status_code == 409     # not failed
    api.w.tables["documents"][0]["status"] = "failed"
    api.w.tables["processing_jobs"][0]["status"] = "failed"
    assert api.c.post(f"/api/v1/documents/{doc}/retry").status_code == 202
    assert api.c.post(f"/api/v1/documents/{doc}/retry").status_code == 409


def test_status_and_pagination(api):
    docs = [api.upload(A) for _ in range(3)]
    for d in docs:
        api.register(d)
    s = api.c.get(f"/api/v1/documents/{docs[0]}/status").json()
    assert s["status"] == "queued" and s["progress"] == 0 and s["attempts"] == 0
    page = api.c.get("/api/v1/documents?limit=2").json()
    assert len(page["items"]) == 2 and page["next_cursor"]
    assert api.c.get("/api/v1/documents?limit=0").status_code == 422
    assert api.c.get("/api/v1/documents?limit=101").status_code == 422
    assert api.c.get("/api/v1/documents?cursor=!!!").status_code == 400
    assert api.c.get("/api/v1/documents/not-a-uuid").status_code == 404


def test_delete_marks_deleting_then_cleans_up(api):
    doc = api.upload(A)
    api.register(doc)
    r = api.c.delete(f"/api/v1/documents/{doc}")
    assert r.status_code == 202 and r.json()["status"] == "deleting"
    assert api.w.tables["documents"][0]["status"] == "deleting"
    assert api.deleted and api.deleted[0]["user_id"] == A              # cleanup scheduled with the owner's id
    assert api.c.post(f"/api/v1/documents/{doc}/process").status_code == 409


def test_admin_endpoints_need_the_admin_token(api):
    assert api.c.post("/api/v1/admin/reindex").status_code in (403, 401)
    h = {"X-Admin-Token": "admintoken"}
    assert api.c.post("/api/v1/admin/reindex", headers=h).json() == {"reindex_queued": 3, "checked": None}
    assert api.c.post("/api/v1/admin/reconcile", headers=h).json()["checked"] == 5
    assert api.c.post("/api/v1/admin/reconcile", headers={"X-Admin-Token": "wrong"}).status_code == 403
    assert api.maint_calls == ["reindex", "reconcile"]


def test_quotas(api, monkeypatch):
    monkeypatch.setenv("MAX_DOCS_PER_USER", "2")
    monkeypatch.setenv("MAX_ACTIVE_JOBS_PER_USER", "1")
    d1, d2, d3 = api.upload(A), api.upload(A), api.upload(A)
    assert api.register(d1, auto_process=False).status_code == 201
    assert api.register(d2, auto_process=False).status_code == 201
    r = api.register(d3, auto_process=False)
    assert r.status_code == 429 and r.json()["error"]["code"] == "quota_exceeded"
    assert api.register(d1).status_code == 200                     # existing document: not a new one
    assert api.c.post(f"/api/v1/documents/{d1}/process").status_code == 202
    assert api.c.post(f"/api/v1/documents/{d2}/process").status_code == 429   # one active job already


def test_registration_is_refused_when_other_files_already_fill_the_space(api):
    api.w.tables["app_limits"] = [{"max_storage_bytes": 2 * len(PDF), "max_objects": 50}]
    d1, d2 = api.upload(A), api.upload(A)
    assert api.register(d1, auto_process=False).status_code == 201        # the file that reaches the limit is fine
    api.register(d2, auto_process=False)
    d3 = api.upload(A)                                                    # e.g. slipped in through parallel uploads
    r = api.register(d3, auto_process=False)
    assert r.status_code == 429 and r.json()["error"]["code"] == "quota_exceeded"
