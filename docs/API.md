# E-Book AI Pro API (for the Flutter client)

Interactive reference: `/docs` (Swagger UI) and `/openapi.json`. Base URL: your deployment, e.g. `https://<service>.onrender.com`.
Liveness probe (no auth, no details): `GET /health` -> `{"status": "ok"}`. Everything else is under `/api/v1`.
The previous single-library endpoints (`/query`, `/query-database/`, `/upload`, `/generate-database/`, `/load-database/`) no longer exist.

## Sign-in (Google and Facebook through Supabase Auth)
The app signs in with Supabase directly; this API has no login endpoints and holds no Google or Facebook secrets.
- Same flow as Dama: a guest is local to the app (never call `signInAnonymously`; guests cannot use this API). First sign-in uses `signInWithOAuth`; a signed-in user adds the other provider with `linkIdentity` (same user id).
- `supabase.auth.signInWithOAuth(OAuthProvider.google | OAuthProvider.facebook, redirectTo: '<app-scheme>://login-callback')` with PKCE; register the deep link on Android and iOS. Only the Supabase URL and publishable key ship in the app.
- Send `session.accessToken` as `Authorization: Bearer`. Let the SDK refresh it; on `401` refresh once, then sign out.
- Anonymous sessions get `401`. A session from any other provider (e.g. e-mail) gets `403 provider_not_allowed` on every route. Direct Supabase database and Storage calls from such sessions are refused too.
- The same verified e-mail on Google and Facebook is one account (same `user_id`, same space). A Facebook account without an e-mail is a separate account.

### Your space
Every user has a private folder `user-documents/{user_id}/` with a storage limit (default 100 MB, 220 files). When it is full, **the Storage upload itself is refused** (row-level security error): show the usage from `/me`.
- `GET /api/v1/me` -> `{"id","generated_key","email","name","full_name","avatar_url","avatar_hidden","country","gender","providers":["google"],"usage":{"documents","storage_bytes","storage_limit_bytes","objects","object_limit"}}` (`email`, `name`, `avatar_url` may be `null`).
- Profile (same model as the Dama app): every account gets a row in `public.profiles`. The app reads it directly (`.from('profiles').select().eq('id', uid).maybeSingle()`) and may update only `display_name` (3-20 letters of any script, digits, space, `_ . -`; not unique), `avatar_hidden`, `country` (ISO alpha-2) and `gender` (`male|female|unspecified`). A rejected value is Postgres error `23514`. `email`, `full_name`, `avatar_url` come from Google/Facebook and only fill empty fields; `generated_key` is a stable per-user identifier, not a secret. In `/me`, `name` is the display name and `avatar_url` is `null` when `avatar_hidden`.
- `DELETE /api/v1/me` with body `{"confirm":"DELETE"}` -> `204`. Deletes the account, files, vectors, documents and chat history; irreversible. Then call `supabase.auth.signOut()` locally. `422` without the exact confirmation; `503 account_deletion_incomplete` means nothing is lost for good: retry.
- `POST /documents` also returns `429 quota_exceeded` when your other files already fill the space, or at the document limit (200).
- An uploaded file that is never registered with `POST /documents` is removed after 24 hours.

## Overview (`/api/v1`)
Auth: `Authorization: Bearer <Supabase access token>` (verified server-side; `user_id` is never accepted from the client).
Errors on `/api/v1` only: `{"error": {"code": "unauthorized", "message": "...", "request_id": "..."}}`.
Pagination: `?limit=1..100&cursor=<opaque>` -> `{"items": [...], "next_cursor": "..."|null}`.
- `GET /api/v1/health` (no auth): `{"status": "ok"|"unconfigured", "environment": "..."}`, 503 when unconfigured.

### Documents (Phase 4)
Flutter flow: upload the PDF to Supabase Storage at `user-documents/{user_id}/{document_id}/{file_name}` (generate `document_id` as a UUID, **upload with `upsert: false`**), then register it.

**Several PDFs:** give each file its own `document_id`, upload it and register it; one after another or all at once. At most 5 documents per user are queued or processing at a time. With `auto_process: true`, further ones are still accepted (`201`) but come back with `status: "uploaded"`: **they do not start on their own**, call `/process` when earlier ones finish. Always read `status` after registering. The total space limit still applies.

A registered file cannot be deleted or replaced directly in Storage. To replace a PDF, delete the document through this API and upload it again under a new id.

