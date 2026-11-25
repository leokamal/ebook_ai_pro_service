import sys
import uuid

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.core.dependencies import get_current_user
from app.core.exceptions import AppError, Conflict, NotFound, install_error_handlers
from app.core.security import AuthUser
from app.repositories.base import clamp_limit, decode_cursor, encode_cursor
from app.repositories.conversations import ConversationRepository, MessageWriter
from app.repositories.documents import DocumentAdminRepository, DocumentRepository, build_file_path
from tests.pro.fakedb import FakeDB

A, B = str(uuid.uuid4()), str(uuid.uuid4())


def _two_users():
    tables = {}
    return (DocumentRepository(FakeDB(tables, A)), DocumentRepository(FakeDB(tables, B)), tables)


# ---------------------------------------------------------------- documents / ownership
def test_build_file_path_is_server_side_and_safe():
    d = str(uuid.uuid4())
    assert build_file_path(A, d, "book.pdf") == f"{A}/{d}/book.pdf"
    for bad in ("../x.pdf", "a/b.pdf", "a\\b.pdf", "..", "", "x\x00.pdf"):
        with pytest.raises(AppError):
            build_file_path(A, d, bad)


def test_user_b_cannot_see_user_a_document():
    ra, rb, _ = _two_users()
    did = str(uuid.uuid4())
    doc, created = ra.create(A, did, "T", "t.pdf", 10)
    assert created and doc["file_path"] == f"{A}/{did}/t.pdf"
    assert ra.get(did)["id"] == did
    with pytest.raises(NotFound):
        rb.get(did)
    assert rb.list()["items"] == []
    with pytest.raises(NotFound):
        rb.update_title(did, "hacked")


def test_create_is_idempotent_for_owner():
    ra, _, _ = _two_users()
    did = str(uuid.uuid4())
    ra.create(A, did, "T", "t.pdf", 10)
    _, created = ra.create(A, did, "T", "t.pdf", 10)
    assert created is False


def test_create_id_collision_with_other_user_is_conflict():
    ra, rb, _ = _two_users()
    did = str(uuid.uuid4())
    ra.create(A, did, "T", "t.pdf", 10)
    with pytest.raises(Conflict):
        rb.create(B, did, "T", "t.pdf", 10)  # B cannot see A's row (RLS) so the insert hits the PK


def test_admin_repo_always_filters_by_owner():
    ra, _, tables = _two_users()
    did = str(uuid.uuid4())
    ra.create(A, did, "T", "t.pdf", 10)
    admin = DocumentAdminRepository(FakeDB(tables))  # service role: sees everything
    assert admin.get_owned(did, A)["id"] == did
    with pytest.raises(NotFound):
        admin.get_owned(did, B)
    with pytest.raises(NotFound):
        admin.update_owned(did, B, {"status": "completed"})
    admin.delete_owned(did, B)
    assert admin.get_owned(did, A)  # B's delete did nothing


# ---------------------------------------------------------------- conversations / messages
def test_conversation_rules_and_isolation():
    tables = {}
    docs_a, convs_a = DocumentRepository(FakeDB(tables, A)), ConversationRepository(FakeDB(tables, A))
    convs_b = ConversationRepository(FakeDB(tables, B))
    did = str(uuid.uuid4())
    docs_a.create(A, did, "T", "t.pdf", 10)
    with pytest.raises(AppError):
        convs_a.create(A, "single_document", None, None)
    with pytest.raises(AppError):
        convs_a.create(A, "all_documents", did, None)
    with pytest.raises(NotFound):
        convs_b.create(B, "single_document", did, None)  # A's document is invisible to B
    c = convs_a.create(A, "single_document", did, "hello")
    with pytest.raises(NotFound):
        convs_b.get(c["id"])
    with pytest.raises(NotFound):
        convs_b.list_messages(c["id"])
    with pytest.raises(NotFound):
        convs_b.delete(c["id"])


def test_message_writer_checks_ownership_and_lists_sources():
    tables = {}
    convs_a = ConversationRepository(FakeDB(tables, A))
    c = convs_a.create(A, "all_documents", None, None)
    w = MessageWriter(FakeDB(tables))
    with pytest.raises(NotFound):
        w.add_message(c["id"], B, "user", "x")
    m = w.add_message(c["id"], A, "assistant", "answer", 5)
    w.add_sources(m["id"], c["id"], A, [{"document_id": None, "page_number": 3}])
    with pytest.raises(NotFound):
        w.add_sources(m["id"], c["id"], B, [])
    msgs = convs_a.list_messages(c["id"])["items"]
    assert msgs[0]["sources"][0]["page_number"] == 3


# ---------------------------------------------------------------- pagination helpers
def test_cursor_roundtrip_and_validation():
    row = {"created_at": "2026-01-01T00:00:00+00:00", "id": str(uuid.uuid4())}
    assert decode_cursor(encode_cursor(row)) == (row["created_at"], row["id"])
    for bad in ("!!!", encode_cursor({"created_at": "x),id.neq.0", "id": row["id"]})):
        with pytest.raises(AppError):
            decode_cursor(bad)
    assert clamp_limit(None) == 20 and clamp_limit(1000) == 100 and clamp_limit(0) == 20


# ---------------------------------------------------------------- API error model + auth dependency
def _app():
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/api/v1/me")
    def me(u: AuthUser = Depends(get_current_user)):
        return {"id": u.id}

    @app.get("/legacy")
    def legacy():
        from fastapi import HTTPException
        raise HTTPException(404, "nope")

    @app.get("/api/v1/needs-int")
    def needs_int(n: int):
        return n
    return TestClient(app)


def test_api_errors_use_error_envelope():
    c = _app()
    r = c.get("/api/v1/me")
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    assert r.json()["error"]["code"] == "unauthorized" and r.json()["error"]["request_id"]
    r = c.get("/api/v1/me", headers={"Authorization": "Bearer junk"})
    assert r.status_code == 401
    r = c.get("/api/v1/needs-int?n=x")
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
    assert c.get("/api/v1/missing").json()["error"]["code"] == "not_found"


def test_legacy_error_shape_unchanged():
    c = _app()
    assert c.get("/legacy").json() == {"detail": "nope"}


def test_main_app_routes_and_lazy_imports():
    import main
    c = TestClient(main.app)
    assert c.get("/health").json() == {"status": "ok"}                       # liveness probe
    assert c.get("/api/v1/health").status_code in (200, 503)
    assert c.get("/api/v1/me-nope").json()["error"]["code"] == "not_found"
    for gone in ("/hello", "/test/", "/query", "/query-database/", "/upload", "/generate-database/", "/load-database/"):
        assert c.request("GET", gone).status_code in (404, 405), gone     # the old version is not served any more
    for heavy in ("torch", "easyocr", "chromadb", "sentence_transformers", "fitz", "langchain_google_genai"):
        assert heavy not in sys.modules, heavy
