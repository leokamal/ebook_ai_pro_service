"""SQL-level security tests: RLS, column grants, constraints, storage policies, job RPC privileges.

NOT run in the normal suite: they need a real Postgres with the Supabase schema (auth, storage) and the
migration applied. Run against a LOCAL Supabase (`supabase start && supabase db reset`) or a throwaway project:

    pip install "psycopg[binary]"
    SUPABASE_TEST_DB_URL=postgresql://postgres:postgres@127.0.0.1:54322/postgres pytest tests/pro/integration -m integration

Every test runs inside a transaction that is rolled back, so the database is left untouched.
"""
import json
import os
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration
psycopg = pytest.importorskip("psycopg")
URL = os.environ.get("SUPABASE_TEST_DB_URL")
if not URL:
    pytest.skip("SUPABASE_TEST_DB_URL not set", allow_module_level=True)


@pytest.fixture
def db():
    conn = psycopg.connect(URL, autocommit=False)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def as_role(conn, role, uid=None, provider="google", **extra):
    conn.execute("reset role")
    if role != "postgres":
        conn.execute(f"set local role {role}")
    claims = {"sub": uid, "role": role, "app_metadata": {"provider": provider}, **extra} if uid else {}
    conn.execute("select set_config('request.jwt.claims', %s, true)", (json.dumps(claims),))


def make_user(conn):
    uid = str(uuid.uuid4())
    as_role(conn, "postgres")
    conn.execute("insert into auth.users (id, email) values (%s, %s)", (uid, f"{uid}@test.local"))
    return uid


def add_doc(conn, uid, doc=None, name="b.pdf", **extra):
    doc = doc or str(uuid.uuid4())
    as_role(conn, "authenticated", uid)
    conn.execute("insert into public.documents (id, user_id, title, file_name, file_path, file_size) "
                 "values (%s,%s,'T',%s,%s,100)", (doc, uid, name, f"{uid}/{doc}/{name}"))
    return doc


def denied(conn, sql, params=()):
    """True if the statement is rejected (RLS, privilege or constraint); the failed statement is rolled back."""
    conn.execute("savepoint s")
    try:
        cur = conn.execute(sql, params)
        rows = cur.rowcount
        conn.execute("rollback to savepoint s")
        return rows == 0 if sql.lstrip().lower().startswith(("update", "delete")) else False
    except (psycopg.errors.InsufficientPrivilege, psycopg.errors.CheckViolation, psycopg.errors.ForeignKeyViolation,
            psycopg.errors.UniqueViolation) as e:
        conn.execute("rollback to savepoint s")
        return True


# ------------------------------------------------------------------ documents
def test_users_only_see_and_touch_their_own_documents(db):
    a, b = make_user(db), make_user(db)
    doc = add_doc(db, a)
    as_role(db, "authenticated", b)
    assert db.execute("select count(*) from public.documents").fetchone()[0] == 0
    assert denied(db, "update public.documents set title='x' where id=%s", (doc,))           # 0 rows
    assert denied(db, "delete from public.documents where id=%s", (doc,))
    as_role(db, "authenticated", a)
    assert db.execute("select count(*) from public.documents").fetchone()[0] == 1


def test_users_cannot_write_server_owned_columns_or_impersonate(db):
    a, b = make_user(db), make_user(db)
    doc = add_doc(db, a)
    as_role(db, "authenticated", a)
    for col, val in (("status", "completed"), ("index_state", "indexed"), ("chunk_count", 99),
                     ("error_message", "x"), ("page_count", 3), ("user_id", b)):
        assert denied(db, f"update public.documents set {col}=%s where id=%s", (val, doc)), col
    d2 = str(uuid.uuid4())
    assert denied(db, "insert into public.documents (id,user_id,title,file_name,file_path,file_size) "
                      "values (%s,%s,'T','b.pdf',%s,10)", (d2, b, f"{b}/{d2}/b.pdf"))           # user_id != auth.uid()
    assert denied(db, "insert into public.documents (id,user_id,title,file_name,file_path,file_size,status) "
                      "values (%s,%s,'T','b.pdf',%s,10,'completed')", (d2, a, f"{a}/{d2}/b.pdf"))  # forged status