- `POST /api/v1/documents` `{"document_id": "<uuid>", "title": "...", "file_name": "book.pdf", "language": "fr"?, "auto_process": true}` -> `201` document (`200` if already registered by you; `409` if the id belongs to someone else, or you already used it for another `file_name`; `422 file_not_found|invalid_file`; `413` too large). Unknown fields (`user_id`, `file_path`) are rejected (422).
- `GET /api/v1/documents?limit=&cursor=&status=` -> `{"items": [...], "next_cursor": ...}`
- `GET /api/v1/documents/{id}` -> document. Other users' documents return `404`.
- `GET /api/v1/documents/{id}/status` -> `{"status","index_state","progress","attempts","error_message","page_count","chunk_count"}`. `status`: `uploaded|queued|processing|completed|failed|deleting`. Poll it.
- `POST /api/v1/documents/{id}/process` (only from `uploaded`) and `/retry` (only from `failed`) -> `202 {"document_id","status":"queued","job_id"}`; wrong state -> `409`; 5 documents already queued or processing -> `429 quota_exceeded`.
- `DELETE /api/v1/documents/{id}` -> `202` (asynchronous: vectors, file and row are removed).
Failures set `status=failed` with a stable `documents.error_code` (read from the table; not part of `/status`: `no_extractable_text`, `encrypted_pdf`, `corrupt_pdf`, `too_many_pages`, `file_too_large`, `size_mismatch`, `invalid_path`, `embedding_quota_exceeded`, `llm_quota_exceeded`, `temporary_error`, `index_unavailable`, `document_missing`, `attempts_exhausted`) and a user-safe `error_message` (e.g. scanned PDF without text, corrupt or password-protected PDF, too many pages).
Admin (`X-Admin-Token`): `POST /api/v1/admin/reindex` (after changing the embedding provider or model), `POST /api/v1/admin/reconcile` (rebuild documents whose vectors were lost).

### Conversations and chat (Phase 5)
Auth required on all routes. Resources of other users return `404`.
- `POST /api/v1/conversations` `{"mode": "single_document"|"all_documents", "document_id": "<uuid>"?, "title"?}` -> `201`. `single_document` requires `document_id` (yours); `all_documents` forbids it. The mode and document are fixed for the life of the conversation. At 1000 conversations -> `429 quota_exceeded`.
- `GET /api/v1/conversations?limit=&cursor=` (most recently updated first), `GET|PATCH|DELETE /api/v1/conversations/{id}` (`PATCH` accepts only `title`; `DELETE` -> `204`).
- `GET /api/v1/conversations/{id}/messages?limit=&cursor=` -> newest first; assistant messages carry `sources`.
- `POST /api/v1/chat` `{"conversation_id": "<uuid>", "message": "1-4000 chars", "summarize": false}` ->
  `{"message_id", "answer", "sources": [{"document_id","document_title","page_number","chunk_id","relevance_score","snippet"}], "usage": {"input_tokens","output_tokens"}}`.
  The answer text contains `[S1]`-style markers that match entries in `sources` (only sources that really support the answer are returned; an answer without markers has `sources: []`). When the documents do not contain the answer, `answer` says so in the user's language and `sources` is `[]`.
  Errors: `404` conversation/document not found, `409 document_not_indexed` (single-document chat before processing finished), `429 llm_quota_exceeded` (with `Retry-After`), `503 llm_unavailable|index_unavailable`. A turn (your message + the answer) is stored only after a successful answer; on any error nothing is stored, so retrying is safe. `429 rate_limited` (with `Retry-After`) if one user sends more than `CHAT_RATE_PER_MINUTE` (default 20) requests per minute. An answer that cannot be grounded in a cited source is returned as the not-found reply with `sources: []`.
  The request cannot carry `user_id` or `document_id`; scope comes from the conversation. Streaming variant: `POST /api/v1/chat/stream` (below).

### Search (Phase 6)
- `POST /api/v1/search` `{"query": "1-1000 chars", "mode": "single_document"|"all_documents", "document_id": "<uuid>"?, "top_k": 1-20 (default 6)}` ->
  `{"results": [{"chunk_id","document_id","document_title","page_number","score","text"}], "searched_documents": N}` sorted by score (best first). No LLM call.
  `single_document` requires `document_id` and `all_documents` forbids it (422 otherwise, as for unknown fields such as `user_id`). Same scope rules as chat: another user's document -> `404`; a document that is not indexed yet -> `409 document_not_indexed`; with `all_documents` only your completed, indexed documents are searched (`searched_documents` tells you how many; an empty library returns `results: []`). In `all_documents` mode results are diverse: at most a few chunks per document. Rate limit: `SEARCH_RATE_PER_MINUTE` (default 60) -> `429 rate_limited`.
