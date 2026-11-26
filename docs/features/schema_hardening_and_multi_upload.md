# Schema hardening and multi-PDF upload

Status: planned (not implemented).

Fix the findings of the review of `20261125000000_pro_schema.sql` and `20261126000000_user_space.sql`, and make sure a user can upload several PDFs, including many at once. Unchanged: Supabase, ChromaDB, Gemini, the Flutter upload-then-register flow, one uvicorn worker, response shapes. Both existing migrations stay untouched; all SQL goes in one new migration.

## Current behavior / problems

### Multiple PDFs
The schema already allows many documents per user (`unique (user_id, file_path)`, and one active job *per document*). These things can still stop or hide a further upload:

1. `register_document` calls `_check_quota`, which rejects a new document with `429` while the user has `MAX_ACTIVE_JOBS_PER_USER` (5) queued or running jobs, even with `auto_process=false`. Selecting 8 PDFs at once fails from the 6th on. The refused files stay unregistered and the janitor removes them after `ORPHAN_MAX_AGE_HOURS`.
2. Reusing a `document_id` for a second file returns `200` with the **first** document (`DocumentRepository.create`). The second PDF is silently ignored.
3. Total space is 100 MB per user (`app_limits`), so two 50 MB PDFs fill it. This is intended and stays.

### Review findings
4. `user_documents_delete` lets a user delete a registered PDF and upload another at the same path. Ingestion only compares the size.
5. `finish_job` ignores `locked_by`. A stalled worker can complete, fail or requeue a job another worker now holds.
6. No index covers `message_sources.chunk_id`, and `processing_jobs.document_id` is only indexed for active jobs.
7. `ChunkRepository.replace` deletes then reinserts, so `on delete set null` clears `chunk_id` on all earlier citations at every reindex.
8. Direct PostgREST inserts into `documents` and `conversations` skip the API limits.
9. Rerunning `20261125000000` alone restores the upload policy without the space limit and the job functions that can revive a `deleting` document.
10. The database accepts `file_name = '.'`; `build_file_path` rejects it.
11. It is unverified that the `name like 'uid/%'` filter in `user_storage_usage` uses an index.
12. Finished `processing_jobs` rows are never removed.

## Required changes

### A. Multi-PDF upload (`app/api/v1/documents.py`)
- `register_document`: the active-job cap no longer rejects registration. For a new document check only the document count and the space limit. Enqueue when `auto_process` is true **and** `active_count() < MAX_ACTIVE_JOBS_PER_USER`; otherwise return `201` with `status: "uploaded"`.
- `/process` and `/retry` keep returning `429` at the cap.
- If the id already exists for this user and `file_name` differs from the stored one, return `409 conflict`. Same id and same `file_name` stays `200`.
- No batch endpoint: the client registers each file with its own UUID.

### B. Migration `supabase/migrations/20261127000000_hardening.sql`
Idempotent, with a header comment.

1. **Storage delete policy** (client deletes only unregistered files; the backend uses the service role and is unaffected):
   ```sql
   drop policy if exists user_documents_delete on storage.objects;
   create policy user_documents_delete on storage.objects for delete to authenticated
     using (bucket_id = 'user-documents'
            and (storage.foldername(name))[1] = (select auth.uid())::text
            and not exists (select 1 from public.documents d where d.file_path = objects.name));
   ```
2. **Lease-checked finish.** Drop `finish_job(uuid, boolean, text, integer)`. Create `finish_job(p_job_id uuid, p_ok boolean, p_worker text, p_error text default null, p_retry_after_seconds integer default null) returns boolean`, `security definer`, `set search_path = ''`. It locks the row only where `id = p_job_id and locked_by = p_worker and status = 'running'`, otherwise returns `false` and changes nothing. Keep the retry logic and the `status <> 'deleting'` guard. Revoke from `public, anon, authenticated`; grant to `service_role`.
3. **Indexes:** create `message_sources (document_id, chunk_id)` and `processing_jobs (document_id, created_at desc)`; drop `message_sources_document_idx`.
4. **Row limits:** add `app_limits.max_documents integer not null default 200` and `max_conversations integer not null default 1000`, both `check (>= 0)`. Add `before insert` triggers on `documents` and `conversations` (`security invoker`, `set search_path = ''`) that take `pg_advisory_xact_lock` on a hash of table name and `user_id`, count that user's rows, and `raise exception 'quota_exceeded'` at the limit.
5. **File name:** add check constraint `documents_file_name_not_dot` (`file_name <> '.'`), using `drop constraint if exists` first.
6. **Job pruning:** `prune_processing_jobs(p_older_than interval, p_limit integer default 500) returns integer`, `service_role` only. Delete `completed`/`failed` jobs with `completed_at` older than `greatest(p_older_than, interval '1 day')`, **never the latest job of a document** (the status endpoint reads it).

