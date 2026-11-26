# Google and Facebook sign-in, and each user's own PDF space

Users sign in with **Google or Facebook through Supabase Auth** and get a private space for their PDFs. Supabase Auth runs the whole OAuth flow, so this backend never sees a Google or Facebook secret and gets no login endpoints. Unchanged: Supabase (Auth, Postgres, Storage), ChromaDB as the only vector store, Gemini, the existing `/api/v1` contracts (additions only), one uvicorn worker. The Flutter app is not in this repository: this spec covers the backend, the Supabase setup and the contract Flutter must follow.

**Status: implemented** (backend, migration, tests, docs). Not done: provider setup in the dashboards (manual), Flutter code, and running the SQL tests against a real Postgres. The open questions below are still open; the defaults were used.

**Deviations (from the security review):** (1) any abort of a running ingestion discards its vectors when the document is gone or `deleting`, so account deletion mid-ingestion leaks nothing; (2) the migration also redefines `claim_processing_job` and `finish_job` so they never revive a `deleting` document; (3) `POST /documents` re-checks the space limit, because the storage policy is only a pre-upload check (parallel or signed-URL uploads can exceed it until cleanup).

## Current behavior / problems
Already in place:
- `app/core/security.verify_token` verifies the Supabase access token (JWKS, issuer, audience, `role=authenticated`).
- Private space exists: RLS on all tables, and the bucket `user-documents` restricted to the folder `{auth.uid()}/` (`20261125000000_pro_schema.sql`).

Missing or weak:
1. No provider is configured or documented. Nothing says how to set up Google or Facebook.
2. `verify_token` accepts **any** authenticated Supabase user, including anonymous sign-ins (they also carry `role=authenticated`) and e-mail signups, if either is enabled in the dashboard.
3. **No storage limit per user.** The storage insert policy only checks the folder, so a signed-in user can upload any number of 50 MB files and never register them. `MAX_DOCS_PER_USER` only counts rows registered through the API.
4. Orphan uploads (uploaded, never registered, or left behind by a failed delete) are never cleaned up.
5. **No account deletion.** Facebook requires data-deletion instructions for apps using Facebook Login, and app stores require in-app account deletion.
6. The app can't show who is signed in or how much space is used.

## Required changes

### A. Supabase and provider setup (manual; documented in `docs/DEPLOYMENT.md`, new section)
- Enable the **Google** and **Facebook** providers. **Disable** anonymous sign-ins and e-mail/password signups.
- Google Cloud: OAuth consent screen (scopes `openid email profile`) and a Web OAuth client with redirect URI `https://<project-ref>.supabase.co/auth/v1/callback`. Client id and secret go into the Supabase dashboard only.
- Meta for Developers: app with the Facebook Login product, the same callback URL, the `email` and `public_profile` permissions, a privacy policy URL, a data-deletion instructions URL, and the app switched to Live mode.
- Supabase URL configuration: allow-list the app's redirect URI (`<app-scheme>://login-callback`, plus any web origin).
- Keep asymmetric JWT signing keys.
- Account linking: Supabase links identities that share a **verified** e-mail, so the same person using Google and then Facebook keeps one `user_id`, and therefore one space. A Facebook account with no e-mail becomes a separate user. Document both.

### B. Accept only the chosen providers (`app/core/security.py`, `app/core/config.py`)
- `AuthUser` gains `email: str | None`, `providers: tuple[str, ...]` and `name: str | None`, `avatar_url: str | None`.
  - `providers` comes from `app_metadata` (server-controlled).
  - `name` and `avatar_url` come from `user_metadata`. It is **user-editable**: display only, never used for any decision.
- A token with `is_anonymous: true` is rejected with `401 unauthorized`.
- New setting `AUTH_ALLOWED_PROVIDERS`, default `google,facebook`. If the token's providers don't intersect it, return `403 provider_not_allowed`. An empty value allows any provider (local development).
- A token with no `app_metadata` counts as having no provider, so it is rejected when the list is non-empty.
- Signature and claim checks are unchanged, and run **before** these checks.

