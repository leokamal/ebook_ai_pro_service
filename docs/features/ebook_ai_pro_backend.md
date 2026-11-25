# E-Book AI Pro backend (per-user RAG + Multi-RAG)

Extend the existing `ebook_ai_service` into the E-Book AI Pro backend. Each user has a private PDF library in Supabase Storage. Users chat with one PDF (Personal RAG) or with their whole library (Personal Multi-RAG). Conversations, messages and citations are persisted. Hard constraints: ChromaDB is the only vector store (no pgvector or vector columns); Gemini is the LLM; Supabase provides Auth, Postgres and Storage; FastAPI on Render; Flutter is the only client. All legacy endpoints (`/query`, `/query-database/`, `/upload`, `/health`, `/generate-database/`, `/load-database/`) keep working with unchanged response shapes.

**This file started as the Phase 1 deliverable** (analysis and migration plan); the status section below tracks what was built. Implementation proceeds one phase at a time (see *Phases*). After each phase, `/do-feature` must stop, list created and modified files, run tests, list open issues and update docs before the next phase starts.

## Implementation status (updated after Phase 8 and the removal of the old version)

**Decision (owner): the product is E-Book AI Pro only.** The old single-library service (global Chroma index, `/query`, `/query-database/`, `/upload` OCR, `/generate-database/`, `/load-database/`, `rag_service.py`, `ocr_service.py`, `build.sh`, its tests and notebook) was deleted; it is recoverable from git history (`first init`). Every passage below that says legacy endpoints must keep working is superseded by this decision. The `books` SQL migration is kept only for migration-history consistency.

All eight phases are implemented. Operational documentation: [README](../../README.md), [DEPLOYMENT](../DEPLOYMENT.md), [API](../API.md). Offline tests: `pytest tests/pro` (no network, no model downloads); SQL-level tests: `tests/pro/integration` (need a real Postgres, **not yet run**).

| Phase | Status | Where |
|---|---|---|
| 1 Analysis | done | this document |
| 2 Supabase foundation | done | `supabase/migrations/20261125000000_pro_schema.sql`, `app/core/`, `app/repositories/` |
| 3 Chroma + embeddings | done | `app/services/{vector_store,embedding,retrieval}.py` |
| 4 Ingestion | done | `app/api/v1/documents.py`, `app/services/{pdf_extraction,ingestion,maintenance}.py`, `app/workers/` |
| 5 Personal RAG | done | `app/services/{orchestrator,rag_parts,llm}.py`, `app/prompts/`, `app/api/v1/{conversations,chat}.py` |
| 6 Multi-RAG | done | `app/api/v1/search.py`, `ContextBuilder(group=True)`, `RerankingService` |
| 7 API integration | done | `/chat/stream` (SSE), request ids, error envelope, OpenAPI contract tests |
| 8 Security + deployment | done (see gaps) | `tests/pro/test_isolation.py`, `tests/pro/integration/`, `Dockerfile`, `render.yaml`, README, DEPLOYMENT |