def test_document_constraints(db):
    a = make_user(db)
    as_role(db, "authenticated", a)
    d = str(uuid.uuid4())
    base = "insert into public.documents (id,user_id,title,file_name,file_path,file_size) values (%s,%s,'T',%s,%s,%s)"
    assert denied(db, base, (d, a, "b.pdf", f"{a}/{d}/other.pdf", 10))              # path != user/doc/file_name
    assert denied(db, base, (d, a, "b.pdf", f"{uuid.uuid4()}/{d}/b.pdf", 10))       # wrong folder
    assert denied(db, base, (d, a, "../b.pdf", f"{a}/{d}/../b.pdf", 10))            # traversal in name
    assert denied(db, base, (d, a, "b.pdf", f"{a}/{d}/b.pdf", 0))                   # empty file
    assert denied(db, base, (d, a, "b.pdf", f"{a}/{d}/b.pdf", 52428801))            # > 50 MB


# ------------------------------------------------------------------ conversations / messages / sources
def test_conversation_rules_and_message_isolation(db):
    a, b = make_user(db), make_user(db)
    doc = add_doc(db, a)
    ins = "insert into public.conversations (user_id, mode, document_id) values (%s,%s,%s)"
    as_role(db, "authenticated", a)
    assert denied(db, ins, (a, "single_document", None))                            # mode/doc pairing
    assert denied(db, ins, (a, "all_documents", doc))
    as_role(db, "authenticated", b)
    assert denied(db, ins, (b, "single_document", doc))                             # B cannot attach A's document
    as_role(db, "authenticated", a)
    conv = db.execute(ins + " returning id", (a, "single_document", doc)).fetchone()[0]
    assert denied(db, "insert into public.messages (conversation_id, role, content) values (%s,'user','hi')", (conv,))
    as_role(db, "postgres")
    msg = db.execute("insert into public.messages (conversation_id, role, content) values (%s,'assistant','x') "
                     "returning id", (conv,)).fetchone()[0]
    as_role(db, "authenticated", b)
    assert db.execute("select count(*) from public.messages").fetchone()[0] == 0
    assert db.execute("select count(*) from public.message_sources").fetchone()[0] == 0
    assert denied(db, "delete from public.messages where id=%s", (msg,))
    as_role(db, "authenticated", a)
    assert db.execute("select count(*) from public.messages").fetchone()[0] == 1


def test_message_source_must_match_chunk_and_document(db):
    a = make_user(db)
    d1, d2 = add_doc(db, a), add_doc(db, a)
    as_role(db, "postgres")
    chunk = str(uuid.uuid4())
    db.execute("insert into public.document_chunks (id, document_id, user_id, chunk_index, page_number) "
               "values (%s,%s,%s,0,1)", (chunk, d1, a))
    conv = db.execute("insert into public.conversations (user_id, mode) values (%s,'all_documents') returning id",
                      (a,)).fetchone()[0]
    msg = db.execute("insert into public.messages (conversation_id, role, content) values (%s,'assistant','x') "
                     "returning id", (conv,)).fetchone()[0]
    ins = "insert into public.message_sources (message_id, document_id, chunk_id, page_number) values (%s,%s,%s,1)"
    assert denied(db, ins, (msg, d2, chunk))                                         # chunk belongs to d1, not d2
    db.execute(ins, (msg, d1, chunk))
    db.execute("delete from public.document_chunks where id=%s", (chunk,))           # reindex: history survives
    row = db.execute("select chunk_id, page_number, document_id from public.message_sources").fetchone()
    assert row[0] is None and row[1] == 1 and str(row[2]) == d1


def test_cascade_deletes(db):
    a = make_user(db)
    doc = add_doc(db, a)
    as_role(db, "postgres")
    conv = db.execute("insert into public.conversations (user_id, mode, document_id) values (%s,'single_document',%s) "
                      "returning id", (a, doc)).fetchone()[0]
    db.execute("insert into public.messages (conversation_id, role, content) values (%s,'user','x')", (conv,))
    db.execute("delete from public.documents where id=%s", (doc,))
    assert db.execute("select count(*) from public.conversations where id=%s", (conv,)).fetchone()[0] == 0
    assert db.execute("select count(*) from public.messages where conversation_id=%s", (conv,)).fetchone()[0] == 0