### C. Storage quota and limits (new migration `supabase/migrations/20261126000000_user_space.sql`)
Never edit the applied migration.
- Table `public.app_limits`: one row, `max_storage_bytes bigint default 104857600` (100 MB) and `max_objects int default 220`. RLS on, `select` for `authenticated`, writes for `service_role` only.
- `public.user_storage_usage(p_user uuid) returns (bytes bigint, objects int)`: sums `(metadata->>'size')` over `storage.objects` in bucket `user-documents` under `p_user/`. `service_role` only.
- `public.can_upload() returns boolean`: `security definer`, `set search_path = ''`, uses `(select auth.uid())` only (no parameter, so a user cannot probe others). True while current bytes **and** objects are below the limits. `execute` for `authenticated`.
- Drop and recreate the policy `user_documents_insert` as the existing folder check **and** `public.can_upload()`.
- `public.list_user_objects(p_user uuid)` and `public.list_orphan_objects(p_older_than interval, p_limit int)`: `service_role` only. An orphan is a `user-documents` object with no `documents.file_path` equal to its name.
- The limit is checked before an upload, so one file (at most 50 MB) can overshoot it, and parallel uploads can overshoot by a few. `max_objects` bounds this.

### D. `GET /api/v1/me` (`app/api/v1/me.py`, `app/schemas/me.py`)
- Returns the identity from the token and the usage (see contract below).
- Usage is read with the service role, passing **only** the verified `user.id`.

### E. Account deletion: `DELETE /api/v1/me` (`app/services/account.py`)
Order, each step safe to repeat:
1. Set all of the user's documents to `deleting`. This hides them and makes a running ingestion abort at its commit.
2. Delete the user's vectors with a new `VectorStore.delete_user(user_id)` (`where user_id`). It must work even in `needs_reindex`, because deleting never mixes vectors.
3. Remove every object returned by `list_user_objects` with `supabase_storage.remove_object`. Each name must start with `{user_id}/`, otherwise skip it and log a security event.
4. Delete the auth user through the GoTrue admin API (`DELETE {SUPABASE_URL}/auth/v1/admin/users/{id}`, service role). Existing `on delete cascade` removes documents, chunks, jobs, conversations, messages and sources.

A failure at any step returns `503 account_deletion_incomplete` and leaves the account in place, so the user can retry. The user row is deleted **last**.

### F. Orphan cleanup (`app/services/maintenance.py`, `app/workers/manager.py`)
- The worker janitor, throttled to once an hour, removes up to 50 orphans older than `ORPHAN_MAX_AGE_HOURS` (default 24).
- The age threshold protects a PDF that is uploaded but not yet registered.

### G. Docs
`README.md`, `docs/API.md`, `docs/DEPLOYMENT.md` (provider setup, quota changes, privacy/data-deletion text to publish), `CLAUDE.md`, `.env.example`.

## API / contract changes
Additions only. One behaviour change: tokens from other providers or anonymous users are now refused.

| Endpoint | Success | Errors |
|---|---|---|
| `GET /api/v1/me` | `200 {"id","email","name","avatar_url","providers":["google"],"usage":{"documents":3,"storage_bytes":123,"storage_limit_bytes":104857600,"objects":3,"object_limit":220}}` | 401, 403 |
| `DELETE /api/v1/me`, body `{"confirm":"DELETE"}` | `204` | 401, 403, 422 (missing or wrong confirm), 503 `account_deletion_incomplete` |

- All routes may now return `403 provider_not_allowed`.
- `email`, `name` and `avatar_url` may be `null`.

### Flutter contract (documented in `docs/API.md`; implemented in the app repo)
- `supabase.auth.signInWithOAuth(OAuthProvider.google | OAuthProvider.facebook, redirectTo: '<app-scheme>://login-callback')` with PKCE. Register the deep link on Android and iOS.
- Only the Supabase URL and publishable key are in the app. No provider secrets.
- Send the current `session.accessToken` as `Authorization: Bearer`. Let the SDK refresh it; on `401` refresh once, then sign out.
- Upload path stays `{user.id}/{document_id}/{file_name}` with `upsert: false`. A refused upload (storage policy error) means the space is full: show `/me` usage.
- Account screen: usage from `/me`, sign out (`supabase.auth.signOut()`), delete account (confirm dialog, then `DELETE /me`, then sign out locally).