### C. Backend
- `app/repositories/jobs.py`: `JobQueue.finish(job_id, worker_id, ok, error=None, retry_after_seconds=None) -> bool`; add `prune(days)`.
- `app/workers/runner.py`: pass `self.worker_id` to every `finish` call; log a warning on `False` (the lease was lost or the job row is gone).
- `app/repositories/documents.py`, `ChunkRepository.replace`: upsert all rows on `id` in batches, then delete this document's rows with `chunk_index >= len(rows)` (indexes are contiguous from 0). Keep the `user_id` filter.
- `DocumentRepository.create` and `ConversationRepository.create`: map the database `quota_exceeded` error to `AppError("quota_exceeded", ..., 429)`.
- `app/workers/manager.py`: call pruning from the hourly janitor throttle. A pruning failure must not stop orphan cleanup.
- `app/core/config.py`: `job_retention_days`.

### D. Docs
- `docs/API.md`: the behavior in A; the conversation limit; to replace a registered PDF, delete the document through the API and upload under a new id.
- `docs/DEPLOYMENT.md`:
  - Apply migrations in order and never rerun an earlier file alone; if one was rerun, rerun every later file.
  - New limits and `JOB_RETENTION_DAYS`. Raising `MAX_DOCS_PER_USER` above `app_limits.max_documents` has no effect.
  - Index check for finding 11 on a real database: with `set enable_seqscan = off`, run `explain` on the query inside `user_storage_usage`. If the `name` prefix is not an index condition, plan a follow-up; do not change the query or add an index on `storage.objects` without that evidence.
- Update `README.md`, `CLAUDE.md`, `.env.example`, `render.yaml` and the status table in `docs/features/ebook_ai_pro_backend.md`.

## API / contract changes
| Endpoint | Before | After |
|---|---|---|
| `POST /documents`, new, at active-job cap | `429` | `201`, `status: "uploaded"`; the client calls `/process` later |
| `POST /documents`, own id, different `file_name` | `200`, old document | `409 conflict` |
| `POST /conversations` at the limit | unlimited | `429 quota_exceeded` |

Flutter must read `status` after registering and call `/process` for `uploaded` documents once earlier ones finish. Other shapes are unchanged.

## Configuration
`JOB_RETENTION_DAYS` (default 30, minimum 1).

## Risks and invariants
- **Deploy order:** apply the migration, then deploy the code right away. In between, one side calls a `finish_job` signature that does not exist; the job's lease expires and it runs again (ingestion is idempotent), using one attempt.
- **Immutability:** B1 makes the `CLAUDE.md` invariant true for registered files.
- **Commit-last ingestion:** the new `replace` still runs before the count check and the compare-and-set. It no longer leaves a document with zero chunk rows midway.
- **Citations:** `chunk_id` now survives a reindex. If chunking settings change, the same id can cover different text; the stored `page_number` and title remain the record of what was cited.
- **Existing data:** B5 fails if a row already has `file_name = '.'`. Users already above a new row limit keep their rows but cannot add more.
- **Queue fairness:** the per-user active-job cap still bounds how much one user can queue.
- Lazy imports, token-only `user_id` and the error envelope are unaffected.

## Acceptance criteria
- One user registers 8 PDFs in a row with `auto_process=true`: all return `201`; 5 are `queued`, 3 `uploaded`; none is refused.
- After the queue drains, `/process` on the 3 succeeds and all 8 are listed and searchable in `all_documents` mode.
- Reusing an id with another file name returns `409`; the first document is unchanged.
- A user cannot delete a registered object from storage but can delete an unregistered one. `DELETE /documents/{id}` and `DELETE /me` still remove files.
- `finish_job` from a worker that does not hold the lease returns `false` and changes neither job nor document.
- After a reindex, earlier `message_sources` keep their `chunk_id`; chunks beyond the new count are removed.
- A direct insert past either row limit is rejected by the database; the API reports `429`.
- Pruning removes old finished jobs and keeps each document's latest job.
- Both new indexes exist.

## Tests
Offline (`tests/pro/`):
- `fakejobs.py`: lease-checked `_finish_job` returning a bool; add `_prune_processing_jobs`.
- `fakedb.py`: add `upsert` and `gte`.
- Update `test_account.py` for the new `finish` signature; `test_quotas` must still pass.
- New: 8-PDF registration; `409` on id reuse; stale worker cannot finish; chunk upsert keeps ids and trims extras; database `quota_exceeded` maps to `429`; the janitor survives a pruning error.

SQL (`tests/pro/integration/test_rls_sql.py`): delete policy for registered and unregistered objects; `finish_job` lease check and privileges (update the existing signature at the privilege test); row-limit triggers; `file_name = '.'`; `chunk_id` after upsert; pruning; indexes. These tests have never been run: run them against a local Supabase (`supabase start && supabase db reset`) as part of this work.

## Out of scope
- Editing `20261124000000`, `20261125000000` or `20261126000000`.
- A batch upload endpoint, automatic queuing of `uploaded` documents, higher storage limits.
- Restoring `chunk_id` values already cleared.
- Rewriting the `supabase-expert` skill (written for another project).

## Assumptions / open questions
- "More than 1 PDF" means both one after another and many selected at once. If a second upload fails today in the app, we need the exact error: none of the limits above blocks a second small PDF.
- The Supabase project reachable from this workspace had no tables or recorded migrations on 2026-11-26. If that is the app's project, uploads cannot work until the migrations are applied.
- Over-cap documents wait for the client to call `/process`. If they should start on their own, that needs a worker change and a separate design.
- Defaults of 200 documents, 1000 conversations and 30 days retention are proposals.