# ------------------------------------------------------------------ jobs
def test_jobs_are_read_only_for_users_and_rpcs_are_service_role_only(db):
    a, b = make_user(db), make_user(db)
    doc = add_doc(db, a)
    as_role(db, "service_role")
    job = db.execute("select public.enqueue_processing_job(%s,%s,'ingest')", (doc, a)).fetchone()[0]
    assert denied(db, "select public.enqueue_processing_job(%s,%s,'ingest')", (doc, a))      # one active job only
    assert db.execute("select status from public.documents where id=%s", (doc,)).fetchone()[0] == "queued"
    claimed = db.execute("select id, attempts from public.claim_processing_job('w1', 300)").fetchone()
    assert claimed[0] == job and claimed[1] == 1
    assert db.execute("select count(*) from public.claim_processing_job('w2', 300)").fetchone()[0] == 0  # leased
    as_role(db, "authenticated", a)
    assert db.execute("select count(*) from public.processing_jobs").fetchone()[0] == 1
    assert denied(db, "update public.processing_jobs set status='completed' where id=%s", (job,))
    assert denied(db, "insert into public.processing_jobs (user_id, document_id) values (%s,%s)", (a, doc))
    for fn in ("enqueue_processing_job(%s,%s,'ingest')", "claim_processing_job('w',300)",
               "finish_job(%s,true,'w1',null,null)"):
        params = (doc, a) if fn.startswith("enqueue") else ((job,) if fn.startswith("finish") else ())
        assert denied(db, f"select public.{fn}", params), fn
    as_role(db, "authenticated", b)
    assert db.execute("select count(*) from public.processing_jobs").fetchone()[0] == 0
    as_role(db, "anon")
    assert denied(db, "select count(*) from public.documents")


# ------------------------------------------------------------------ storage
def test_storage_policies_confine_users_to_their_own_folder(db):
    a, b = make_user(db), make_user(db)
    as_role(db, "authenticated", a)
    ins = "insert into storage.objects (bucket_id, name, owner) values ('user-documents', %s, %s)"
    doc = str(uuid.uuid4())
    db.execute(ins, (f"{a}/{doc}/b.pdf", a))                                         # own folder: allowed
    assert denied(db, ins, (f"{b}/{doc}/b.pdf", a))                                   # someone else's folder
    assert denied(db, "insert into storage.objects (bucket_id, name, owner) values ('other-bucket', %s, %s)",
                  (f"{a}/x", a))
    assert denied(db, "update storage.objects set name=%s where name=%s", (f"{a}/{doc}/c.pdf", f"{a}/{doc}/b.pdf"))
    as_role(db, "authenticated", b)
    assert db.execute("select count(*) from storage.objects where bucket_id='user-documents'").fetchone()[0] == 0
    # storage.protect_delete rejects direct deletes with 42501 unless this flag is set; set it so that the
    # assertion below really exercises the RLS policy (0 rows visible/deleted) and not the trigger
    db.execute("select set_config('storage.allow_delete_query', 'true', true)")
    assert denied(db, "delete from storage.objects where name=%s", (f"{a}/{doc}/b.pdf",))
    as_role(db, "authenticated", a)
    db.execute("select set_config('storage.allow_delete_query', 'true', true)")
    assert db.execute("select count(*) from storage.objects where name=%s", (f"{a}/{doc}/b.pdf",)).fetchone()[0] == 1
    as_role(db, "anon")
    assert db.execute("select count(*) from storage.objects where bucket_id='user-documents'").fetchone()[0] == 0
    row = db.execute("select public, file_size_limit, allowed_mime_types from storage.buckets "
                     "where id='user-documents'").fetchone()
    assert row[0] is False and row[1] == 52428800 and list(row[2]) == ["application/pdf"]