## Configuration
- New: `AUTH_ALLOWED_PROVIDERS=google,facebook`, `ORPHAN_MAX_AGE_HOURS=24`. Add both to `.env.example` and `render.yaml`.
- Storage limits live in `public.app_limits` (change with SQL), not in env vars, so the policy and the API can't disagree.
- No Google or Facebook credentials in this repo or on Render.

## Risks and invariants
- **User id only from the verified token:** `/me` and account deletion take no id from the request.
- **Service role after ownership:** every new service-role call is scoped to `user.id`; object names are prefix-checked.
- **`user_metadata` is untrusted:** never used for authorization.
- **Stateless JWT:** after deletion the old access token stays valid until it expires (about 1 hour). API writes then fail on the foreign key to `auth.users`, but a storage upload could still succeed; the orphan janitor removes it. State this in `DEPLOYMENT.md`.
- **Lockout:** turning on provider enforcement rejects existing e-mail test users. Set `AUTH_ALLOWED_PROVIDERS` accordingly on dev.
- **RLS function in a policy:** `can_upload()` is `security definer` and runs on every upload. It filters by bucket and prefix.
- Lazy imports, single worker, error envelope: unchanged.

## Acceptance criteria
- A Google or Facebook token works on all routes. An anonymous token gets 401, and an e-mail-provider token gets 403 `provider_not_allowed`.
- A user whose usage is at the limit cannot upload to storage; another user still can.
- `/me` shows only the caller's usage.
- After `DELETE /me`: no vectors, objects, rows or auth user remain for that user, and another user's data is untouched.
- A failed step leaves the auth user present, and a retry completes.
- An unregistered object older than the threshold is removed; a newer one and registered ones are kept.

## Tests (offline, `tests/pro/`)
- `test_jwt.py`: google and facebook accepted, linked providers accepted, anonymous 401, e-mail 403, missing `app_metadata` 403, empty allow-list accepts, forged `user_metadata.provider` ignored. Update the existing token fixtures to carry `app_metadata`.
- `test_account.py`: `/me` shape and two-user isolation; confirm validation; deletion order and cascade (fakes); failure at each step then retry; foreign object name skipped; the other user is untouched.
- `test_vector_store.py`: `delete_user`, including in `needs_reindex`.
- `test_ingestion_and_worker.py`: orphan cleanup age and registration rules.
- `test_streaming_and_openapi.py`: add `/me`; every user route still requires auth.
- `integration/test_rls_sql.py`: `can_upload()` at and under the limit, the new insert policy, and RPC privileges.

## Out of scope
- Flutter code. Sign in with Apple. E-mail/password and magic links. A `profiles` table. Roles, paid plans and per-user quota overrides. Sharing documents. A Facebook data-deletion **callback** endpoint (the instructions URL is used instead). Local `supabase/config.toml` provider setup.

## Assumptions / open questions
Assumptions:
1. Only Google and Facebook; e-mail and anonymous sign-ins are off.
2. The app uses Supabase's browser OAuth flow (one code path for both providers), not the native Google/Facebook SDKs.
3. 100 MB per user. Supabase's free tier offers about 1 GB of storage in total, so that is roughly 10 full users.
4. Verified-e-mail identity linking is wanted.

Open questions:
- **Will you publish on iOS?** Apple's guideline 4.8 generally requires an equivalent privacy-preserving login (usually Sign in with Apple) when you offer Google or Facebook login. If so, add `apple` to the providers.
- Is 100 MB per user right?
- What are the app's deep-link scheme and package id?
- Where will the privacy policy and data-deletion pages be hosted? Facebook needs both URLs before going Live.
