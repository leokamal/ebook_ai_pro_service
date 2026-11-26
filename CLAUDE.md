# E-Book AI Pro backend

FastAPI service. Per-user PDF library, Personal RAG / Multi-RAG chat with citations, semantic search. Supabase (Auth, Postgres + RLS, Storage), ChromaDB (only vector store), Gemini, multilingual embeddings. **Pro only: the old single-library service (rag_service, OCR `/upload`, `/query*`, `/generate-database/`) was removed on purpose; do not reintroduce it.** Design + status: `docs/features/ebook_ai_pro_backend.md`. Operations: `docs/DEPLOYMENT.md`. Flutter contract: `docs/API.md`. Overview: `README.md`.

## Commands
- Install: `pip install -r requirements.txt -r requirements-dev.txt` · Run: `python main.py` (honours `$PORT`) or `uvicorn main:app --reload` (always ONE worker)
- Tests (offline, no network/models): `pytest` (on this Windows box: `.venv/Scripts/python -m pytest -q`). SQL tests: `SUPABASE_TEST_DB_URL=... pytest tests/pro/integration -m integration`.

## Environment variables (never hardcode; see `.env.example`)
`SUPABASE_URL`, `SUPABASE_ANON_KEY` (forwards the user's JWT to PostgREST), `SUPABASE_SERVICE_ROLE_KEY` (server only, bypasses RLS), `SUPABASE_JWT_JWKS_URL` (default derived from the URL), `SUPABASE_JWT_SECRET` (optional legacy HS256), `SUPABASE_DB_URL` (ops/tests only), `SUPABASE_STORAGE_BUCKET` (`user-documents`), `GEMINI_API_KEY`, `GEMINI_MODEL`, `EMBEDDING_MODEL`, `EMBEDDING_DIMENSION`, `CHROMA_PERSIST_DIRECTORY`, `CHROMA_COLLECTION_NAME`, `ADMIN_TOKEN` (admin routes; 503 if unset), `HF_TOKEN`/`HF_HOME` (model cache), `CORS_ORIGINS`, `AUTH_ALLOWED_PROVIDERS`, `ORPHAN_MAX_AGE_HOURS`, `ENVIRONMENT`, plus limits/knobs listed in `docs/DEPLOYMENT.md` section 6.

## Architecture (`app/`)
- `main.py` – entrypoint: pysqlite3 swap FIRST, CORS, `install_error_handlers` (request ids + `{"error": {...}}` envelope), mounts `/api/v1`, lifespan starts/stops the job runner and closes Chroma. `/health` = liveness only.
- `app/core` – `config` (env, no secrets in code), `security` (Supabase JWT via JWKS, asymmetric algs only, iss/aud/exp/role/sub), `dependencies` (Annotated aliases, lazy singletons, wiring), `exceptions`, `ratelimit`.
- `app/api/v1` – thin routes (plain `def`: blocking work runs in the threadpool). `app/schemas` – Pydantic, `extra="forbid"`, no `user_id`/`file_path` in any request.
- `app/repositories` – PostgREST. User-scoped client (anon key + the user's JWT => RLS). Service-role methods ALWAYS take and filter on `user_id`; cross-user queries exist only for maintenance and are named as such.
- `app/services` – `embedding`, `vector_store` (`VectorStore` protocol + `ChromaVectorStore`), `retrieval`, `pdf_extraction`, `ingestion`, `maintenance`, `rag_parts` (memory, rerank, context, citations), `llm` (`LLMProvider` + `GeminiService`), `orchestrator` (`prepare` / `answer` / `stream`).
- `app/workers` – `ThreadJobRunner` polls `processing_jobs` via RPCs (leases, backoff, janitor, startup reconcile). `app/prompts` – all prompt text.
- `supabase/migrations/20261125000000_pro_schema.sql` – tables, RLS, column grants, bucket policies, job RPCs. Never edit an applied migration; add a new one. (`20261124000000_init_books.sql` is the removed old version's migration, kept only for history.)

## Invariants (each one is covered by a test; do not break them)
- Heavy imports (torch/sentence_transformers, chromadb, fitz, langchain_google_genai) are lazy, inside functions. The `pysqlite3` swap stays the first block of `main.py`.
- The user id comes ONLY from the verified token. Foreign resources are `404`. Service-role code paths verify ownership first. Chroma filters are not a security boundary: hits are re-checked against Postgres-validated document ids.
- Storage paths are built server-side (`{user_id}/{document_id}/{file_name}`), pinned before any download; temp files never use user-supplied names. Uploaded objects are immutable (no UPDATE storage policy).
- Ingestion commits last: upsert all chunks, then delete stale ones, replace `document_chunks`, verify counts, compare-and-set `status=completed`. A failed rerun never destroys the previous index. Deterministic chunk ids make re-ingestion idempotent.
- Chroma collection metadata records the embedding model/dimension; a mismatch => `needs_reindex` (reads/writes refused) until `POST /api/v1/admin/reindex`. Never mix vectors of two models.
- RAG: scope comes only from the conversation; document text appears only inside `<sources>` in the user turn (tags neutralised); citations come only from `[Sn]` markers that map to chunks really in the prompt; an answer without a valid citation becomes a refusal; a turn (user+assistant+sources) is persisted only after success; the refusal sentinel is never streamed; `done.answer` is authoritative in streams.
- Errors: `/api/v1` responses use the envelope with `request_id` = `X-Request-ID`; 500s are generic (details only in logs); never `str(exc)` in a response.
- Secrets only from env; `.env`, `chroma_data/` stay uncommitted. One uvicorn worker (Chroma + in-process runner).

## Sign-in and user space
Google/Facebook through Supabase Auth (no login endpoints, no provider secrets here). `security._user_from_claims` rejects anonymous tokens (401) and providers outside `AUTH_ALLOWED_PROVIDERS` (403), using `app_metadata` only: `user_metadata` is user-editable, display only. Space limit is enforced by the storage INSERT policy (`can_upload()`, limits in `public.app_limits`). `DELETE /api/v1/me` deletes vectors, files, then the auth user LAST. Registration re-checks the limit. An aborted ingestion discards its vectors if the document is gone or `deleting`, and the job RPCs never revive a `deleting` document. The janitor removes unregistered uploads older than `ORPHAN_MAX_AGE_HOURS`. Spec: `docs/features/social_auth_and_user_space.md`.

## Working agreements
- Keep docs current in the same change: `README.md`, `docs/API.md`, `docs/DEPLOYMENT.md`, this file, and the status table in `docs/features/ebook_ai_pro_backend.md`.
- Add tests next to behavior changes (`tests/pro/`, offline: fake Supabase/Chroma/embedder/LLM, see `conftest_helpers.py`, `fakedb.py`, `fakechroma.py`, `fakejobs.py`).
- Known gaps (see the spec status): SQL integration tests never run, no real Chroma/model/Gemini verification, untuned RAG defaults, PyMuPDF is AGPL-3.0, no snapshot backup/OCR/Celery.