# ------------------------------------------------------------------ user space (20261126000000_user_space.sql)
def test_upload_is_blocked_at_the_space_limit_per_user(db):
    a, b = make_user(db), make_user(db)
    ins = ("insert into storage.objects (bucket_id, name, owner, metadata) "
           "values ('user-documents', %s, %s, '{\"size\": 600}')")
    as_role(db, "postgres")
    db.execute("update public.app_limits set max_storage_bytes = 1000, max_objects = 10")
    as_role(db, "authenticated", a)
    db.execute(ins, (f"{a}/{uuid.uuid4()}/1.pdf", a))
    db.execute(ins, (f"{a}/{uuid.uuid4()}/2.pdf", a))              # 600 < 1000: allowed (may overshoot)
    assert denied(db, ins, (f"{a}/{uuid.uuid4()}/3.pdf", a))       # 1200 >= 1000
    as_role(db, "authenticated", b)
    db.execute(ins, (f"{b}/{uuid.uuid4()}/1.pdf", b))              # another user is unaffected
    assert denied(db, "select public.user_storage_usage(%s)", (a,))
    assert denied(db, "select public.list_user_objects(%s)", (a,))
    assert denied(db, "select public.list_orphan_objects(interval '1 hour', 5)")
    assert denied(db, "update public.app_limits set max_objects = 999999")
    as_role(db, "service_role")
    assert db.execute("select bytes, objects from public.user_storage_usage(%s)", (a,)).fetchone() == (1200, 2)
    assert db.execute("select count(*) from public.list_user_objects(%s)", (a,)).fetchone()[0] == 2


# ------------------------------------------------------------------ hardening
def add_job(conn, uid, doc, status="completed", age_days=0):
    as_role(conn, "postgres")
    return conn.execute(
        "insert into public.processing_jobs (user_id, document_id, status, created_at, completed_at) values "
        "(%s,%s,%s, now() - make_interval(days => %s), now() - make_interval(days => %s)) returning id",
        (uid, doc, status, age_days, age_days)).fetchone()[0]


def test_users_may_delete_only_unregistered_files(db):
    a = make_user(db)
    doc = add_doc(db, a)
    as_role(db, "authenticated", a)
    ins = "insert into storage.objects (bucket_id, name, owner) values ('user-documents', %s, %s)"
    registered, loose = f"{a}/{doc}/b.pdf", f"{a}/{uuid.uuid4()}/loose.pdf"
    db.execute(ins, (registered, a))
    db.execute(ins, (loose, a))
    db.execute("select set_config('storage.allow_delete_query', 'true', true)")
    assert denied(db, "delete from storage.objects where name=%s", (registered,))
    assert db.execute("delete from storage.objects where name=%s", (loose,)).rowcount == 1


def test_only_the_lease_holder_can_finish_a_job(db):
    a = make_user(db)
    doc = add_doc(db, a)
    as_role(db, "service_role")
    job = db.execute("select public.enqueue_processing_job(%s,%s,'ingest')", (doc, a)).fetchone()[0]
    db.execute("select * from public.claim_processing_job('w1', 300)")
    finish = "select public.finish_job(%s, %s, %s, 'boom', null)"
    assert db.execute(finish, (job, False, "w2")).fetchone()[0] is False
    assert db.execute("select status from public.documents where id=%s", (doc,)).fetchone()[0] == "processing"
    assert db.execute(finish, (job, False, "w1")).fetchone()[0] is True
    assert db.execute("select status from public.documents where id=%s", (doc,)).fetchone()[0] == "failed"
    assert db.execute(finish, (job, True, "w1")).fetchone()[0] is False          # already finished


def test_pruning_keeps_the_latest_job_of_each_document(db):
    a = make_user(db)
    d1, d2 = add_doc(db, a), add_doc(db, a)
    old = add_job(db, a, d1, age_days=90)
    latest = add_job(db, a, d1, age_days=60)
    only = add_job(db, a, d2, age_days=90)
    as_role(db, "authenticated", a)
    assert denied(db, "select public.prune_processing_jobs(interval '30 days')")
    as_role(db, "service_role")
    assert db.execute("select public.prune_processing_jobs(interval '30 days')").fetchone()[0] == 1
    left = {r[0] for r in db.execute("select id from public.processing_jobs").fetchall()}
    assert old not in left and {latest, only} <= left