### Deviations from this spec (and why)
1. **Persistence order.** The user message is no longer stored before generation. A turn (user + assistant messages + sources) is stored only after a complete, grounded answer: a failed generation leaves nothing behind, retries cannot duplicate turns, history never has consecutive user turns. Consequently the SSE `meta` event carries only `conversation_id`; `done` carries `message_id`, `answer`, `refused`, `usage`.
2. **`SUPABASE_ANON_KEY` is required** by the backend (it forwards the user's JWT to PostgREST), not reference-only.
3. **Ungrounded answers become refusals.** An answer with no valid `[Sn]` citation is replaced by the "not found" reply (strict: tune after real-model testing).
4. **`done.answer` is authoritative** in the stream (refusals and stripped markers can differ from the streamed tokens); the refusal sentinel is never streamed.
5. **No UPDATE policy on `storage.objects`**: uploaded PDFs are immutable (clients upload with `upsert: false`).
6. **Added**: table `document_chunks`; per-user quotas (`MAX_DOCS_PER_USER`, `MAX_ACTIVE_JOBS_PER_USER`); rate limits on chat/search; `/admin/reindex` and `/admin/reconcile` (they run inside the request for now); `X-Request-ID`.
7. **Legacy removed** (owner decision, see above); config no longer reads `SUPABASE_SERVICE_KEY`/`GOOGLE_API_KEY`; CORS is env-driven (`CORS_ORIGINS`, credentials off); `/health` is a liveness probe.
7b. **Layout**: no `app/main.py` (the root `main.py` stays the entrypoint) and migrations live in `supabase/migrations/`.
8. Final review (Phase 7-8) hardening: per-user cap on concurrent streams + stream deadline, `LLM_MAX_OUTPUT_TOKENS`, auto-title through the service-role writer (the user-scoped client may be closed when a stream ends), container entrypoint that fixes volume ownership then drops to a non-root user, legacy modules also accept `SUPABASE_SERVICE_ROLE_KEY`/`GEMINI_API_KEY`, SQL storage test sets `storage.allow_delete_query` so the DELETE policy is really exercised.
9. `reindex_all` flags documents stale **before** resetting the collection and refuses while jobs are running (found in review).

### Known gaps / not verified (be honest before launch)
- SQL integration tests (RLS, column grants, storage policies, RPC privileges) were written but **never executed**; run them against a local Supabase.
- Real ChromaDB persistence/reopen behaviour, the real multilingual embedding model (Arabic/French quality), real Gemini calls and the Supabase Storage REST helpers were not exercised (the suite uses fakes; one real-Chroma test skips when `chromadb` is absent).
- `RAG_MIN_SCORE`, per-document caps, chunk size (1000/150) and the condense step are untuned defaults.
- Performance/memory figures in DEPLOYMENT are design bounds, not measurements.
- Not built: Chroma snapshot backup, OCR for scanned PDFs, Celery/Redis, per-user storage quota beyond document/job counts, background execution of reindex/reconcile.
- PyMuPDF is AGPL-3.0: confirm the licence is acceptable (or switch to `pypdfium2`).

## Current behavior / problems (Phase 1 analysis report)

Inspected: `main.py` (146 lines), `rag_service.py` (455), `ocr_service.py`, `supabase_storage.py`, `security.py`, `supabase/migrations/20261124000000_init_books.sql`, `docs/API.md`, `tests/`.

| Area | Today | Verdict for Pro |
|---|---|---|
| Routes | Flat in `main.py`. `/query` is SSE (`sources`, `token`\*, `done`/`error`). `/query-database/` returns `{"answer","context"}`. | Keep untouched. New routes go under `/api/v1`. |
| Auth | None for users. Only admin endpoints, via `security.verify_admin_token` (constant-time `X-Admin-Token`). | Keep for admin. Add Supabase JWT verification (new). Never reuse the admin token for users. |
| Firebase | Fully removed. Only a docstring in `supabase_storage.py` mentions it. | Nothing to migrate. |
| DB models | One table, `public.books` (public-read catalog, seeded). | Untouched. New tables are added in a new migration. |
| Storage | `supabase_storage.py` uses the service-role key and bucket `ebook-ai` (`books/`, `images/`, `chroma_db/`). `list_files` is recursive. `download_bytes` loads the whole object in RAM. | Reuse the client pattern. Add a streaming download to a temp file and an object-info call for the new bucket `user-documents`. Legacy bucket untouched. |
| Vector store | Global single-tenant Chroma in `CHROMA_DIR` via `langchain_chroma`, with only `file_name` metadata. Index is published to Supabase as files (`VERSION.json` last). | Not reusable for multi-user: no `user_id`/`page_number` metadata, whole-directory snapshot. Keep it as the legacy library index. Pro gets a separate Chroma directory and collection. |
| Embeddings | `sentence-transformers/all-MiniLM-L6-v2` (English-centric, 384d). The code already notes French scores poorly. | Wrong for Arabic and French. The Pro embedder is separate and configurable. |
| Chunking | PyPDF2 joins all pages into one string, then `RecursiveCharacterTextSplitter(chunk_size=1000)`. Page numbers are lost. | Unusable for citations. Pro splits per page with PyMuPDF. `langchain_text_splitters` is reused. |
| LLM | `get_llm()` caches `ChatGoogleGenerativeAI` with hardcoded `LLM_MODEL="gemini-3.5-flash"` and `GOOGLE_API_KEY`. No retry, timeout, quota handling or usage tracking. | Reuse the `langchain_google_genai` dependency behind a new `LLMProvider` interface. The legacy `get_llm()` is left alone. |
| Prompt | `PROMPT` forbids citing sources, because the app shows them separately. It already treats excerpts as untrusted. | Pro needs citation-aware prompts, in `app/prompts/`. The legacy prompt is unchanged. |
| Jobs | `/generate-database/` uses `BackgroundTasks`, so work is lost on restart and there is no status. | Not acceptable for ingestion. Pro uses a DB-backed queue. |
| Concurrency | `_lock` guards the in-memory swap. `_update_lock` serializes download and generate. | Pro uses its own locks and does not touch these. |
| Tests | pytest with fakes, no network. | Same style for Pro. |
| Reusable | `security.verify_admin_token`, `supabase_storage._client()` pattern, the traversal guard idea, the lazy-import and warm-up pattern, the SSE `sse()` helper, `_chunk_text()` for Gemini content, test fakes. | |

## Migration plan

New code lives in a package `app/` next to the legacy root modules. `main.py` stays the entrypoint (`uvicorn main:app` on Render). The `pysqlite3` swap stays at the very top, and `main.py` only adds `app.include_router(api_v1_router, prefix="/api/v1")` plus scoped exception handlers. There is **no `app/main.py`** (this adapts the requested layout). Importing the router must stay light (fastapi and pydantic only).

```
app/core/{config,security,dependencies,exceptions}.py
app/api/v1/{documents,conversations,chat,search,health,admin}.py
app/schemas/  app/repositories/  app/services/  app/workers/  app/prompts/  app/utils/
migrations → supabase/migrations/   (existing location, not a new top-level dir)
tests/pro/    Dockerfile    .env.example    README.md
```

Reuse (do not duplicate): extend `supabase_storage.py` with new functions instead of a second storage wrapper. Reuse `security.verify_admin_token` for admin routes. Legacy modules are not edited, except for registering the router in `main.py` and a lifespan hook that starts the worker.

## Phases (each ends with: summary, file list, test run, open issues, doc update)

1. **Analysis**: this document. No code changes.
2. **Supabase foundation**: migration SQL, RLS, storage policies, JWT verification, repositories, error model.
3. **Chroma + embeddings**: `VectorStore` interface and Chroma implementation, `EmbeddingService`, model-consistency guard.
4. **Ingestion**: `POST/GET/DELETE /documents`, `/process`, `/status`, `/retry`, the worker, extraction and chunking, progress, recovery on startup.
5. **Personal RAG**: orchestrator, Gemini service, memory, citations, `/chat`, `/conversations`.
6. **Multi-RAG**: multi-doc retrieval, diversity and dedup, `/search`.
7. **API polish**: SSE `/chat/stream`, OpenAPI examples, error model finalisation, `docs/API.md`.
8. **Security and deploy**: two-user isolation suite, Dockerfile, README, recovery drill, optional Chroma snapshot backup.

## Required changes

### A. Database (`supabase/migrations/<ts>_pro_schema.sql`, Phase 2)
- Tables, all with `id uuid default gen_random_uuid()` primary keys and `timestamptz` timestamps. `updated_at` uses a trigger.
  - `documents`: the requested fields, plus `index_state` (`none|indexed|missing|stale`), `embedding_model`, `chunk_count`.
    - `user_id → auth.users(id) on delete cascade`.
    - `status` check in (`uploaded`, `queued`, `processing`, `completed`, `failed`, `deleting`).
    - `file_type = 'application/pdf'` check.
    - `file_size > 0` and `<= 52428800`, matching `MAX_PDF_BYTES`.
    - `unique(user_id, file_path)`.
    - `check (file_path like user_id::text || '/' || id::text || '/%')`.
    - Index on `(user_id, created_at desc)`.
  - `conversations`: `mode` check in (`single_document`, `all_documents`).
    - `check ((mode='single_document') = (document_id is not null))`.
    - `document_id → documents on delete cascade`.
    - Index on `(user_id, updated_at desc)`.
    - A composite FK `(document_id, user_id) → documents(id, user_id)` stops a conversation from pointing at another user's document. `documents` needs `unique(id, user_id)`.
  - `messages`: `conversation_id → conversations on delete cascade`.
    - `role` check in (`user`, `assistant`, `system`).
    - `content` non-empty, `token_count >= 0`.
    - Index on `(conversation_id, created_at, id)`.
  - `document_chunks`: **added table**, with no text and no vectors. Columns: `id` (equals the Chroma id), `document_id`, `user_id`, `chunk_index`, `page_number`.
    - `unique(document_id, chunk_index)`, `unique(id, document_id)`.
    - It makes citations verifiable and recovery verifiable (compare PG count against Chroma count).
  - `message_sources`: `message_id → messages on delete cascade`, plus `document_id`, `chunk_id`, `page_number >= 1`, `relevance_score`, and a `document_title` snapshot.
    - `(chunk_id, document_id) → document_chunks(id, document_id)`, so a source can't reference a chunk of a different document.
    - Chunk and document links are `ON DELETE SET NULL (col)` (PG15+), so chat history survives document deletion or reindex. The `page_number` and `document_title` snapshots keep the citation readable.
    - `unique(message_id, chunk_id)`.
  - `processing_jobs`: the requested fields plus `run_after`, `locked_by`, `locked_until`, `progress` (0-100), `max_attempts` (default 3), `kind` (`ingest|reindex|delete_cleanup`).
    - `status` check in (`queued`, `running`, `completed`, `failed`).
    - Partial unique index on `(document_id)` where `status in ('queued','running')`, which gives duplicate prevention.
    - Index on `(status, run_after)`.
- RPC functions, `security definer`, `execute` granted to `service_role` only:
  - `enqueue_processing_job(document_id, user_id, kind)` sets the document to `queued` and inserts the job in one transaction. This is the transactional outbox.
  - `claim_processing_job(worker_id, lease_seconds)` uses `FOR UPDATE SKIP LOCKED`, takes `queued` jobs and `running` jobs whose lease expired, increments `attempts`, and fails the job and the document when `attempts > max_attempts`.
  - `heartbeat_job`.
  - `finish_job(job_id, ok, error)`.
- Run each migration in a transaction and keep it idempotent (`if not exists`, `drop policy if exists`), matching the existing migration style.
- The existing migration is not modified.

### B. RLS and storage (same migration)
- RLS is enabled on all new tables.
  - `documents`, `conversations`: owner policies `user_id = (select auth.uid())`.
  - `messages`: `exists` on the parent conversation owned by `auth.uid()`.
  - `message_sources`: `exists` through message to conversation.
  - `document_chunks`, `processing_jobs`: owner `select` only.
- Column privileges:
  - `revoke all` on the new tables from `anon`, and from `authenticated` except what is listed here.
  - `authenticated` gets `select` everywhere, `insert (…)` on `documents` limited to `id, user_id, title, file_name, file_path, file_type, file_size, language`, `update (title)` on `documents`, and `update (title)` and `delete` on `conversations`.
  - Status, error, page_count, index_state and all job columns are writable only by `service_role`.
  - Insert policy on `documents` requires `status = 'uploaded'`.
- Bucket `user-documents`: private, `file_size_limit` 50 MB, `allowed_mime_types = {application/pdf}`.
  - Policies on `storage.objects` for `select`, `insert`, `update` and `delete` require `bucket_id='user-documents' and (storage.foldername(name))[1] = (select auth.uid())::text`.
  - No policy for `anon`.
- Key roles:
  - **Anon/publishable key**: Flutter only, grants nothing without a user session.
  - **User access token**: sent by Flutter in `Authorization: Bearer` to this API, verified here. Forwarded to PostgREST and Storage for user-scoped operations, so RLS applies.
  - **Service-role key**: backend env only, used only by the worker and by the existence and size checks on storage objects. Every service-role code path must first load the row with the caller's user id and check ownership (assertion in the repository method signature, e.g. `get_owned(document_id, user_id)`).
  - **`SUPABASE_DB_URL`**: used only by migration and ops scripts (e.g. `scripts/`) and the RLS integration tests. The request path uses PostgREST. Never exposed.

### C. Auth (`app/core/security.py`, Phase 2)
- `get_current_user` dependency:
  - Parse `Authorization: Bearer`.
  - Verify with PyJWT, signature through the JWKS at `SUPABASE_JWT_JWKS_URL` (default `{SUPABASE_URL}/auth/v1/.well-known/jwks.json`), cached with TTL and a refresh on unknown `kid`.
  - Allowed algs are asymmetric only (`RS256`, `ES256`, `EdDSA`). `none` and `HS*` are rejected.
  - Require `exp`, `sub` (UUID), `iss == {SUPABASE_URL}/auth/v1`, `aud == "authenticated"`, and `role == "authenticated"` (this rejects the anon key, which is itself a JWT). 30 s leeway.
  - Returns `AuthUser(id, access_token)`.
- Optional legacy `SUPABASE_JWT_SECRET` (HS256) is accepted only if set, and never when a `kid` is present in the header, to avoid algorithm confusion. See open questions.
- `user_id` fields in request bodies or query strings are not part of any schema. Unknown fields are rejected by Pydantic (`extra="forbid"`).
- Failures return 401 with a `WWW-Authenticate: Bearer` header and the standard error body. Details are never leaked.

### D. Chroma service (`app/services/vector_store.py`, Phase 3)
- `VectorStore` protocol: `upsert_chunks`, `delete_document`, `query(embedding, user_id, document_ids, top_k)`, `count(document_id)`, `health`, `reset_collection`.
- `ChromaVectorStore` implements it with `chromadb.PersistentClient(path=CHROMA_PERSIST_DIRECTORY)`, imported lazily.
  - Collection `ebook_ai_pro_documents`, cosine space, created with metadata `{embedding_model, embedding_dimension}`.
  - Embeddings are always supplied by `EmbeddingService`, never by a Chroma embedding function.
  - Chunk metadata: `user_id`, `document_id`, `document_title`, `page_number`, `chunk_index`, `language`, `index_run` (job id), `embedding_model`, `char_count`.
  - Upsert in batches of `EMBED_BATCH` (32). Ids are deterministic: `uuid5(document_id, chunk_index)`, so re-ingestion is idempotent.
  - Query filter is `$and[user_id, document_id $in <ownership-validated ids>]`. Results are post-filtered in code: any hit whose metadata `user_id` or `document_id` doesn't match the validated set is dropped and logged as a security event. The metadata filter is not the security boundary; the PG ownership check plus the post-filter is.
- Model guard: on open, compare collection metadata `embedding_model` and `embedding_dimension` with config.
  - On mismatch, the store enters `needs_reindex`. Writes and reads are refused with `index_unavailable`, `/health` reports it, and no mixed vectors are ever written.
  - `POST /api/v1/admin/reindex` (admin token) recreates the collection with the new metadata, marks all `completed` documents `index_state='stale'`, and enqueues `reindex` jobs. Documents remain `completed` but are unsearchable until reindexed.
- Chroma runs in-process (single uvicorn worker). It isn't a network service, which is what "not exposed" means here, and it sits behind the `VectorStore` protocol so it can later move to a server.

### E. Embeddings (`app/services/embedding.py`, Phase 3)
- `EmbeddingService`: `embed_documents(list[str])` in batches, `embed_query(str)`. The model is loaded lazily and cached (`lru_cache`), using `sentence_transformers` with `normalize_embeddings=True`.
- Config is `EMBEDDING_MODEL` and `EMBEDDING_DIMENSION`, validated against the loaded model's real dimension at load time (hard error on mismatch).
- Default recommendation: **`paraphrase-multilingual-MiniLM-L12-v2`** (≈118M params, 384d, ~0.5 GB RAM, covers ar/fr/en) or `intfloat/multilingual-e5-small` (needs `query:`/`passage:` prefixes, handled via a per-model prefix map).
- **`BAAI/bge-m3`** is the best quality (1024d, 100+ languages, 8k tokens) but ≈568M params, about 2.3 GB in fp32. It does not fit alongside the legacy stack on a 1-2 GB Render instance. It is supported by config and recommended only for a 4 GB+ instance. The evaluation table goes in the README.
- Pro and legacy embedders are separate and never share a collection.

### F. Ingestion (`app/workers/`, `app/services/{pdf_extraction,chunking,ingestion}.py`, Phase 4)
- `POST /documents` (body `document_id`, `title`, `file_name`, optional `language`):
  - `file_path` is derived server-side as `{user_id}/{document_id}/{sanitized_file_name}`. The client never supplies it.
  - The file name must have no `/`, `\`, `..` or control characters.
  - Using the service role and an explicit path, check the object exists, size ≤ `MAX_PDF_BYTES`, and the first bytes are `%PDF-`.
  - Insert the row through the user's JWT client (RLS).
  - Idempotent: the same `document_id` and same owner returns 200 with the existing row. A collision with another user's id returns 409 with a generic message.
  - Optional `auto_process` (default `true`) calls the enqueue RPC.
- `/process`: allowed from `uploaded` only, calls `enqueue_processing_job`, returns 202.
- `/retry`: allowed from `failed` only, resets attempts.
- Anything else returns 409.
- `DELETE`: set `deleting`, then delete Chroma chunks, then the storage object, then the row (cascade). A janitor step in the worker loop finishes any document stuck in `deleting`. Returns 202.
- `/status`: `{status, index_state, progress, attempts, error_message, page_count, chunk_count}`.
- Worker (`app/workers/runner.py`):
  - A daemon thread started from the lifespan hook, with concurrency 1 (memory).
  - Loop: `claim_processing_job`, process, `finish_job`. Sleeps `WORKER_POLL_SECONDS` (default 5) when idle. Heartbeat every 30 s extends the lease (`JOB_LEASE_SECONDS`, default 300).
  - Crash or sleep recovery happens through lease expiry, so no job is lost when the free tier sleeps.
  - Interface `JobRunner` (`start`, `stop`) and `JobHandler` (`handle(job)`) so Celery or Redis can replace the loop later.
  - Graceful shutdown on lifespan exit.
- Pipeline (`ingestion.process_document(job)`):
  1. Load the document with `get_owned(document_id, job.user_id)`. Assert `file_path` starts with `{user_id}/{document_id}/`.
  2. Stream the object to a temp file. Never hold a large PDF in RAM.
  3. Open with PyMuPDF. An encrypted or corrupt file is a permanent failure (`corrupt_pdf`), and a page limit `MAX_PDF_PAGES` (default 1000) also fails permanently.
  4. Extract page by page. Normalize with NFKC, remove control characters, collapse whitespace, and de-hyphenate only for Latin scripts.
  5. Split each page separately with `RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=150)`, so every chunk has exactly one `page_number`.
  6. Zero chunks means permanent failure `no_extractable_text` ("scanned PDF; OCR not supported yet").
  7. Embed and upsert in batches, updating `progress`.
  8. After all batches: delete chunks of this document whose `index_run != job.id`, replace `document_chunks` rows, then set `documents` to `completed`, `index_state='indexed'`, `chunk_count`, `page_count`, `embedding_model`. This commit-last order mirrors the legacy VERSION.json-last rule: a failed rerun never removes the previously good index.
  9. `finally`: delete the temp file.
- Transient errors (network, Supabase 5xx) are retried with backoff (`run_after = now + 2^attempts × 30 s`) up to `max_attempts`. Permanent errors fail immediately. `error_message` is user-safe text, and details go to logs only.
- Heavy imports (`fitz`, `sentence_transformers`, `chromadb`) are lazy, inside functions.

### G. Recovery (Phase 4 and Phase 8)
- On worker start, and via `POST /api/v1/admin/reconcile` (admin token), compare PG with Chroma for each `completed` document.
  - If the Chroma count for the document differs from `documents.chunk_count`, or the collection is empty or missing, set `index_state='missing'` and enqueue a `reindex` job.
  - While `missing` or `stale`, the document is excluded from retrieval and reported in `/status`, and chat in `single_document` mode on it returns 409 `document_not_indexed`.
- Verification after reindex: Chroma count equals `chunk_count` equals `document_chunks` row count, otherwise the job fails.
- Free-hosting reality: Render's free tier has an ephemeral disk and sleeps. Recommended config is a paid instance of at least 2 GB with a persistent disk mounted at `CHROMA_PERSIST_DIRECTORY`. Without a disk, every restart triggers a full re-embed on CPU, which is workable only for small libraries.
- Optional (Phase 8, see open questions): periodic snapshot of the Chroma directory to a backend-only Supabase bucket, reusing the "manifest uploaded last" pattern.

### H. RAG services (`app/services/`, `app/prompts/`, Phases 5-6)
- `RAGOrchestrator.answer(user, conversation_id, message)` is the single path for `/chat` and `/chat/stream`. It uses the services you named:
  1. Load the conversation through the user JWT client (RLS). A missing or foreign conversation returns 404.
  2. Scope comes **only** from the conversation: `single_document` uses its `document_id`, `all_documents` uses the user's documents.
  3. Resolve the allowed document ids from PG. Single: owned, `completed`, `index_state='indexed'`, else 409. Multi: all owned `completed` and indexed. If there are none, return the fixed not-found answer without calling Gemini.
  4. `ConversationMemoryService` takes the last `HISTORY_MESSAGES` (default 6) within a token budget. When history exists and `RAG_CONDENSE_QUESTION` is true, one cheap Gemini call rewrites the follow-up into a standalone query, falling back to the raw message on any failure.
  5. `QueryEmbeddingService` embeds the standalone query. `ChromaRetrievalService` calls the `VectorStore` with `top_k=RAG_CANDIDATES` (default 20).
  6. `RerankingService` (MVP, no cross-encoder because of RAM) applies an optional `RAG_MIN_SCORE`, removes near-duplicates (normalized text overlap above 0.9), and caps `RAG_MAX_PER_DOC` (default 3) per document for multi mode. It keeps `RAG_CONTEXT_CHUNKS` (default 6) in order of score. The interface allows swapping in a cross-encoder later.
  7. `ContextBuilder` numbers the chunks `[S1]…[Sn]`, each with its document title and page, inside a delimited `<sources>` block.
  8. `GeminiService.generate` or `.stream` runs with a system prompt, history and context. PDF text is declared untrusted data and never placed in the system or instruction role.
  9. `CitationService` parses `[Sn]` markers in the answer. Unknown markers are stripped. `sources` is built only from markers that map to chunks actually in the context, so there are no fabricated citations. A refusal (not-found answer) has an empty `sources`.
  10. Persist the user message before generation, and the assistant message plus `message_sources` after success. A failure persists no assistant message. A client disconnect mid-stream aborts without saving a partial assistant message.
- Prompts live in `app/prompts/*.py` as templates (grounding, language mirroring ar/fr/en, citation format, untrusted-content rule, not-found sentence). No prompt text sits in the services.
- `LLMProvider` protocol (`generate`, `stream`, returns `LLMResult{text, usage}`). `GeminiService` wraps `langchain_google_genai`.
  - Config `GEMINI_MODEL`, `LLM_TIMEOUT_SECONDS` (default 60), and `LLM_MAX_RETRIES` (default 2) with exponential backoff on 5xx and timeouts only.
  - A 429 or quota error maps to `LLMQuotaError`, which becomes HTTP 429 `llm_quota_exceeded`, and is never retried inside the request.
  - Usage metadata fills `messages.token_count` (output tokens) and is logged.
- A summary request ("document summaries") is served by the same chat path using a `summarize` hint. Dedicated summary endpoints and map-reduce are not built.

## API / contract changes

All new routes are under `/api/v1`, need `Authorization: Bearer <supabase access token>` (except `health`), and are backward compatible: no legacy route changes.

- Error body for `/api/v1` only: `{"error": {"code": "string", "message": "string", "request_id": "string"}}`. Codes include `unauthorized`, `forbidden`, `not_found`, `validation_error`, `conflict`, `document_not_indexed`, `index_unavailable`, `llm_quota_exceeded`, `llm_unavailable`, `payload_too_large`, `internal_error`. Exception handlers check `request.url.path.startswith("/api/v1")` and delegate to the default handlers otherwise, so legacy `{"detail": ...}` shapes are unchanged. Resources that belong to another user return 404, not 403.
- Pagination: `?limit=` (1-100, default 20) and `?cursor=` (opaque, keyset on `(created_at, id)`). Response `{"items": [...], "next_cursor": string|null}`.

| Endpoint | Success | Notes |
|---|---|---|
| `POST /documents` | 201 (200 if existing) | body above; returns the document |
| `GET /documents` | 200 | paginated, optional `status` filter |
| `GET /documents/{id}` | 200 | |
| `DELETE /documents/{id}` | 202 | async deletion |
| `POST /documents/{id}/process` | 202 | from `uploaded` only |
| `GET /documents/{id}/status` | 200 | |
| `POST /documents/{id}/retry` | 202 | from `failed` only |
| `POST /conversations` | 201 | `{mode, document_id?, title?}`; validates the mode and document pairing and document ownership |
| `GET /conversations`, `GET /conversations/{id}` | 200 | |
| `PATCH /conversations/{id}` | 200 | `title` only |
| `DELETE /conversations/{id}` | 204 | cascades |
| `GET /conversations/{id}/messages` | 200 | paginated; each assistant message has `sources[]` |
| `POST /chat` | 200 | `{conversation_id, message (1-4000), summarize?: bool}` → `{message_id, answer, sources[], usage}` |
| `POST /chat/stream` | 200 `text/event-stream` | same request body |
| `POST /search` | 200 | `{query, mode, document_id?, top_k (1-20)}` → `{results:[{chunk_id, document_id, document_title, page_number, score, text}]}`. Retrieval only, no LLM. |
| `GET /health` | 200 / 503 | no auth; `{status, chroma, embedding_model, worker, queue_depth, index_state}` |
| `POST /admin/reindex`, `POST /admin/reconcile` | 202 | **additions**, `X-Admin-Token` |

- `sources[]` item: `{document_id, document_title, page_number, chunk_id, relevance_score, snippet}`.
- SSE events, same style as legacy:
  - `meta` `{conversation_id, user_message_id}`
  - `token` `{text}`
  - `sources` `{sources:[…]}`, sent after generation, because citations need the full text
  - `done` `{message_id, usage}`
  - `error` `{code, message}`
- `X-Accel-Buffering: no` and `Cache-Control: no-cache` are sent as in the legacy stream.
- All routes get OpenAPI summaries, response models and error responses (`responses={…}`).

## Configuration

`.env.example` lists the variables you named: `SUPABASE_URL`, `SUPABASE_ANON_KEY` (reference only, the backend does not need it), `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_JWT_JWKS_URL`, `SUPABASE_DB_URL`, `GEMINI_API_KEY`, `GEMINI_MODEL`, `EMBEDDING_MODEL`, `EMBEDDING_DIMENSION`, `CHROMA_PERSIST_DIRECTORY` (default `./chroma_data`), `CHROMA_COLLECTION_NAME` (default `ebook_ai_pro_documents`), `SUPABASE_STORAGE_BUCKET` (default `user-documents`), `ENVIRONMENT`. Placeholders only.

Added: `MAX_PDF_BYTES`, `MAX_PDF_PAGES`, `WORKER_ENABLED` (default true), `WORKER_POLL_SECONDS`, `JOB_LEASE_SECONDS`, `HISTORY_MESSAGES`, `RAG_*` knobs, `LLM_TIMEOUT_SECONDS`, `LLM_MAX_RETRIES`.

`app/core/config.py` reads the new names and falls back to the legacy ones (`GOOGLE_API_KEY`, `SUPABASE_SERVICE_KEY`). That way one Render service can run both stacks. Legacy `CHROMA_DIR` and `CHROMA_PERSIST_DIRECTORY` must be different directories (startup check). If the Pro variables are absent, `/api/v1` returns 503 `index_unavailable` and the legacy service is unaffected.

New dependencies: `PyMuPDF`, `PyJWT[crypto]`, `chromadb` (already present transitively), `httpx` (already via supabase). `requirements-dev.txt` unchanged. Note on licensing: PyMuPDF is AGPL-3.0 (commercial licence available). Confirm that is acceptable for this product (open question).

Cost summary for the README: Supabase free tier (quota-limited: 500 MB DB, 1 GB storage); Gemini free quota (rate limited, quota errors are expected); embedding model and ChromaDB are free but need RAM; Render persistent disk and a 2 GB instance are the paid part.

## Risks and invariants

- Lazy heavy imports: `fitz`, `sentence_transformers`, `chromadb`, `langchain_google_genai`, `jwt` crypto backends are all imported inside functions. Verified by a test that imports `main` and asserts none of them is in `sys.modules`.
- `pysqlite3` swap stays first in `main.py`. The worker thread and Chroma open only after the swap (lifespan hook).
- Legacy index locks (`_lock`, `_update_lock`) and the legacy "queries never call Supabase" rule are untouched. Pro queries call Supabase (ownership checks, persistence) by design, but never the legacy `rag_service`. Pro Chroma writes are serialized by a `threading.Lock` in `ChromaVectorStore`, and reads are lock-free.
- Publish order: the Pro equivalent is "new chunks and chunk rows first, document `completed` last, stale chunks deleted only after success".
- Path safety: the storage path is built server-side and asserted against the `{user_id}/{document_id}/` prefix before any download. Temp files use `tempfile` with generated names, and the user file name is never used as a local path. The legacy traversal guard is unchanged.
- Secrets only from env. The service-role key is never logged or returned, and is never used on a code path that has not first done an ownership check. Admin routes use `verify_admin_token`.
- Gitignore: add `chroma_data/` and `.env` stays. `chroma_db_local/` and `config/` stay ignored.
- Memory: legacy MiniLM (~100 MB) plus the Pro embedder (~0.5 GB), and Gemini calls, fit in 2 GB only with worker concurrency 1 and 32-chunk batches. The Pro embedder loads lazily on first use (not in legacy warm-up), so the cold-start time of legacy endpoints is unchanged.
- Single process: Chroma `PersistentClient` is not multi-process safe, so the service must run one uvicorn worker (documented in the Dockerfile and README).
- Free tier sleeps: the worker stops. Lease expiry resumes jobs after wake-up. Document this and recommend a ping on `/api/v1/health` (it must not load the embedding model).
- CORS stays `*` (existing known gap). Bearer-token auth does not rely on cookies. Tightening is out of scope here.

## Acceptance criteria

- Legacy endpoints return identical responses before and after (existing tests still pass unchanged).
- A token that is expired, has a bad signature, wrong `iss`, wrong `aud`, `role=anon`, `alg=none`, or HS256-signed with the public key is rejected with 401.
- With two users A and B: B receives 404 for A's document, conversation, messages and job on every endpoint. B's `/search` and `/chat` never return A's chunks even when A's `document_id` is forced into a request. A tampered Chroma metadata filter result is dropped by the post-filter.
- A user can't set `status`, `error_message`, job fields or another `user_id` through PostgREST (RLS and column privileges, verified in the integration test).
- Flutter can upload only under its own `{auth.uid()}/` folder, and can't read another user's objects.
- Uploading a valid PDF and calling `/process` leads to `completed` with `chunk_count > 0`. Every chunk has the right `page_number`. Re-running ingestion gives the same ids and no duplicate vectors. A second `/process` while queued or running returns 409.
- An empty-text PDF, a corrupt PDF, and an oversized PDF fail with the right `error_message` and are not retried. A transient failure is retried up to `max_attempts`. A worker killed mid-job is resumed by another claim after the lease expires.
- Changing `EMBEDDING_MODEL` makes the store refuse reads and writes until `/admin/reindex` has run. Vectors of two models are never in the same collection.
- Deleting Chroma storage, then running reconcile, marks documents `missing`, reindexes them, and `/search` returns results again. Counts match.
- Personal RAG only retrieves from its document. Multi-RAG retrieves from all of the user's completed documents and cites document and page. Every returned source maps to a chunk that was in the prompt context. A question outside the documents returns the not-found answer with empty `sources`. A prompt-injection string inside a PDF does not change behavior (golden-prompt check that the text is only inside the `<sources>` block).
- A follow-up question ("and what about the second one?") uses history. A Gemini 429 returns HTTP 429 `llm_quota_exceeded` and no assistant message is stored.
- `/chat/stream` emits `meta`, `token*`, `sources`, `done`, or `error`, and the persisted assistant message equals the concatenated tokens.
- Importing `main` pulls in none of the heavy modules.

## Tests

All run offline with no model downloads (`pytest`, markers noted). Fakes: `FakeEmbedder` (deterministic hash vectors), `FakeLLM` (scriptable, can raise quota errors), in-memory repositories implementing the same interfaces, a fake storage. Real `chromadb` is used in `tmp_path` (no network, no model) and the tests skip if it can't be imported (`importorskip`). Place them under `tests/pro/`.

- `test_jwt.py`: generates an RSA key pair and a local JWKS; covers valid, expired, wrong iss, wrong aud, anon role, `alg=none`, HS256 confusion, unknown `kid` refresh, malformed header.
- `test_isolation.py`: users A and B. Documents, conversations, messages, `/search` and `/chat`; forced foreign `document_id`; metadata-post-filter drop.
- `test_vector_store.py`: upsert idempotency, delete document, filters, batch sizes, model-mismatch guard, count and health.
- `test_ingestion.py`: generates PDFs in-test with PyMuPDF (text, empty, corrupt, encrypted, many pages); page numbers, chunk ids, progress, temp file cleanup, duplicate `/process`, retry/backoff, lease-expiry takeover, commit-last ordering (failure at batch N leaves the old index intact).
- `test_recovery.py`: wipe the Chroma dir, reconcile, reindex, count verification, reindex on embedding-model change.
- `test_rag.py`: personal and multi modes, per-doc cap, dedup, citation validation (unknown markers stripped, no sources on refusal), history and condense fallback, injection fixture, Gemini quota and timeout, persistence rules, SSE event order.
- `test_errors_and_legacy.py`: `/api/v1` error body shape, legacy `{"detail"}` shape unchanged, lazy-import check.
- `tests/pro/integration/` (marker `integration`, skipped unless `SUPABASE_TEST_DB_URL` is set): SQL-level RLS, column privilege and storage policy checks as users A and B against a local Supabase (`supabase start`) or a throwaway project.

## Out of scope

- Legacy modules and endpoints: `rag_service.py`, `ocr_service.py`, the legacy Chroma publish/download flow, `books` table, admin endpoints.
- OCR for scanned PDFs (EasyOCR exists, but is not wired into ingestion). Non-PDF files. Sharing documents between users. Cross-encoder reranking. Redis and Celery (the interfaces allow them later). A hosted Chroma server. Dedicated summary pipeline. CORS tightening. Flutter code.

## Assumptions / open questions

Assumptions made (change before `/do-feature` if wrong):
1. Pro lives alongside the legacy service in the same repo, process and Render service, with a separate Chroma directory.
2. The default embedder is a small multilingual model (not bge-m3) because of the 1-2 GB RAM limit. bge-m3 is config-selectable.
3. The `document_chunks` table is added (not in your list) so source and recovery consistency can be enforced in SQL without storing text or vectors.
4. Flutter generates `document_id` and uploads first, then calls `POST /documents`. Auto-process is on by default.
5. In-process worker thread with a DB queue (`SKIP LOCKED` and leases) is the MVP dispatcher.
6. Citations use `[Sn]` markers, and `sources` is sent after generation in the stream.
7. Deleting a document keeps chat history, with citations degraded to snapshots.
8. New env names are primary, with fallback to the legacy names.

Open questions:
- **PyMuPDF is AGPL-3.0.** Is that acceptable for the product, or should we use `pypdfium2` (Apache/BSD) instead?
- Do you want the optional Chroma snapshot backup to Supabase Storage in Phase 8, for hosting without a persistent disk?
- Does the project use Supabase's new asymmetric JWT signing keys (JWKS) or the legacy HS256 shared secret? This decides whether `SUPABASE_JWT_SECRET` support is needed.
- What PDF limits do you want (default 50 MB, 1000 pages)?
