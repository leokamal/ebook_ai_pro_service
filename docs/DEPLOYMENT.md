# Deployment, operations and recovery (E-Book AI Pro)

Audience: whoever deploys and operates the service. Contract for the app: [API.md](API.md). Design: [features/ebook_ai_pro_backend.md](features/ebook_ai_pro_backend.md).

## 1. Topology

One container, **one uvicorn worker**: FastAPI + the in-process job runner + an in-process ChromaDB (`PersistentClient`). Chroma is never exposed on a port, so Flutter and the internet cannot reach it. Stateful data:

| Data | Where | Lost if the disk is lost? |
|---|---|---|
| Users, documents, conversations, messages, citations, job queue | Supabase Postgres | no |
| Original PDFs | Supabase Storage `user-documents` | no |
| **Pro vector index** | `CHROMA_PERSIST_DIRECTORY` (disk) | yes, but rebuildable (section 4) |
| Embedding model cache (local provider only) | `HF_HOME` (disk) | yes, re-downloaded |

More than one worker/replica is **not supported** (Chroma's SQLite files and the job runner assume a single process; extra replicas would also each reindex). Scale vertically.

## 2. Hosting options (be realistic)

The RAM sizes below assume the **local** embedding model (`EMBEDDING_PROVIDER=huggingface`). With the default Gemini provider torch is not installed and the process should need much less (**not measured yet**: check before choosing a smaller instance). The persistent disk is needed either way.

| Option | Persistence | Cold start | Verdict |
|---|---|---|---|
| Render free web service | **ephemeral disk**, sleeps after ~15 min idle, ~512 MB RAM | minutes (model + index) | Demo only. Torch + the embedding model need roughly 1-2 GB. Every restart/sleep wipes Chroma, so the whole library is re-embedded on CPU. Not suitable for real users. |
| Render paid web service (>= 2 GB RAM) + persistent disk | disk survives restarts and deploys | seconds (index on disk) | **Recommended MVP**: `render.yaml` in the repo. Check current plan sizes and prices. |
| Any VPS / Fly.io machine + volume (2 GB+) | volume | seconds | Equivalent, cheaper at small scale; use the Dockerfile and mount a volume at `/data`. |
| Free tier + snapshot backup of the Chroma directory to Storage | rebuild-from-snapshot | minutes | **Not implemented** (open question in the spec). The documented recovery (section 4) re-embeds from the PDFs instead. |

Free vs paid components: Supabase free tier (quota-limited: DB size, 1 GB storage, bandwidth, projects pause after inactivity), Gemini API free quota (rate/day limited, separate for answers and embeddings; errors surface as `429 llm_quota_exceeded` / `embedding_quota_exceeded`), ChromaDB and the optional local model (free, cost = RAM/CPU), Render persistent disk and the larger instance (the paid part). Check each provider's current limits before launch.

## 3. Configure and deploy

1. Create the Supabase project and apply the migrations ([README](../README.md#supabase-setup-once-per-project)), in filename order. `20261126000000_user_space.sql` is required: it holds the only Storage upload policy.
2. Environment (see `.env.example`; **placeholders only are committed**). Required: `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`, `GEMINI_API_KEY`, `ADMIN_TOKEN`. With the local embedding provider also: `HF_TOKEN` (higher Hugging Face rate limits), `HF_HOME` on the disk. Defaults: bucket `user-documents`, collection `ebook_ai_pro_documents`, embeddings `gemini-embedding-2` (768 dimensions).
3. Docker: `docker build -t ebook-ai . && docker run --env-file .env -p 8000:8000 -v ebook_ai_data:/data ebook-ai`. Render: connect the repo, use `render.yaml` (Docker runtime, disk at `/data`), fill the secret env vars.
   **Render native Python runtime** (a service created by hand rather than from `render.yaml`): `.python-version` pins Python 3.11; set the environment variables in the dashboard. Without a persistent disk at `CHROMA_PERSIST_DIRECTORY` the index is lost on every deploy.
4. Smoke test:
   - `GET /health` -> `{"status":"ok"}` (liveness); `GET /api/v1/health` -> `{"status":"ok","worker":"running","vector_index":{...}}` (503 `unconfigured` if a key is missing, 503 if the index needs a reindex).
   - With a real user token: register a PDF, poll `/documents/{id}/status` until `completed`, `POST /api/v1/search`, `POST /api/v1/chat`.
5. Keep-alive pings (paid instances do not sleep): not needed. If you must use a sleeping tier, ping `/health` (it never loads the embedding model).

Container: the entrypoint starts as root only to `chown` the mounted disk (providers mount it root-owned), then drops to the unprivileged `app` user with `gosu`.

Startup/shutdown: the lifespan hook starts the job runner (no model loaded, Chroma not opened until first use or the startup reconcile), and on shutdown stops the runner and closes Chroma. Heavy imports are lazy, so the port binds quickly.

### Embedding providers

`EMBEDDING_PROVIDER` selects the strategy (`app/services/embedding/`). Each provider has its own model variables, so switching changes one variable (plus the install, once).

| Provider | Variables (defaults) | Install | Notes |
|---|---|---|---|
| `gemini` (default) | `GEMINI_EMBEDDING_MODEL` (`gemini-embedding-2`, or the text-only `gemini-embedding-001`; changing it needs a reindex; each has its strategy in `app/services/embedding/gemini_models.py`), `GEMINI_EMBEDDING_DIMENSION` (768; 128-3072) | `requirements.txt` | uses `GEMINI_API_KEY`; **all document text and queries are sent to Google** |
| `huggingface` | `EMBEDDING_MODEL` (`paraphrase-multilingual-MiniLM-L12-v2`), `EMBEDDING_DIMENSION` (384) | `requirements-huggingface.txt`; Docker `--build-arg INSTALL_HUGGINGFACE=true` | runs on the server's CPU and RAM |

Local models:

| Model | Size / RAM (CPU, fp32) | Dim | Notes |
|---|---|---|---|
| `paraphrase-multilingual-MiniLM-L12-v2` | ~118M params, ~0.5 GB | 384 | ar/fr/en, fits a 2 GB instance |
| `intfloat/multilingual-e5-small` | ~118M, ~0.5 GB | 384 | uses `query:`/`passage:` prefixes (handled automatically) |
| `BAAI/bge-m3` | ~568M, ~2.3 GB | 1024 | best quality, long inputs; needs a 4 GB+ instance |

**Gemini quotas and privacy.** Check the embedding limits of your project in Google AI Studio and the data-use terms of your tier (free-tier content may be used by Google to improve its products) before real users upload private files. During ingestion a `429`/`5xx` is retried inside the job (5, 15, 30, 60 s), then the job is retried with backoff; a retried job re-embeds the document from the start, so a large book can fail repeatedly on a small quota. Searches and chat questions never wait for quota: they return `429 embedding_quota_exceeded`.

**Changing the provider or model**: set the variables, redeploy. The store detects the mismatch and enters `needs_reindex` (`/api/v1/health` returns 503, search/chat return `503 index_unavailable`, nothing is written). Then run `POST /api/v1/admin/reindex` (header `X-Admin-Token`) while no job is running: documents are flagged `stale` first, the collection is recreated for the new model, and rebuild jobs are queued. Documents become searchable again one by one as they finish. With Gemini, re-indexing a whole library spends quota. Check quality on your real Arabic/French content by hand and tune `RAG_MIN_SCORE` afterwards (scores differ between models).

## 3b. Sign-in providers and user space (one-time setup, dashboard work)

1. **Supabase → Authentication → Providers**: enable **Google** and **Facebook**; **disable** anonymous sign-ins and e-mail/password signups; **enable manual linking** (needed for `linkIdentity`).
2. **Google Cloud**: configure the OAuth consent screen (scopes `openid email profile`), create a *Web* OAuth client with authorised redirect URI `https://<project-ref>.supabase.co/auth/v1/callback`, and paste the client id and secret into the Supabase Google provider.
3. **Meta for Developers**: create an app with the Facebook Login product, add the same callback URL as a valid OAuth redirect URI, request `email` and `public_profile`, provide a privacy policy URL and a **data deletion instructions URL**, switch the app to **Live**, and paste the app id and secret into Supabase.
4. **Supabase → Authentication → URL configuration**: add `<app-scheme>://login-callback` (and any web origin) to the redirect allow list. Keep asymmetric JWT signing keys.
5. Apply `supabase/migrations/20261126000000_user_space.sql`, then `20261127000000_profiles.sql`.
6. Provider secrets live only in the Supabase dashboard, never in this repository or on the API host. `AUTH_ALLOWED_PROVIDERS` (default `google,facebook`) makes the API refuse other providers; set it empty only for local development. The database and Storage enforce the same rule through `public.app_limits.allowed_providers`, so change both together (e.g. when adding `apple`): `update public.app_limits set allowed_providers = array['google','facebook','apple'];`
7. **iOS**: Apple's App Store guidelines generally require an equivalent privacy-preserving login (usually Sign in with Apple) when Google or Facebook login is offered. If you ship on iOS, enable the Apple provider and add `apple` to `AUTH_ALLOWED_PROVIDERS`.

Text to publish as the data deletion instructions: "Open the app, go to Account, choose Delete account and confirm. Your account, uploaded PDFs, search index and chat history are permanently deleted." (Add a contact address for people who can no longer sign in.)

**Space limits** live in the database: `update public.app_limits set max_storage_bytes = 104857600, max_objects = 220, max_documents = 200, max_conversations = 1000;` (takes effect immediately). The last two are row limits enforced by database triggers, so direct PostgREST inserts cannot pass them; raising `MAX_DOCS_PER_USER` above `max_documents` has no effect. The limit is checked before each upload, so one file can overshoot it, and parallel uploads or signed upload URLs created while under the limit can exceed it further; the API refuses to register such files and the cleanup removes them after `ORPHAN_MAX_AGE_HOURS`. It is a soft cap on raw storage and a hard-enough cap on indexed content. Supabase's free tier has about 1 GB of storage in total: plan the per-user limit accordingly.

**After account deletion** an already issued access token stays valid until it expires (about an hour). API writes then fail (the user no longer exists), but a direct storage upload could still succeed; the hourly orphan cleanup removes it after `ORPHAN_MAX_AGE_HOURS` (24).

**Check once on the real database** that the usage query behind `can_upload()` (it runs on every upload) uses an index: `set enable_seqscan = off; explain select count(*) from storage.objects where bucket_id = 'user-documents' and name like '<user-uuid>/%';`. The `name` prefix should appear in an `Index Cond`. If it only appears in a `Filter`, plan a follow-up before the bucket grows large.

## 3c. Production checklist

- `/api/v1/health` returns 200 with `"database":"ok"` and `"embedding":"ok"`, and `"environment":"production"`.
- Environment: `ENVIRONMENT=production`, `EMBEDDING_PROVIDER=gemini` (or install the local model), publishable key in `SUPABASE_ANON_KEY`, `ADMIN_TOKEN`, `CORS_ORIGINS` set to your web origins (default `*`).
- Python 3.11 (`.python-version`), CI green (`.github/workflows/tests.yml`).
- Persistent disk at `CHROMA_PERSIST_DIRECTORY`, one instance, one worker, no sleeping tier.
- Paid or sufficient Gemini quota; data-use terms checked.
- Dependencies pinned (**not done yet**: `requirements.txt` is unpinned, so each build can pull new versions).
- Real end-to-end run: sign in, upload, index, search, chat, summarize, delete account.

## 4. Recovery: ChromaDB storage lost

Postgres and Storage are the source of truth; Chroma is derived data.

1. Redeploy/restart (a fresh, empty `CHROMA_PERSIST_DIRECTORY`). On start the worker runs a reconcile; you can also call it explicitly: `POST /api/v1/admin/reconcile`.
2. Reconcile compares, for every `completed` document, the vector count in Chroma with `documents.chunk_count`. Missing/partial -> `index_state='missing'` and a `reindex` job (the document status goes back to `queued`, so it is excluded from search while rebuilding). `stale`/`missing` documents without a job (e.g. after a crash) are re-queued too.
3. The worker re-downloads each PDF, re-extracts, re-embeds and upserts with deterministic chunk ids; the final verification requires `Chroma count == chunk_count == rows in document_chunks`, otherwise the job fails and is retried.
4. Watch `GET /api/v1/documents/{id}/status` (users) or the Postgres tables `processing_jobs` (operators). Chats on a document that is still rebuilding return `409 document_not_indexed`; multi-document chats simply skip it.
5. Time/cost: with the Gemini provider re-embedding is limited by your API quota (and billed on paid tiers). With the local provider it is CPU-bound. Rough planning number (not measured): hundreds of chunks per second on a modern multi-core CPU for the MiniLM-class models, so a library of tens of thousands of chunks rebuilds in minutes; bge-m3 is several times slower. Measure on your instance before relying on it.

Backups: use the host's disk snapshots if offered (verify what your provider guarantees) in addition to the rebuild path. A manual cold backup is a tar of `CHROMA_PERSIST_DIRECTORY` taken while the service is stopped (copying a live SQLite-backed store can be inconsistent).

## 5. Observability

- Logs: everything goes through uvicorn's logger to stdout; ship it with the platform's log drain. Server errors are logged with stack traces; responses only contain generic text plus a `request_id` (also in the `X-Request-ID` header): ask the user for it and grep the logs.
- Useful log lines: `rag usage conversation=... {input_tokens, output_tokens}` (Gemini usage per answer), `job ... failed ...`, `startup reconcile: {...}`, `vector store returned a foreign chunk` (logger `uvicorn.error.security`: should never appear; alert on it), `answer without valid citations replaced by a refusal` (prompt/model quality signal).
- Error monitoring: point Sentry (or similar) at the process (`sentry-sdk[fastapi]`, not installed by default) or alert on log patterns above; alert on `/api/v1/health` returning 503 and on `processing_jobs` with `status='failed'`.
- SQL snippets: stuck work `select * from processing_jobs where status='running' and locked_until < now();` failures `select id, document_id, error_message from processing_jobs where status='failed' order by created_at desc limit 50;`

## 6. Limits and knobs

`MAX_PDF_BYTES` (50 MB, also the bucket limit), `MAX_PDF_PAGES` (1000), `MAX_DOCS_PER_USER` (200), `MAX_ACTIVE_JOBS_PER_USER` (5), `JOB_RETENTION_DAYS` (30; finished jobs older than this are removed hourly, the latest job of each document is kept), `ORPHAN_MAX_AGE_HOURS` (24), `CHAT_RATE_PER_MINUTE` (20), `CHAT_MAX_CONCURRENT_STREAMS` (2 in-flight streams per user), `LLM_STREAM_DEADLINE_SECONDS` (120), `SEARCH_RATE_PER_MINUTE` (60), `EMBED_BATCH` (32), `RAG_CANDIDATES` (20), `RAG_CONTEXT_CHUNKS` (6), `RAG_MAX_PER_DOC` (3), `RAG_MIN_SCORE` (unset), `HISTORY_MESSAGES` (6), `LLM_TIMEOUT_SECONDS` (180; timeouts are never retried), `LLM_MAX_OUTPUT_TOKENS` (1024), `LLM_MAX_RETRIES` (1; other transient errors; a stream is retried only while nothing was sent), `GEMINI_FALLBACK_MODEL` (empty; tried after the retries, e.g. `gemini-3.1-flash-lite`), `WORKER_POLL_SECONDS` (5), `JOB_LEASE_SECONDS` (300). Rate limits are per process (fine for the single-worker deployment).

Memory: the process holds Chroma's index (plus, with the local provider, torch and the embedding model, ~0.5 GB, loaded on first use); ingestion streams the PDF to disk, processes one page at a time and embeds in batches of 32 (the local model runs one encode at a time). These are design bounds, not measurements: load-test on the target instance.

## 7. Runbook

| Symptom | Likely cause | Action |
|---|---|---|
| All calls `403 provider_not_allowed` | the user signed in with a provider not in `AUTH_ALLOWED_PROVIDERS` | enable it in the list or sign in with Google/Facebook |
| Uploads refused by Storage | the user's space is full (or the migration's policy sees no `app_limits` row) | check `GET /api/v1/me`; adjust `public.app_limits` |
| All `/api/v1` calls `401` | token expired/wrong project, JWKS unreachable, `SUPABASE_URL` mismatch (issuer check) | check the app uses the same project; check logs for `JWKS fetch failed` |
| Log `Name or service not known` / `worker cannot reach Supabase` / `configuration: SUPABASE_URL ...` | the host in `SUPABASE_URL` does not exist: typo, another project's ref, or a paused/deleted project | copy the Project URL from the Supabase dashboard into the host's environment, then redeploy |
| `/api/v1/health` 503 `degraded` | see its `database` and `embedding` fields: `unreachable` (URL), `bad_anon_key` / `bad_service_key` (keys of another project), `schema_outdated` (apply migrations), `not_installed` (provider without its packages), `no_api_key` | fix the setting; the result is cached for 30 s |
| `/api/v1/health` 503 `unconfigured` | missing `SUPABASE_URL` / anon / service-role key | set env |
| `/api/v1/health` 503 `needs_reindex` | embedding model or dimension changed | `POST /api/v1/admin/reindex` |
| Document stays `uploaded` after registering | the user already had `MAX_ACTIVE_JOBS_PER_USER` documents queued or processing | the app calls `POST .../process` later |
| Document stuck `queued` | worker not running (`WORKER_ENABLED=false`, health `worker: stopped`) or unconfigured | check health + logs |
| Document stuck `processing` | worker died; lease expires after `JOB_LEASE_SECONDS`, then another claim resumes | wait, or restart |
| Document `failed` | see `error_message` (scanned PDF without text, corrupt, encrypted, too many pages, size mismatch) | user fixes the file, or `POST .../retry` for transient errors |
| Log `llm_timeout` (client sees `llm_unavailable`) | Gemini did not answer in time | check `summarize input: N chars` and the model; `LLM_TIMEOUT_SECONDS` must not be set low in the host's environment |
| `503 UNAVAILABLE ... high demand` in the logs | the Gemini model is overloaded | set `GEMINI_FALLBACK_MODEL`, or change `GEMINI_MODEL` |
| Chat `429 llm_quota_exceeded` | Gemini quota | wait (`Retry-After`) or upgrade the plan |
| Search/chat `429 embedding_quota_exceeded`, or ingestion jobs failing with it in the logs | Gemini embedding quota | wait, upgrade the plan, or switch to `huggingface` and reindex |
| Chat answers always "not found" | `RAG_MIN_SCORE` too high, wrong embedding model for the language, or documents not indexed | lower/unset it; check `/status`; check retrieval with `/api/v1/search` |
| Chat `409 document_not_indexed` | document still processing or being rebuilt | poll `/status` |

## 8. Flutter integration checklist

1. Sign in with Supabase Auth; keep the access token fresh (refresh before expiry) and send it as `Authorization: Bearer`.
2. Upload: generate a UUID `document_id`; upload to bucket `user-documents` at `{user_id}/{document_id}/{file_name}` with `upsert: false` (file name without `/`).
3. `POST /api/v1/documents` -> poll `GET /api/v1/documents/{id}/status` (`queued -> processing -> completed | failed`, show `error_message`).
4. `POST /api/v1/conversations` (`single_document` + `document_id`, or `all_documents`), then `POST /api/v1/chat/stream` for live answers (replace the streamed text with `done.answer`), or `/chat`. Render citations from `sources` (`document_title`, `page_number`).
5. Handle `429` with `Retry-After`, `409 document_not_indexed`, `503`, and always show a generic message for `500` with the `request_id`.
6. Never ship the service-role key; the anon key alone is safe in the app.

## 9. Upgrade path (not built)

Celery/Redis consumer instead of the thread (`JobRunner`/`JobHandler` are the seams), Chroma as a separate server or another vector store behind `VectorStore`, a cross-encoder reranker behind `RerankingService`, a snapshot backup of the Chroma directory to Storage, background execution of `reindex`/`reconcile` as queued jobs (they currently loop inside the admin request).

## Migration for direct app access
Apply `supabase/migrations/20261128000000_client_rpcs.sql` after the earlier ones, then redeploy the service so its worker handles `account_deletion_requests`. Keep `WORKER_ENABLED` on.

Then apply `supabase/migrations/20261129000000_document_language.sql` (lets owners set a document's language). No redeploy needed.

## Share page

Redeploy before releasing an app that shares by QR code: it opens `GET /s` on this service. `ANDROID_PACKAGE` in `app/api/share_page.py` must match the app's `applicationId`.

## Migration 20261204000000_reader_avatars.sql

Apply it after `20261203000000_document_readers.sql`. SQL only: no service redeploy.

## Migration 20261203000000_document_readers.sql

Apply it after `20261202000000_share_any_status.sql`, before releasing the app. SQL only: no service redeploy.

## Migration 20261202000000_share_any_status.sql

Apply it after `20261201000000_document_sharing.sql`. SQL only: no service redeploy needed for it. It replaces `share_document` and `accept_shared_document` and adds triggers that keep waiting copies in step with the uploader's document.

## Migration 20261201000000_document_sharing.sql

Apply it **before** deploying the service: the new code reads `documents.content_id` / `content_owner_id`. Then deploy, then release the app. No reindex is needed. Do not roll the service back to an older version once anyone has accepted a shared document: older code deletes vectors and files without checking other holders.

## Migration 20261130000000_document_error_code.sql

Apply it **before** deploying the worker: the new worker passes `p_error_code` to `finish_job`. Then deploy the service, then release the app. Quota (429) job failures are requeued after the larger of `Retry-After` and the exponential backoff, plus up to 25% jitter.