@pytest.mark.parametrize("table,column,insert", [
    ("documents", "max_documents", None),
    ("conversations", "max_conversations",
     "insert into public.conversations (user_id, mode) values (%s, 'all_documents')"),
])
def test_row_limits_hold_for_direct_inserts(db, table, column, insert):
    a, b = make_user(db), make_user(db)
    as_role(db, "postgres")
    db.execute(f"update public.app_limits set {column} = 1")

    def add(uid):
        if insert is None:
            add_doc(db, uid)
        else:
            as_role(db, "authenticated", uid)
            db.execute(insert, (uid,))

    add(a)
    db.execute("savepoint s")
    with pytest.raises(psycopg.errors.RaiseException, match="quota_exceeded"):
        add(a)
    db.execute("rollback to savepoint s")
    add(b)                                                                        # per user


def test_dot_file_name_is_rejected(db):
    a = make_user(db)
    d = str(uuid.uuid4())
    as_role(db, "authenticated", a)
    assert denied(db, "insert into public.documents (id,user_id,title,file_name,file_path,file_size) "
                      "values (%s,%s,'T','.',%s,10)", (d, a, f"{a}/{d}/."))


def test_upserting_a_chunk_keeps_the_citation(db):
    a = make_user(db)
    doc = add_doc(db, a)
    as_role(db, "service_role")
    chunk = str(uuid.uuid4())
    upsert = ("insert into public.document_chunks (id, document_id, user_id, chunk_index, page_number) "
              "values (%s,%s,%s,0,%s) on conflict (id) do update set id = excluded.id, "
              "document_id = excluded.document_id, user_id = excluded.user_id, "
              "chunk_index = excluded.chunk_index, page_number = excluded.page_number")
    db.execute(upsert, (chunk, doc, a, 1))
    conv = db.execute("insert into public.conversations (user_id, mode) values (%s,'all_documents') returning id",
                      (a,)).fetchone()[0]
    msg = db.execute("insert into public.messages (conversation_id, role, content) values (%s,'assistant','x') "
                     "returning id", (conv,)).fetchone()[0]
    db.execute("insert into public.message_sources (message_id, document_id, chunk_id, page_number) "
               "values (%s,%s,%s,1)", (msg, doc, chunk))
    db.execute(upsert, (chunk, doc, a, 2))
    assert str(db.execute("select chunk_id from public.message_sources").fetchone()[0]) == chunk


def test_indexes_exist(db):
    names = {r[0] for r in db.execute("select indexname from pg_indexes where schemaname='public'").fetchall()}
    assert {"message_sources_doc_chunk_idx", "processing_jobs_document_idx"} <= names


def test_rerunning_the_first_migration_keeps_the_upload_limit(db):
    a = make_user(db)
    sql = Path(__file__).parents[3] / "supabase" / "migrations" / "20261125000000_pro_schema.sql"
    db.execute(sql.read_text(encoding="utf-8"))
    db.execute("update public.app_limits set max_objects = 0")
    as_role(db, "authenticated", a)
    assert denied(db, "insert into storage.objects (bucket_id, name, owner) values ('user-documents', %s, %s)",
                  (f"{a}/{uuid.uuid4()}/b.pdf", a))


@pytest.mark.parametrize("claims,allowed", [
    ({"provider": "google"}, True),
    ({"provider": "facebook"}, True),
    ({"provider": "email"}, False),
    ({"provider": "email", "app_metadata": {"provider": "email", "providers": ["email", "google"]}}, True),
    ({"provider": "email", "user_metadata": {"provider": "google"}}, False),   # user-editable: ignored
    ({"provider": "google", "is_anonymous": True}, False),
    ({"app_metadata": {}}, False),
])
def test_only_google_and_facebook_sessions_reach_data_and_storage(db, claims, allowed):
    a = make_user(db)
    doc = add_doc(db, a)                                                          # the user's own row
    as_role(db, "authenticated", a, **claims)
    assert db.execute("select count(*) from public.documents").fetchone()[0] == int(allowed)
    conv = "insert into public.conversations (user_id, mode) values (%s,'all_documents')"
    upload = "insert into storage.objects (bucket_id, name, owner) values ('user-documents', %s, %s)"
    for sql, params in ((conv, (a,)), (upload, (f"{a}/{doc}/x.pdf", a))):
        if allowed:
            db.execute(sql, params)
        else:
            assert denied(db, sql, params)


