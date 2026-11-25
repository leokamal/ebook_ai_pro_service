---
name: do-feature
description: Implement a feature, fix, or optimization in the ebook_ai_service FastAPI backend from a spec in docs/features/ (written by /plan-feature) or from a direct description, then test it, review it, and update docs. Use this whenever the user says "implement", "build", "do the feature", "execute the plan", "apply docs/features/...", or asks for a code change to main.py, rag_service.py, ocr_service.py, supabase_storage.py or security.py — even if they don't name the skill. Do not use it for planning only (use plan-feature) or for pure questions.
---

# do-feature

> **Note (E-Book AI Pro only):** the old library service (`rag_service.py`, `ocr_service.py`, `/query*`, `VERSION.json` publishing, `chroma_db_local/`) was removed. Ignore those invariants below; the current ones are in `CLAUDE.md` and apply to `app/`. Specs live in `docs/features/`; keep README/API/DEPLOYMENT/spec status in sync.

Implement a spec end to end: code, tests, docs, review. The spec is the contract; this skill is how to honor it without breaking this service's invariants.

## Steps

1. **Locate the spec.** Argument is a path or a feature name; resolve it to `docs/features/<name>.md`. If only a free-text request is given and no spec exists, either write a short inline plan (small change) or tell the user to run `/plan-feature` first (anything touching the index lifecycle, endpoints, or auth). Read the spec fully, then `CLAUDE.md`, then only the source files you will change.

2. **Make a checklist** from the spec's lettered sections and acceptance criteria (a todo list). Work through it in order; tick items only when verified, not when written.

3. **Implement in small, reviewable edits.** Respect these invariants (each exists because breaking it has caused or would cause outages):
   - Keep heavy imports (torch, easyocr, chroma, gemini) lazy inside functions so the port binds fast.
   - Keep the `pysqlite3` swap as the very first block of `main.py`.
   - Queries use the local Chroma only; never call Supabase on the query path.
   - Index changes: download/generate in a temp dir on the same filesystem, verify, then swap atomically. `_update_lock` serializes download/generate; `_lock` guards the in-memory swap.
   - Publishing: upload index files first, `VERSION.json` last, delete stale remote files only after success.
   - Preserve the path-traversal guard on downloaded object names.
   - No hardcoded secrets; read env vars. Admin endpoints use `Depends(verify_admin_token)`.
   - Do not touch `ocr_service.py` or `/upload` unless the spec says so.
   - Never stage or commit `.env`, `config/`, `chroma_db_local/`.
   - Don't leak internals through `HTTPException(detail=str(e))` in new code; log the error, return a generic message.

4. **Write tests alongside the code** in `tests/`. They must run with no network and no model downloads: mock `supabase_storage`, the retriever, and the LLM; use `tmp_path` for index directories. Match the style of `tests/test_index.py` and `tests/test_admin_and_sse.py`. Add any new test dependency to `requirements-dev.txt`.

5. **Run the tests.** `python` is not on PATH in this Windows shell; use the launcher:
   ```
   py -m pytest -q
   ```
   Fix failures at the cause. If you cannot run something (needs network, model, real Supabase), say so explicitly in the summary rather than implying it passed.

6. **Review before finishing.** Delegate to the project subagents in parallel, scoped to what changed:
   - `security-reviewer` if you touched endpoints, uploads, auth, storage paths, CORS, or error handling.
   - `rag-pipeline-reviewer` if you touched `rag_service.py`, chunking, embeddings, index lifecycle, or retrieval.
   Fix high/medium findings or record why they're deferred.

7. **Update docs** that the change makes stale: `docs/API.md` for any endpoint/contract change (Flutter relies on it), the Architecture / env-var / Render sections of `CLAUDE.md`, and the spec itself if you deviated (note the deviation and why).

8. **Report.** Summarize: what changed (by file), how to verify locally (exact commands, including a sample `curl`), test results, anything not verified, and deviations from the spec. Do not commit or push unless asked.

## Judgment calls

- If the spec conflicts with an invariant above, stop and ask; don't silently pick one.
- If the spec is silent, choose the smallest behavior-preserving option and note it in the report.
- Keep diffs focused: no drive-by refactors, no reformatting of untouched code.
