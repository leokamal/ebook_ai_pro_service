# Share a PDF with another user by its id

**Status: implemented** (migration, backend, offline tests, SQL tests on plain Postgres with a stand-in `auth`/`storage` schema). Not done: applying the migration to the real project, Flutter code.

**Deviations:** (1) `user_storage_usage` is unchanged: the uploader stays charged for a shared file even after deleting their row, because freeing it would allow unlimited storage by sharing to a second account. (2) The delete rule is a RESTRICTIVE policy `user_documents_delete_in_use` plus `path_in_use()`, so rerunning the first migration cannot undo it. (3) A trigger reserves a `content_id` while any holder exists, so nobody can register that id again and overwrite shared vectors. (4) `VectorStore.delete_document` now also works in `needs_reindex`. (5) A worker sweep (`Maintenance.resume_rebuilds`, every 60 s) re-queues shared content whose rebuild job was lost with its holder, and marks waiting holders `failed` when the rebuild failed.

The owner of a PDF shares it and gives its id to another user. The other user enters the id and the PDF appears in their library, ready to chat and search. The PDF file and its vectors are **not copied**: both users use the same stored file and the same embeddings. Unchanged: Supabase, ChromaDB as the only vector store, the existing Chroma collection (**no reindex needed**), Gemini, one uvicorn worker, existing API and RPC contracts (additions only).

## Terms
- **Content**: one stored PDF and its vectors, identified by `content_id` and `content_owner_id` (the original document id and uploader id).
- **Holder**: a `documents` row that uses a content. The uploader's row is a holder; each accepted share adds one.

## Current behavior / problems
1. Everything is keyed to one user: `documents` has `check (file_path = user_id/id/file_name)`; the storage SELECT policy allows only your own folder.
2. Chroma metadata holds the uploader's `user_id` and `document_id`, and `ChromaVectorStore.query` filters on the caller's `user_id`, so nobody else can reach the vectors.
3. `IngestionService.delete_document` removes vectors and file unconditionally; `AccountService.delete` calls `store.delete_user(user_id)` and removes the whole folder. With sharing, both would destroy other users' copies.
4. Policy `user_documents_delete` allows deleting a file when no `documents` row refers to it, but that subquery runs under the caller's RLS. With sharing, an uploader who deleted their row would not see recipients' rows and could delete (then re-upload a different) shared file.

## Design
A recipient gets **their own row** (own id, title, conversations) that points at the same content. They also get their own `document_chunks` rows (ids and page numbers only, no text or vectors), so citations, foreign keys and RLS work as today. Retrieval searches the content's vectors and translates each hit to the holder's ids.

## Required changes

### A. Migration `supabase/migrations/20261201000000_document_sharing.sql` (apply before deploying code)
- `documents`: add `content_id uuid` and `content_owner_id uuid` (no foreign key: they must survive deletion of the uploader). Backfill `content_id = id`, `content_owner_id = user_id`, then `not null`. Index on `content_id`.
- A `before insert` trigger sets both from `id`/`user_id` for every insert made by `authenticated` (clients have no column grant on them and must not choose them).
- Replace the path check with `file_path = content_owner_id::text || '/' || content_id::text || '/' || file_name`. The old constraint is unnamed: find it in `pg_constraint` by its definition.
- Unique `(user_id, content_id)`: one user holds a content once.
- `documents`: add `sharing_enabled boolean not null default false`. No client column grant: only the RPCs change it.
- RPCs (`security definer`, `set search_path = ''`, `auth.uid()` + `is_allowed_user()`, execute for `authenticated` only):
  - `share_document(p_document_id) returns uuid`: caller must own the row **and** be the uploader (`content_owner_id = auth.uid()`), document `completed` + `indexed`. Sets `sharing_enabled = true` and returns the id to hand over.
  - `stop_sharing_document(p_document_id)`: sets it to `false`. Copies already accepted stay.
  - `accept_shared_document(p_document_id) returns uuid`: locks the uploader's row (`id = p_document_id`) `for update`; refuses unless it has `sharing_enabled` and is `completed` + `indexed`. If the caller already holds the content, returns that row's id. Otherwise inserts the new row (copy of `title, file_name, file_path, file_size, page_count, language, embedding_model, chunk_count, content_id, content_owner_id`; `status='completed'`, `index_state='indexed'`, `sharing_enabled=false`) and its `document_chunks` with `id = uuid_generate_v5(new_id, chunk_index::text)` (must equal Python `chunk_id`). One transaction.
  - Errors: `not_found` (unknown id, not shared, or not usable: same answer for all), `conflict` (`share_document` on an unindexed document), `forbidden`, `quota_exceeded` (the existing `documents_row_limit` trigger).
- Storage:
  - SELECT policy also allows an object when the caller has a `documents` row with that `file_path`.
  - DELETE policy uses a `security definer` function that sees **all** users' rows: your folder and no row anywhere refers to the file.
- Space: unchanged, a file counts for the user whose folder holds it (see deviation 1).

### B. Retrieval (`app/services/retrieval.py`, `vector_store.py`, `app/repositories/documents.py`)
- `DOC_COLUMNS` and `list_searchable` include `content_id, content_owner_id`.
- `VectorStore.query` takes `{content_id: content_owner_id}` built from the Postgres-validated documents. It filters on those content ids and drops any hit whose `(document_id, user_id)` metadata pair is not in the map (same security log as today).
- `RetrievalService.search` maps each hit back to the caller's document: `document_id`, `chunk_id = chunk_id(holder_id, chunk_index)`, `document_title` = the holder's title. The content ids never reach the prompt, citations or API responses.