# ------------------------------------------------------------------ profiles (20261127000000_profiles.sql)
PROFILE = ("select generated_key, email, full_name, display_name, avatar_url from public.profiles where id=%s")


def signup(conn, meta, email=True):
    uid = str(uuid.uuid4())
    as_role(conn, "postgres")
    conn.execute("insert into auth.users (id, email, raw_user_meta_data) values (%s, %s, %s)",
                 (uid, f"{uid}@test.local" if email else None, json.dumps(meta)))
    return uid


def link(conn, uid, provider, data):
    as_role(conn, "postgres")
    conn.execute("insert into auth.identities (id, user_id, provider, provider_id, identity_data) "
                 "values (gen_random_uuid(), %s, %s, %s, %s)", (uid, provider, str(uuid.uuid4()), json.dumps(data)))


def test_each_new_user_gets_a_profile_from_the_provider(db):
    uid = signup(db, {"full_name": "Ana", "picture": "https://img.test/a.png"})
    key, email, full, display, image = db.execute(PROFILE, (uid,)).fetchone()
    assert key and email == f"{uid}@test.local"
    assert (full, display, image) == ("Ana", "Ana", "https://img.test/a.png")
    link(db, uid, "facebook", {"name": "Other", "picture": "https://img.test/fb.png"})   # only fills empty fields
    assert db.execute(PROFILE, (uid,)).fetchone() == (key, email, "Ana", "Ana", "https://img.test/a.png")


def test_linked_account_supplies_missing_name_and_image(db):
    uid = signup(db, {"avatar_url": "javascript:alert(1)"}, email=False)          # unsafe image, no name, no e-mail
    _, email, full, display, image = db.execute(PROFILE, (uid,)).fetchone()
    assert (email, full, image) == (None, None, None) and display.startswith("Reader")
    link(db, uid, "google", {"full_name": "  <Zed> " + "z" * 300, "avatar_url": "https://img.test/g.png"})
    _, _, full, display, image = db.execute(PROFILE, (uid,)).fetchone()
    assert len(full) == 200 and display == "Zed " + "z" * 16 and image == "https://img.test/g.png"


def test_users_edit_only_their_own_choices(db):
    uid, other = signup(db, {"full_name": "Ana"}), signup(db, {"full_name": "Ana"})   # names need not be unique
    key = db.execute(PROFILE, (uid,)).fetchone()[0]
    as_role(db, "authenticated", uid)
    assert db.execute("select count(*) from public.profiles").fetchone()[0] == 1
    assert db.execute("update public.profiles set display_name='Ani', avatar_hidden=true, country='MA', "
                      "gender='unspecified' where id=%s", (uid,)).rowcount == 1
    for column, value in (("email", "x@x"), ("full_name", "x"), ("avatar_url", "https://x"),
                          ("generated_key", str(uuid.uuid4())), ("display_name", "a!"), ("country", "morocco")):
        assert denied(db, f"update public.profiles set {column}=%s where id=%s", (value, uid)), column
    assert denied(db, "update public.profiles set display_name='Bad' where id=%s", (other,))
    assert denied(db, "insert into public.profiles (id, display_name) values (%s,'Mine')", (other,))
    as_role(db, "authenticated", uid, provider="email")
    assert db.execute("select count(*) from public.profiles").fetchone()[0] == 0
    # a later sign-in never overwrites the user's choices or the key
    as_role(db, "postgres")
    db.execute("update auth.users set raw_user_meta_data=%s where id=%s", (json.dumps({"full_name": "New"}), uid))
    assert db.execute(PROFILE, (uid,)).fetchone()[:4:3] == (key, "Ani")
    db.execute("delete from auth.users where id=%s", (uid,))
    assert db.execute("select count(*) from public.profiles where id=%s", (uid,)).fetchone()[0] == 0


