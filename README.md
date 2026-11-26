# E-Book AI Pro — backend

FastAPI backend for **E-Book AI Pro**: every user has a private PDF library and can chat with one document (**Personal RAG**) or with the whole library (**Personal Multi-RAG**). Conversations and chat history are persisted, answers are grounded and carry document/page citations, and Arabic, French and English are supported.

Users sign in with **Google or Facebook** (Supabase Auth) and each gets a private, size-limited space for their PDFs, with in-app account deletion ([setup](docs/DEPLOYMENT.md#3b-sign-in-providers-and-user-space-one-time-setup-dashboard-work)).

Stack: Python 3.11, FastAPI, Supabase (Auth, Postgres + RLS, Storage), ChromaDB (the only vector store; persistent, in-process, never exposed), PyMuPDF, a multilingual sentence-transformers model, Gemini. No pgvector, no paid vector database. The previous "E-Book AI" single-library service has been removed; this repository is Pro only (the old code is in git history, commit `first init`).

Docs: [docs/API.md](docs/API.md) (contract for the Flutter app) · [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) (setup, hosting, recovery, runbook) · [docs/features/ebook_ai_pro_backend.md](docs/features/ebook_ai_pro_backend.md) (design + implementation status) · [CLAUDE.md](CLAUDE.md) (engineering notes and invariants). Interactive API docs at `/docs` when running.

## How it works

```
Flutter ──upload PDF (upsert:false)──▶ Supabase Storage  user-documents/{user_id}/{document_id}/{file}
   │                                         ▲ RLS: first folder must equal auth.uid()
   │ POST /api/v1/documents (Bearer = Supabase access token)
   ▼
FastAPI ── verify JWT (JWKS) ──▶ register row in Postgres (user-scoped client => RLS)
   │                                   │ enqueue_processing_job()  (transactional outbox, one active job per document)
   │                                   ▼
   │                      processing_jobs  ◀── worker thread (leases, backoff, janitor, reconcile)
   │                                   │  download PDF (ownership + path check) → PyMuPDF per page → chunks
   │                                   │  → embeddings → Chroma upsert → commit last (stale chunks removed, status=completed)
   ▼
POST /api/v1/chat | /chat/stream | /search
   ownership + readiness from Postgres → query embedding → Chroma (user_id + validated document ids, hits re-checked)
   → rerank (dedup, per-document cap) → numbered <sources> context → Gemini → [S1] citations validated against the
   chunks really in the prompt → turn persisted only after a complete, grounded answer
```

Guarantees (covered by tests in `tests/pro/`):
- The user id comes only from the verified JWT. Request bodies cannot carry `user_id` or `file_path`; unknown fields are rejected.
- Other users' resources behave as non-existent (`404`). Postgres RLS + column grants protect the tables; every service-role code path re-checks ownership; Chroma metadata filters are not the security boundary (hits are re-validated).
- Document text is untrusted data: it only appears inside a `<sources>` block of the user turn and cannot close that block.
- No fabricated citations; ungrounded answers become a "not found" reply; failed generations store nothing.
- Chroma can be rebuilt entirely from the PDFs and rows in Supabase (`/api/v1/admin/reconcile`, `/admin/reindex`); a changed embedding model can never mix vectors in one collection.

## Repository layout

```
main.py                    entrypoint (pysqlite3 swap first, CORS, request ids, mounts /api/v1, starts the worker)
security.py                admin token guard (X-Admin-Token)
supabase_storage.py        Storage REST helpers (stream download, inspect, remove)
app/
  core/         config, JWT verification, dependencies, error model + request ids, rate/concurrency limits
  api/v1/       me (account), documents, conversations, chat (+stream), search, health, admin
  schemas/      Pydantic request/response models
  repositories/ PostgREST access (user-scoped = RLS; service-role methods always filter by user_id)
  services/     embedding, vector_store (Chroma), retrieval, ingestion, maintenance, rag_parts, llm, orchestrator, pdf_extraction
  workers/      DB-backed job runner (+ manager started from the app lifespan)
  prompts/      all prompt text
supabase/migrations/       SQL: Pro schema, RLS, storage policies, job RPCs (+ the historical `books` migration, see below)
tests/pro/                 offline tests;  tests/pro/integration/  SQL tests (need Postgres)
Dockerfile, docker-entrypoint.sh, render.yaml, .env.example
```

`supabase/migrations/20261124000000_init_books.sql` belongs to the removed old version (a `books` catalog table and an `ebook-ai` bucket). It is kept only so migration history stays consistent on projects where it was already applied (deleting an applied migration breaks `supabase db push`). Pro does not use it. To clean a project up, add a new migration that drops `public.books` and delete the `ebook-ai` bucket in the dashboard once you are sure you no longer need that data.

## Local development

```bash
python -m venv .venv && source .venv/bin/activate        # Windows: py -m venv .venv ; .venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env                                      # fill in the placeholders (never commit .env)
python main.py                                            # or: uvicorn main:app --reload   (keep a single worker)
pytest                                                    # no network, no model downloads
```

### Supabase setup (once per project)
1. Create a project. Apply `supabase/migrations/*.sql` (`supabase db push`, or paste them in the SQL editor in order). PostgreSQL 15+ is required (`ON DELETE SET NULL (column)`).
2. Auth: enable **Google and Facebook**, disable anonymous and e-mail sign-ups (steps in `docs/DEPLOYMENT.md` 3b). Use **asymmetric JWT signing keys** (default on new projects) so the backend can verify via JWKS (`/auth/v1/.well-known/jwks.json`). Legacy HS256 projects: set `SUPABASE_JWT_SECRET` (only accepted for tokens without a `kid`).
3. Storage: the migration creates the private bucket `user-documents` (50 MB limit, PDF only) with per-user-folder policies. Flutter uploads to `{user_id}/{document_id}/{file_name}` with `upsert: false` (there is deliberately no UPDATE policy: uploaded PDFs are immutable).
4. Keys, and who may hold them:

| Credential | Where | Purpose |
|---|---|---|
| anon / publishable key | Flutter app **and** `SUPABASE_ANON_KEY` here | identifies the project; grants nothing without a user session |
| user access token (JWT) | sent by Flutter in `Authorization: Bearer` | verified by this API; forwarded to PostgREST so RLS applies |
| service-role key | backend env only (`SUPABASE_SERVICE_ROLE_KEY`) | bypasses RLS; used by the worker and storage checks, always after an explicit ownership check; **never** in Flutter or git |
| database URL | `SUPABASE_DB_URL`, ops/tests only | migrations and the SQL integration tests; not used on the request path |

## Testing

| Suite | Command | Needs |
|---|---|---|
| Everything offline (unit, API, two-user isolation, streaming, OpenAPI contract) | `pytest` | nothing (fakes for Supabase, Chroma, embeddings, Gemini) |
| SQL: RLS, column grants, storage policies, job RPC privileges | `SUPABASE_TEST_DB_URL=... pytest tests/pro/integration -m integration` | a real Postgres with the Supabase schema (`supabase start`), `pip install "psycopg[binary]"` |

Not exercised by the automated suite, so check on a staging project before launch: the SQL integration suite, real ChromaDB persistence (one test is skipped unless `chromadb` is installed), the real multilingual embedding model (Arabic/French retrieval quality, `RAG_MIN_SCORE` tuning), real Gemini calls, and the Storage REST helpers in `supabase_storage.py`.

## Deployment

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md): Docker/Render, persistent disk, free-hosting limits, recovery procedure, monitoring, runbook.
