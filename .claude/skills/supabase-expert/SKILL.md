---
name: supabase-expert
description: Supabase for Dama — supabase_flutter auth (anonymous guests, linkIdentity for Google/Facebook, session restore/refresh, OAuth deep links), Postgres migrations, Row Level Security, triggers, RPCs, Realtime, and Edge Functions, with the security rules this project enforces. Use whenever touching lib/core, lib/features/*, supabase/migrations, dart_defines, OAuth/redirect config, or designing any online feature (multiplayer, currency, marketplace).
---

# Supabase expert (Dama)

Dama is **offline-first**. Supabase adds identity and, in later milestones, online
features. Nothing Supabase-related may ever block launching or playing offline.

## Configuration

- Credentials come only from `--dart-define-from-file=dart_defines.json`
  (git-ignored; template `dart_defines.example.json`) → `SupabaseConfig.url/anonKey`.
- Only the **publishable/anon** key ships in the app. The **service-role key never**
  appears in Flutter code, defines, or the repo.
- `SupabaseConfig.isConfigured == false` ⇒ app runs as pure offline
  (`AuthStatus.unavailable`). Every new feature must handle that state.
- OAuth redirect: `com.dev.kamalelmagouri.dama://login-callback` — must match the
  Android intent filter in `AndroidManifest.xml` **and** the dashboard "Redirect URLs".

## Auth model (Milestone 1 — the identity foundation)

- First launch: restore persisted session → else `signInAnonymously()` (exactly once;
  concurrent calls share one future — see `AuthController._ensureSession`).
- Guest → linked via `auth.linkIdentity(OAuthProvider.google|facebook, redirectTo: …)`.
  **Never** `signInWithOAuth` for a guest — that replaces the user. The UUID must be
  unchanged after linking; verify it (`identityMismatch` notice otherwise).
- `identity_already_exists` ⇒ show an error, keep the guest session. Never merge or
  switch accounts automatically.
- Logout (linked only): confirm → `signOut()` → new guest. Never delete the account.
- Guest vs linked is derived from the **verified user** (`is_anonymous` + `identities`),
  never from the client-editable `profiles.account_type`.
- Offline/network failure ⇒ `AuthStatus.recoveryRequired` + periodic retry; never
  pretend a local-only session is a Supabase account.
- Never log tokens, sessions, or full `User` objects.

## Access control (the rule for every future online feature)

- Client check: `AuthController.canAccessOnlineFeatures` (linked + healthy session).
  This is UX only.
- Server check (authoritative): RLS policies / RPCs use
  `public.is_linked_user()` (reads the JWT's `is_anonymous`) **and**
  `user_id = (select auth.uid())`.
  ```sql
  create policy "matches_insert_linked" on public.matches
    for insert to authenticated
    with check (public.is_linked_user() and host_id = (select auth.uid()));
  ```
- A valid anonymous JWT is **authenticated but not trusted** for online features.

## Migrations

- One file per change: `supabase/migrations/NNNN_<snake_name>.sql`, next number after
  the highest existing one. **Never edit an applied migration** — add a new one.
- Idempotent where practical: `create … if not exists`, `drop policy if exists` +
  `create policy`, `create or replace function`, `on conflict do nothing`.
- Every new table: `enable row level security` + `force row level security`, explicit
  `revoke all … from anon, authenticated`, then grant only what is needed (prefer
  column-level `grant update (col, …)`).
- `security definer` functions: always `set search_path = ''` and schema-qualify
  every identifier (`public.profiles`, `auth.users`).
- Use `(select auth.uid())` (wrapped) in policies for per-statement caching.
- Server-owned columns (ids, ownership, balances, timestamps, status) are set by
  triggers/RPCs and protected by a `before update` guard trigger (see
  `public.profiles_guard`).
- Each migration's header comment states the milestone and what it does.
  Apply with the SQL editor or `supabase db push`; record which in the done entry.

## Money, currency, marketplace, multiplayer (future milestones)

- Balances and ownership change **only** inside `security definer` RPCs or Edge
  Functions that validate inputs, check `is_linked_user()`, and write a ledger row in
  the same transaction. Clients never `update` a balance column.
- Purchases: verify Google Play receipts server-side (Edge Function) before crediting.
- Multiplayer: server is authoritative. Moves are submitted as data to an RPC that
  re-runs the Dama rules (same tables as `lib/game/board.dart`) and rejects illegal
  or out-of-turn moves; Realtime channels broadcast results, not trust.
- Rate-limit and validate everything that costs money or affects other players.

## Flutter integration

- Wrap SDK calls behind an interface in `lib/core/services/` or a feature
  `repositories/` file; map exceptions to project types (`AuthFailure`).
- Controllers depend on the interface; tests inject fakes — tests must never call
  the real Supabase.
- `select()` only the columns you need; `.maybeSingle()` when a row may be missing
  (RLS can hide it).
- Treat profile/remote data as cosmetic or retryable: a failed fetch must not reset
  identity or break the screen.

## Manual setup is part of "done"

When a feature needs dashboard work (providers, redirect URLs, Google Cloud OAuth
client, Facebook app, secrets for Edge Functions), list the exact steps in the done
entry under **Manual setup** — never invent client IDs, secrets, or URLs.