# ------------------------------------------------------------------ sharing (20261201000000_document_sharing.sql)
def ready_doc(conn, uid, chunks=2):
    doc = add_doc(conn, uid)
    as_role(conn, "postgres")
    conn.execute("update public.documents set status='completed', index_state='indexed', chunk_count=%s where id=%s",
                 (chunks, doc))
    for i in range(chunks):
        conn.execute("insert into public.document_chunks (id, document_id, user_id, chunk_index, page_number) "
                     "values (%s,%s,%s,%s,1)", (str(uuid.uuid5(uuid.UUID(doc), str(i))), doc, uid, i))
    return doc


def rpc(conn, uid, name, doc):
    as_role(conn, "authenticated", uid)
    return conn.execute(f"select public.{name}(%s)", (doc,)).fetchone()[0]


def refused(conn, uid, name, doc, message):
    conn.execute("savepoint r")
    with pytest.raises(psycopg.Error, match=message):
        rpc(conn, uid, name, doc)
    conn.execute("rollback to savepoint r")


def test_sharing_by_id_is_opt_in_and_copies_rows_only(db):
    a, b, c = make_user(db), make_user(db), make_user(db)
    doc = ready_doc(db, a)
    refused(db, b, "accept_shared_document", doc, "not_found")                 # not shared yet
    refused(db, b, "share_document", doc, "not_found")                         # not B's
    rpc(db, a, "share_document", doc)
    copy = str(rpc(db, b, "accept_shared_document", doc))
    assert str(rpc(db, b, "accept_shared_document", doc)) == copy             # twice: same row
    assert str(rpc(db, a, "accept_shared_document", doc)) == doc              # own id
    refused(db, b, "share_document", copy, "forbidden")                        # only the uploader shares
    as_role(db, "authenticated", b)
    assert denied(db, "update public.documents set sharing_enabled=true where id=%s", (copy,))
    rows = db.execute("select id, chunk_index from public.document_chunks order by chunk_index").fetchall()
    assert [str(r[0]) for r in rows] == [str(uuid.uuid5(uuid.UUID(copy), str(i))) for i in range(2)]
    row = db.execute("select status, index_state, content_owner_id from public.documents").fetchone()
    assert row[:2] == ("completed", "indexed") and str(row[2]) == a
    rpc(db, a, "stop_sharing_document", doc)
    refused(db, c, "accept_shared_document", doc, "not_found")
    refused(db, c, "accept_shared_document", copy, "not_found")                # a copy's id opens nothing


def test_shared_file_is_readable_by_holders_and_deletable_by_nobody(db):
    a, b, c = make_user(db), make_user(db), make_user(db)
    doc = ready_doc(db, a)
    path = f"{a}/{doc}/b.pdf"
    as_role(db, "authenticated", a)
    db.execute("insert into storage.objects (bucket_id, name, owner) values ('user-documents', %s, %s)", (path, a))
    rpc(db, a, "share_document", doc)
    rpc(db, b, "accept_shared_document", doc)
    seen = "select count(*) from storage.objects where name=%s"
    as_role(db, "authenticated", b)
    assert db.execute(seen, (path,)).fetchone()[0] == 1
    assert db.execute("select public.path_in_use(%s)", (f"{a}/anything",)).fetchone()[0] is True   # no probing
    as_role(db, "authenticated", c)
    assert db.execute(seen, (path,)).fetchone()[0] == 0
    as_role(db, "postgres")
    db.execute("delete from public.documents where id=%s", (doc,))             # the uploader's row is gone
    for uid in (a, b):
        as_role(db, "authenticated", uid)
        db.execute("select set_config('storage.allow_delete_query', 'true', true)")
        assert denied(db, "delete from storage.objects where name=%s", (path,))
    as_role(db, "authenticated", c)                                             # the freed id stays reserved
    assert denied(db, "insert into public.documents (id,user_id,title,file_name,file_path,file_size) "
                      "values (%s,%s,'T','b.pdf',%s,10)", (doc, c, f"{c}/{doc}/b.pdf"))


def test_a_shared_copy_counts_toward_the_document_limit(db):
    a, b = make_user(db), make_user(db)
    doc = ready_doc(db, a)
    rpc(db, a, "share_document", doc)
    as_role(db, "postgres")
    db.execute("update public.app_limits set max_documents = 0")
    refused(db, b, "accept_shared_document", doc, "quota_exceeded")
