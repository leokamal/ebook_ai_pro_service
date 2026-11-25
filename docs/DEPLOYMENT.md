# Deployment, operations and recovery (E-Book AI Pro)

Audience: whoever deploys and operates the service. Contract for the app: [API.md](API.md). Design: [features/ebook_ai_pro_backend.md](features/ebook_ai_pro_backend.md).

## 1. Topology

One container, **one uvicorn worker**: FastAPI + the in-process job runner + an in-process ChromaDB (`PersistentClient`). Chroma is never exposed on a port, so Flutter and the internet cannot reach it. Stateful data:

| Data | Where | Lost if the disk is lost? |
|---|---|---|
| Users, documents, conversations, messages, citations, job queue | Supabase Postgres | no |
| Original PDFs | Supabase Storage `user-documents` | no |
| **Pro vector index** | `CHROMA_PERSIST_DIRECTORY` (disk) | yes, but rebuildable (section 4) |
| Embedding model cache | `HF_HOME` (disk) | yes, re-downloaded |

More than one worker/replica is **not supported** (Chroma's SQLite files and the job runner assume a single process; extra replicas would also each reindex). Scale vertically.

## 2. Hosting options (be realistic)

| Option | Persistence | Cold start | Verdict |
|---|---|---|---|
| Render free web service | **ephemeral disk**, sleeps after ~15 min idle, ~512 MB RAM | minutes (model + index) | Demo only. Torch + the embedding model need roughly 1-2 GB. Every restart/sleep wipes Chroma, so the whole library is re-embedded on CPU. Not suitable for real users. |
| Render paid web service (>= 2 GB RAM) + persistent disk | disk survives restarts and deploys | seconds (index on disk) | **Recommended MVP**: `render.yaml` in the repo. Check current plan sizes and prices. |
| Any VPS / Fly.io machine + volume (2 GB+) | volume | seconds | Equivalent, cheaper at small scale; use the Dockerfile and mount a volume at `/data`. |
| Free tier + snapshot backup of the Chroma directory to Storage | rebuild-from-snapshot | minutes | **Not implemented** (open question in the spec). The documented recovery (section 4) re-embeds from the PDFs instead. |

Free vs paid components: Supabase free tier (quota-limited: DB size, 1 GB storage, bandwidth, projects pause after inactivity), Gemini API free quota (rate/day limited; quota errors surface as `429 llm_quota_exceeded`), sentence-transformers models and ChromaDB (free, cost = RAM/CPU), Render persistent disk and the larger instance (the paid part). Check each provider's current limits before launch.

## 3. Configure and deploy

1. Create the Supabase project and apply the migrations ([README](../README.md#supabase-setup-once-per-project)).
2. Environment (see `.env.example`; **placeholders only are committed**). Required: `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`, `GEMINI_API_KEY`, `ADMIN_TOKEN`. Recommended: `HF_TOKEN` (higher Hugging Face rate limits), `HF_HOME` on the disk. Defaults: bucket `user-documents`, collection `ebook_ai_pro_documents`, model `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (384 dimensions).
3. Docker: `docker build -t ebook-ai . && docker run --env-file .env -p 8000:8000 -v ebook_ai_data:/data ebook-ai`. Render: connect the repo, use `render.yaml` (Docker runtime, disk at `/data`), fill the secret env vars.
4. Smoke test:
   - `GET /health` -> `{"status":"ok"}` (liveness); `GET /api/v1/health` -> `{"status":"ok","worker":"running","vector_index":{...}}` (503 `unconfigured` if a key is missing, 503 if the index needs a reindex).
   - With a real user token: register a PDF, poll `/documents/{id}/status` until `completed`, `POST /api/v1/search`, `POST /api/v1/chat`.
5. Keep-alive pings (paid instances do not sleep): not needed. If you must use a sleeping tier, ping `/health` (it never loads the embedding model).

Container: the entrypoint starts as root only to `chown` the mounted disk (providers mount it root-owned), then drops to the unprivileged `app` user with `gosu`.

Startup/shutdown: the lifespan hook starts the job runner (no model loaded, Chroma not opened until first use or the startup reconcile), and on shutdown stops the runner and closes Chroma. Heavy imports are lazy, so the port binds quickly.

### Embedding model choice

| Model | Size / RAM (CPU, fp32) | Dim | Notes |
|---|---|---|---|
| `paraphrase-multilingual-MiniLM-L12-v2` (default) | ~118M params, ~0.5 GB | 384 | ar/fr/en, fits a 2 GB instance |
| `intfloat/multilingual-e5-small` | ~118M, ~0.5 GB | 384 | uses `query:`/`passage:` prefixes (handled automatically) |
| `BAAI/bge-m3` | ~568M, ~2.3 GB | 1024 | best quality, long inputs; needs a 4 GB+ instance. Set `EMBEDDING_MODEL=BAAI/bge-m3`, `EMBEDDING_DIMENSION=1024` |

**Changing the model**: set the two variables, redeploy. The store detects the mismatch and enters `needs_reindex` (`/api/v1/health` returns 503, search/chat return `503 index_unavailable`, nothing is written). Then run `POST /api/v1/admin/reindex` (header `X-Admin-Token`) while no job is running: documents are flagged `stale` first, the collection is recreated for the new model, and rebuild jobs are queued. Documents become searchable again one by one as they finish. Quality of any model on your real Arabic/French content must be checked by hand; tune `RAG_MIN_SCORE` afterwards.

## 4. Recovery: ChromaDB storage lost

Postgres and Storage are the source of truth; Chroma is derived data.

1. Redeploy/restart (a fresh, empty `CHROMA_PERSIST_DIRECTORY`). On start the worker runs a reconcile; you can also call it explicitly: `POST /api/v1/admin/reconcile`.
2. Reconcile compares, for every `completed` document, the vector count in Chroma with `documents.chunk_count`. Missing/partial -> `index_state='missing'` and a `reindex` job (the document status goes back to `queued`, so it is excluded from search while rebuilding). `stale`/`missing` documents without a job (e.g. after a crash) are re-queued too.
3. The worker re-downloads each PDF, re-extracts, re-embeds and upserts with deterministic chunk ids; the final verification requires `Chroma count == chunk_count == rows in document_chunks`, otherwise the job fails and is retried.
4. Watch `GET /api/v1/documents/{id}/status` (users) or the Postgres tables `processing_jobs` (operators). Chats on a document that is still rebuilding return `409 document_not_indexed`; multi-document chats simply skip it.
5. Time/cost: re-embedding is CPU-bound. Rough planning number (not measured): hundreds of chunks per second on a modern multi-core CPU for the MiniLM-class models, so a library of tens of thousands of chunks rebuilds in minutes; bge-m3 is several times slower. Measure on your instance before relying on it.

Backups: use the host's disk snapshots if offered (verify what your provider guarantees) in addition to the rebuild path. A manual cold backup is a tar of `CHROMA_PERSIST_DIRECTORY` taken while the service is stopped (copying a live SQLite-backed store can be inconsistent).

## 5. Observability

- Logs: everything goes through uvicorn's logger to stdout; ship it with the platform's log drain. Server errors are logged with stack traces; responses only contain generic text plus a `request_id` (also in the `X-Request-ID` header): ask the user for it and grep the logs.
- Useful log lines: `rag usage conversation=... {input_tokens, output_tokens}` (Gemini usage per answer), `job ... failed ...`, `startup reconcile: {...}`, `vector store returned a foreign chunk` (logger `uvicorn.error.security`: should never appear; alert on it), `answer without valid citations replaced by a refusal` (prompt/model quality signal).
- Error monitoring: point Sentry (or similar) at the process (`sentry-sdk[fastapi]`, not installed by default) or alert on log patterns above; alert on `/api/v1/health` returning 503 and on `processing_jobs` with `status='failed'`.
- SQL snippets: stuck work `select * from processing_jobs where status='running' and locked_until < now();` failures `select id, document_id, error_message from processing_jobs where status='failed' order by created_at desc limit 50;`

## 6. Limits and knobs

`MAX_PDF_BYTES` (50 MB, also the bucket limit), `MAX_PDF_PAGES` (1000), `MAX_DOCS_PER_USER` (200), `MAX_ACTIVE_JOBS_PER_USER` (5), `CHAT_RATE_PER_MINUTE` (20), `CHAT_MAX_CONCURRENT_STREAMS` (2 in-flight streams per user), `LLM_STREAM_DEADLINE_SECONDS` (120), `SEARCH_RATE_PER_MINUTE` (60), `EMBED_BATCH` (32), `RAG_CANDIDATES` (20), `RAG_CONTEXT_CHUNKS` (6), `RAG_MAX_PER_DOC` (3), `RAG_MIN_SCORE` (unset), `HISTORY_MESSAGES` (6), `LLM_TIMEOUT_SECONDS` (60), `LLM_MAX_OUTPUT_TOKENS` (1024), `LLM_MAX_RETRIES` (2), `WORKER_POLL_SECONDS` (5), `JOB_LEASE_SECONDS` (300). Rate limits are per process (fine for the single-worker deployment).

Memory: the process holds torch + the embedding model (~0.5 GB, loaded on first use) + Chroma's index; ingestion streams the PDF to disk, processes one page at a time and embeds in batches of 32, and only one encode runs at a time. These are design bounds, not measurements: load-test on the target instance.

## 7. Runbook

| Symptom | Likely cause | Action |
|---|---|---|
| All `/api/v1` calls `401` | token expired/wrong project, JWKS unreachable, `SUPABASE_URL` mismatch (issuer check) | check the app uses the same project; check logs for `JWKS fetch failed` |
| `/api/v1/health` 503 `unconfigured` | missing `SUPABASE_URL` / anon / service-role key | set env |
| `/api/v1/health` 503 `needs_reindex` | embedding model or dimension changed | `POST /api/v1/admin/reindex` |
| Document stuck `queued` | worker not running (`WORKER_ENABLED=false`, health `worker: stopped`) or unconfigured | check health + logs |
| Document stuck `processing` | worker died; lease expires after `JOB_LEASE_SECONDS`, then another claim resumes | wait, or restart |
| Document `failed` | see `error_message` (scanned PDF without text, corrupt, encrypted, too many pages, size mismatch) | user fixes the file, or `POST .../retry` for transient errors |
| Chat `429 llm_quota_exceeded` | Gemini quota | wait (`Retry-After`) or upgrade the plan |
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

Celery/Redis consumer instead of the thread (`JobRunner`/`JobHandler` are the seams), Chroma as a separate server or another vector store behind `VectorStore`, a cross-encoder reranker behind `RerankingService`, OCR for scanned PDFs (EasyOCR is already a dependency), a snapshot backup of the Chroma directory to Storage, sharing documents between users, background execution of `reindex`/`reconcile` as queued jobs (they currently loop inside the admin request).
