"""/me, account deletion and orphan cleanup, with two users and the real ingestion pipeline."""
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import router
from app.core import dependencies as deps
from app.core.exceptions import install_error_handlers
from app.core.security import AuthUser
from app.repositories.account import AccountRepository
from app.repositories.jobs import JobQueue
from app.services.account import AccountService
from app.services.maintenance import Maintenance
from app.workers.runner import IngestHandler, ThreadJobRunner
from tests.pro.conftest_helpers import A, B, PDF, World


def _ingest(w, user, doc):
    JobQueue(w.db).enqueue(doc, user)
    ThreadJobRunner(JobQueue(w.db), IngestHandler(w.service)).run_once()


class Acct:
    def __init__(self):
        self.w = World()
        self.w.tables["app_limits"] = [{"max_storage_bytes": 1000, "max_objects": 5}]
        self.doc_a, self.doc_b = self.w.add_doc(A), self.w.add_doc(B)
        _ingest(self.w, A, self.doc_a)
        _ingest(self.w, B, self.doc_b)
        self.w.storage.put(A, str(uuid.uuid4()), "never-registered.pdf")
        self.user, self.deleted_users, self.fail = A, [], set()
        app = FastAPI()
        install_error_handlers(app)
        app.include_router(router, prefix="/api/v1")
        o = app.dependency_overrides
        o[deps.get_current_user] = lambda: AuthUser(self.user, "tok", "a@x.test", ("google",), "Ana", None)
        o[deps.get_service_db] = lambda: self.w.db
        o[deps.get_account_service] = self.service
        self.c = TestClient(app)

    def service(self):
        def step(name, fn):
            def run(*a):
                if name in self.fail:
                    raise RuntimeError("secret failure detail")
                return fn(*a)
            return run

        class Store:
            delete_user = staticmethod(step("vectors", self.w.store.delete_user))

        class Storage:
            remove_object = staticmethod(step("files", self.w.storage.remove_object))

        return AccountService(AccountRepository(self.w.db), Store(), Storage(), "user-documents",
                              step("auth", self.deleted_users.append))

    def delete(self, body={"confirm": "DELETE"}):
        return self.c.request("DELETE", "/api/v1/me", json=body)

    def files(self, user):
        return [k for k in self.w.storage.files if k.startswith(user + "/")]

    def vectors(self, user):
        return [m for _, _, m in self.w.client.cols["col"].rows.values() if m["user_id"] == user]


@pytest.fixture
def acct():
    return Acct()


def test_me_shows_only_my_profile_and_space(acct):
    body = acct.c.get("/api/v1/me").json()
    assert body["id"] == A and body["providers"] == ["google"] and body["name"] == "Ana"
    assert body["usage"] == {"documents": 1, "storage_bytes": 2 * len(PDF), "storage_limit_bytes": 1000,
                             "objects": 2, "object_limit": 5}
    acct.user = B
    assert acct.c.get("/api/v1/me").json()["usage"]["objects"] == 1


@pytest.mark.parametrize("body", [{}, {"confirm": "delete"}, {"confirm": "DELETE", "user_id": B}])
def test_delete_needs_exact_confirmation(acct, body):
    assert acct.delete(body).status_code == 422
    assert acct.deleted_users == [] and acct.files(A) and acct.vectors(A)


def test_delete_removes_everything_of_mine_and_nothing_else(acct):
    b_before = (acct.files(B), len(acct.vectors(B)))
    r = acct.delete()
    assert r.status_code == 204
    assert acct.files(A) == [] and acct.vectors(A) == [] and acct.deleted_users == [A]
    assert (acct.files(B), len(acct.vectors(B))) == b_before
    rows = {d["user_id"]: d["status"] for d in acct.w.tables["documents"]}
    assert rows[A] == "deleting" and rows[B] == "completed"   # real rows go with the auth user's cascade


@pytest.mark.parametrize("step", ["vectors", "files", "auth"])
def test_failure_keeps_the_account_and_a_retry_completes(acct, step):
    acct.fail = {step}
    r = acct.delete()
    assert r.status_code == 503 and r.json()["error"]["code"] == "account_deletion_incomplete"
    assert "secret" not in r.text and acct.deleted_users == []
    acct.fail = set()
    assert acct.delete().status_code == 204
    assert acct.deleted_users == [A] and acct.files(A) == [] and acct.vectors(A) == []


def test_foreign_object_names_are_never_removed(acct):
    class Repo(AccountRepository):
        def list_user_objects(self, user_id):
            return [f"{B}/x/y.pdf", f"{A}/../{B}/y.pdf"]
    removed = []

    class Storage:
        remove_object = staticmethod(lambda bucket, name: removed.append(name))

    AccountService(Repo(acct.w.db), acct.w.store, Storage(), "b", lambda u: None).delete(A)
    assert removed == []


def test_delete_user_vectors_works_while_reindex_is_needed(acct):
    from app.services.vector_store import ChromaVectorStore
    other = ChromaVectorStore("p", "col", "another-model", 4, client_factory=lambda _p: acct.w.client)
    assert other.health()["state"] == "needs_reindex"
    other.delete_user(A)
    assert acct.vectors(A) == [] and acct.vectors(B)
    with pytest.raises(ValueError):
        other.delete_user("")


def test_orphan_cleanup_only_removes_old_unregistered_uploads(acct):
    w = acct.w
    m = Maintenance(w.docs, JobQueue(w.db), w.store, w.service)
    repo = AccountRepository(w.db)
    orphan = next(k for k in w.storage.files if "never-registered" in k)
    registered = next(k for k in w.storage.files if k.startswith(A) and k != orphan)
    assert m.clean_orphans(repo, 24) == 0                       # fresh upload, not registered yet: kept
    w.storage.ages = {orphan: 25, registered: 999}
    assert m.clean_orphans(repo, 24) == 1
    assert orphan not in w.storage.files and registered in w.storage.files


def test_account_deleted_during_ingestion_leaves_no_vectors(acct):
    from app.services.ingestion import LeaseLost
    w = acct.w
    doc = w.add_doc(A)
    q = JobQueue(w.db)
    q.enqueue(doc, A)
    job = q.claim("w1", 300)

    def account_vanishes(pct):
        if w.client.cols["col"].rows:                 # some chunks written, then the cascade removes the rows
            w.tables["documents"][:] = [d for d in w.tables["documents"] if d["user_id"] != A]
            return False
        return True
    with pytest.raises(LeaseLost):
        w.service.process(job, account_vanishes)
    assert not [m for m in acct.vectors(A) if m["document_id"] == doc] and acct.vectors(B)


def test_claim_and_finish_never_revive_a_deleting_document(acct):
    w = acct.w
    doc = w.add_doc(A)
    q = JobQueue(w.db)
    q.enqueue(doc, A)
    row = next(d for d in w.tables["documents"] if d["id"] == doc)
    row["status"] = "deleting"
    job = q.claim("w1", 300)
    assert row["status"] == "deleting"
    assert q.finish(job["id"], "w1", False, "x", None)
    assert row["status"] == "deleting"
