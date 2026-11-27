---
name: plan-feature
description: Turn a feature idea, bug, or optimization for the ebook_ai_service FastAPI backend (RAG over Chroma + Gemini, Supabase Storage, Render deploy) into a written feature spec at docs/features/<name>.md, without changing any code. Use this whenever the user says "plan", "spec", "design", "scope", "how should we add/change ...", or describes a backend change (new endpoint, retrieval/chunking change, index lifecycle, auth, deploy, performance) before it is built — even if they never say "plan-feature". The spec it writes is what /do-feature implements.
---

# plan-feature

> **Note (E-Book AI Pro only):** the old library service (`rag_service.py`, `ocr_service.py`, `/query*`, `VERSION.json` publishing, `chroma_db_local/`) was removed. Ignore those invariants below; the current ones are in `CLAUDE.md` and apply to `app/`. Specs live in `docs/features/`; keep README/API/DEPLOYMENT/spec status in sync.

Write a spec precise enough that someone (usually `/do-feature`) can implement it without re-deriving the design. Planning is read-only: do not edit source files, `CLAUDE.md`, or `docs/API.md` here. The only file you create is the spec.

## Why this shape

This service has fragile invariants (lazy heavy imports, the `pysqlite3` swap at the top of `main.py`, atomic index swap with `VERSION.json` uploaded last, a path-traversal guard on downloads, locks around in-memory state). A vague plan breaks them. A good spec names the invariants it touches and says how each is kept, so the implementer doesn't have to guess.

## Steps

1. **Understand the request.** Input is the text after the command (or the conversation). If the request is ambiguous in a way that changes the design (e.g. streaming vs. non-streaming, public vs. admin-only), ask at most 2-3 short questions. Otherwise pick the sensible default and record it under *Assumptions* rather than interrogating the user.

2. **Ground it in the code.** Read `CLAUDE.md` first, then only the files the feature touches (`main.py`, `app/`, `supabase_storage.py`, `security.py`, `docs/API.md`, `tests/`, `supabase/migrations/`). Skim existing specs in `docs/features/` to match tone and depth. Cite real functions and line-level behavior ("`get_query_chain()` calls `load_database_from_supabase(..., force=True)`"), not guesses.

3. **Check the constraints.** For each, note whether the feature touches it and how it stays safe:
   - Heavy imports (torch, chroma, fitz, gemini) stay lazy inside functions.
   - `pysqlite3` swap stays first in `main.py`.
   - Queries never call Supabase; index download/swap is under `_update_lock`, in-memory swap under `_lock`.
   - Publish order: upload index files, `VERSION.json` last, delete stale remote files only after success.
   - Storage object names are traversal-checked before writing to disk.
   - Secrets come from env vars only; admin endpoints use `X-Admin-Token` (`security.verify_admin_token`).
   - `chroma_db_local/`, `config/`, `.env` stay uncommitted.
   - Flutter is the only client: API changes must keep existing response shapes (`GET /query-database/` -> `{"response": ...}`) unless the spec says otherwise.
   - Render host is memory-constrained (~1-2 GB) and the free tier cold-starts.

4. **Write `docs/features/<snake_case_name>.md`** using the template below. Keep it as short as the feature allows; omit sections that don't apply instead of padding them.

5. **Report back** in a few lines: the path, the 3-5 key decisions, open questions, and "run `/do-feature <name>` to implement".

## Spec template

```markdown
# <Feature title>

<One paragraph: goal, and the hard constraints (what must NOT change: vector store, LLM, host, client).>

## Current behavior / problems
1. <What the code does today and why it hurts, with function names.>

## Required changes
### A. <Area> (<file>)
- <Concrete, testable change.>
### B. ...

## API / contract changes
<Endpoints, request/response/SSE shapes, status codes. State backward compatibility explicitly.>

## Configuration
<New env vars, defaults, Render settings.>

## Risks and invariants
<Which CLAUDE.md invariants are touched and how they are preserved. Concurrency, memory, cold start.>

## Acceptance criteria
- <Observable, verifiable statement.>

## Tests
<pytest cases to add; they must run with no network and no model downloads (mock supabase_storage, retriever, LLM).>

## Out of scope
<Files/behaviors deliberately not changed.>

## Assumptions / open questions
```

## Quality bar

- Every requirement is verifiable: an acceptance criterion or a test maps to it.
- Name files and functions; avoid "improve" or "optimize" without a measurable outcome.
- Prefer the smallest design that satisfies the request; call out what you chose not to build.