### C. Deletion (`ingestion.py`, `account.py`, `repositories/`)
- New cross-user, service-role method `other_live_holders(content_id, excluding_document_ids)`: rows with that content and `status <> 'deleting'`.
- `delete_document`: delete the row always; delete vectors and file (path pinned to `content_owner_id/content_id/file_name`) only when there is no other live holder. `_discard_if_gone` follows the same rule.
- Account deletion: remove `delete_user` vector wipe; delete vectors per content that has no live holder outside this account, including contents the user uploaded and had already deleted from their library. `_remove_files` skips files another user's row refers to. The auth user is still deleted last.
- No race with accept: accept needs a live source row under lock, so once every holder is `deleting` nobody can join.

### D. Ingestion, reindex, reconcile (`ingestion.py`, `maintenance.py`)
- `safe_storage_path` pins the path to `content_owner_id/content_id/file_name`; Chroma writes use the content keys. A normal upload behaves exactly as today.
- A reindex job runs on any one live holder and, on success, updates `index_state`, `chunk_count`, `page_count`, `embedding_model` and `document_chunks` of **every** live holder of the content.
- `reindex_all` and `reconcile` mark all holders, but queue one job per content.

### E. API and docs
- `DocumentOut` gains `shared: bool` (`content_owner_id != user_id`).
- No new HTTP routes: the app already calls Supabase RPCs for everything except chat streaming.
- Update `docs/API.md`, `README.md`, `docs/DEPLOYMENT.md` (migration order), `CLAUDE.md` (the invariants on storage paths, immutability and ownership), and the backend spec status table.

## Client contract (Flutter)
```
final sharedId = await supabase.rpc('share_document', params: {'p_document_id': id});          // show / copy
final newId = await supabase.rpc('accept_shared_document', params: {'p_document_id': sharedId}); // other user
await supabase.rpc('stop_sharing_document', params: {'p_document_id': id});
```
The recipient reads and downloads the PDF with the row's `file_path`, deletes it with `request_document_delete`, and never uploads anything. Text that is not a UUID is rejected by the app before the call.

## Risks and invariants
- **Changes an invariant:** "storage path is `{user_id}/{document_id}/{file_name}`" becomes the content's owner and id. Still built only server-side and pinned before any download.
- **Ownership:** the user still reaches content only through their own row, checked by RLS before Chroma. The Chroma filter loses its `user_id` clause, so the post-query pair check is mandatory and tested.
- **Immutability:** no UPDATE storage policy; the new DELETE rule keeps shared files from being swapped.
- **Privacy:** while sharing is on, anyone with the id gets the whole PDF, and it cannot be taken back from them. A recipient can pass the id on (it is also inside their `file_path`); `stop_sharing_document` ends that. The recipient can also see the uploader's user id there, nothing else about them.
- **Guessing:** document ids are random UUIDs and cannot be guessed.
- No new heavy imports; `pysqlite3` swap untouched.

## Acceptance criteria
- A shares and B enters the id: B sees a `completed` document, can search and chat, and citations carry B's document and chunk ids.
- Accepting creates no new storage object, no new vectors and no processing job.
- Accepting twice (or the uploader entering their own id) returns the same row.
- A deletes the document or their account: B's copy still opens, searches and chats. When the last holder deletes, vectors and file are removed.
- A cannot delete the shared file directly from Storage after deleting their row; a user without a row cannot read it.
- An unknown id, a document never shared and a document no longer shared all give `not_found`; a recipient cannot call `share_document` on their copy.
- After `reindex`, all holders are searchable again and the content was embedded once.
- Existing documents work after the migration without reindexing.

## Tests
- `tests/pro/test_sharing.py` (offline; extend `fakedb`, `fakechroma`): hit translation, foreign pair dropped, delete by one of two holders, delete by the last, account deletion of the uploader, reindex with two holders.
- `tests/pro/integration`: the three RPCs, no client write access to `sharing_enabled`, both storage policies, usage accounting, row limit, `uuid_generate_v5` equal to `chunk_id`.

## Out of scope
Sharing conversations or whole libraries, sharing with a named user or by e-mail, separate share codes, notifications, links, expiry, a list of who accepted, Flutter code.

## Assumptions / open questions
**Open risk (not fixed, owner to decide):** if the uploader deletes their *account*, the kept file counts toward nobody's space. Someone could repeat upload, share, delete account to pile free storage onto a second account (bounded by that account's 200-document limit). Closing it means charging holders for files whose uploader no longer exists.

Defaults chosen; change before implementing if wrong.
1. **The id handed over is the document id**, and it works only after the owner turned sharing on.
2. **Recipients keep their copy** when the uploader deletes the document or account. The alternative (it disappears for everyone) is simpler but surprises recipients. If chosen, the privacy policy must say that shared files outlive the uploader's account.
3. Only the uploader can turn sharing on or off.
4. Sharing stays on until the uploader turns it off or deletes the document (once their row is gone, nobody new can join).
5. A shared copy counts toward the recipient's document limit (200), not their storage space.
6. Sharing books may raise copyright questions; the terms of use should cover it.