- Multi-document chat (`mode: all_documents`) cites the document and page of every source; the prompt context is grouped by document so answers can compare books.

### Streaming chat (Phase 7)
`POST /api/v1/chat/stream` takes the same body as `/chat` and the same rules, and responds with `text/event-stream`
(headers `Cache-Control: no-cache`, `X-Accel-Buffering: no`).
Validation, ownership, readiness and retrieval happen **before** the stream starts, so `401/404/409/422/429/503` are ordinary
JSON errors (error envelope below). Events, in order:
```
event: meta     data: {"conversation_id": "..."}
event: token    data: {"text": "Cats sleep "}            (0..n, raw model text incl. [S1] markers)
event: sources  data: {"sources": [{"document_id","document_title","page_number","chunk_id","relevance_score","snippet"}]}
event: done     data: {"message_id": "...", "answer": "Cats sleep a lot [S1].", "refused": false, "usage": {"output_tokens": 7}}
```
or, if generation fails after the stream started: `event: error data: {"code": "llm_quota_exceeded", "message": "..."}` as the last event (nothing is stored).
- **`done.answer` is authoritative.** It equals the concatenated tokens for a normal cited answer. It differs when the answer was turned into a refusal (the documents do not contain it, or the answer had no valid citation) or when unknown citation markers were stripped: replace the streamed text with `done.answer`. For a "not found" refusal the only token is the refusal sentence itself (the internal marker is never streamed).
- `sources` is sent after generation (citations need the full text) and `refused: true` implies `sources: []`.
- The turn (your message + the answer) is stored only when `done` is sent. If the client disconnects, nothing is stored.
- At most `CHAT_MAX_CONCURRENT_STREAMS` (default 2) streams per user may be in flight; another one gets `429 rate_limited` (`Retry-After: 5`). A generation that runs longer than `LLM_STREAM_DEADLINE_SECONDS` (120) ends with an `error` event (`llm_unavailable`) and stores nothing. With `summarize: true` the limit is at least `LLM_TIMEOUT_SECONDS` + 60 (240), and the first token can take a while: do not time the request out earlier in the app.
- `usage.output_tokens` is Gemini's count when available, otherwise an estimate.
- Dart sketch: `http.Client().send(request)`, then split `response.stream.transform(utf8.decoder)` on `

`; each block has an `event:` and a `data:` line.

### Error envelope, request ids, OpenAPI
- Every `/api/v1` error: `{"error": {"code": "...", "message": "...", "request_id": "..."}}`. `request_id` equals the `X-Request-ID` response header (a client-supplied `X-Request-ID` of 8-64 chars `[A-Za-z0-9._-]` is reused, otherwise one is generated). Quote it when reporting a problem.
- Codes: `unauthorized`(401, `WWW-Authenticate: Bearer`), `forbidden`(403, admin only), `not_found`(404, also for other users' resources), `conflict`(409), `document_not_indexed`(409), `validation_error`(400/422), `payload_too_large`(413), `file_not_found`/`invalid_file`(422), `quota_exceeded`(429, per-user limits), `rate_limited`(429, `Retry-After`), `llm_quota_exceeded`(429, `Retry-After`), `llm_unavailable`(503), `embedding_quota_exceeded`(429, `Retry-After`; search and chat), `embedding_unavailable`(503; search and chat), `index_unavailable`(503), `storage_unavailable`(503), `internal_error`(500, details only in server logs).
- Interactive docs: `/docs` (Swagger UI, "Authorize" accepts the Supabase access token) and `/openapi.json`. `tests/pro/test_streaming_and_openapi.py` fails if a required endpoint, the bearer scheme, the error schema or the SSE description disappears.

## Direct Supabase access (current app)
The Flutter app calls this API only for `POST /api/v1/chat/stream`. Everything else goes to Supabase with the user's session: rows under RLS, plus the functions of `20261128000000_client_rpcs.sql`: `request_processing(p_document_id)`, `request_document_delete(p_document_id)`, `my_usage()`, `request_account_deletion()`. Errors are raised as `not_found|conflict|file_not_found|quota_exceeded|forbidden`. The worker must be running: it processes jobs, finishes `deleting` documents and performs requested account deletions.

The app also updates `documents.language` directly (migration `20261129000000_document_language.sql`; owner only).

## Health
`GET /api/v1/health` (no auth): `status` (`ok|degraded|unconfigured`), `database`, `embedding`, `embedding_provider`, `embedding_model`, `environment`, `vector_index`, `worker`. HTTP 503 when the service cannot work. `GET /health` is the liveness probe only.
