# Remove the remaining OCR references

OCR was removed with the old single-library service: there is no OCR module, endpoint, dependency or Docker step. What is left is wording that still describes it, one user message that promises it, and one false statement in the deployment doc. Remove those and record that OCR is not supported. Unchanged: ingestion behavior, `error_code` values, API routes and response shapes, vector store, LLM, host, Flutter contract.

## Current behavior / problems
1. `app/services/ingestion.py` (about line 111) fails a PDF with no text as `no_extractable_text` with "No text found in this PDF (scanned PDFs are not supported yet)". "Yet" promises OCR. Users see this message.
2. `docs/DEPLOYMENT.md` section 9 lists OCR as an upgrade and says "EasyOCR is already a dependency". It is not in any requirements file.
3. `docs/features/ebook_ai_pro_backend.md` lists OCR under "Not built".
4. `CLAUDE.md` lists OCR among the known gaps.
5. `.claude/agents/security-reviewer.md` describes a service that "accepts image uploads, runs OCR" and checks EasyOCR parameters, so reviews look for code that does not exist and miss what does (PDF registration, storage paths, JWT, RLS).
6. `.claude/agents/rag-pipeline-reviewer.md` and `.claude/skills/do-feature/SKILL.md` refer to `ocr_service.py`, `/upload`, `easyocr` and `rag_service.py`.

## Required changes
### A. User message (`app/services/ingestion.py`)
- New text: "No text found in this PDF (scanned or image-only PDFs are not supported)". Keep the code `no_extractable_text` and permanent failure (no retry).

### B. Docs
- `docs/DEPLOYMENT.md` section 9: delete the OCR item and the EasyOCR claim.
- `docs/features/ebook_ai_pro_backend.md`: drop OCR from "Not built"; add under the deviations: "OCR is not supported (owner decision); scanned PDFs fail with `no_extractable_text`."
- `CLAUDE.md`: drop OCR from the known gaps. Keep the sentence saying the old OCR `/upload` was removed on purpose and must not return.

### C. Agent and skill files (`.claude/`)
- `security-reviewer.md`: rewrite the scope for the current service (PDF documents registered from Supabase Storage, `app/`, JWT, RLS and service-role ownership checks, error leakage, CORS). Remove image, PIL and EasyOCR checks.
- `rag-pipeline-reviewer.md`: remove `ocr_service.py`, `rag_service.py`, `easyocr` and the OCR reader cache; point to `app/services/`.
- `do-feature/SKILL.md`: remove the `ocr_service.py`/`/upload` bullet and `easyocr`; point to `app/`.

## API / contract changes
None. Only the human-readable `error_message` changes; `error_code` stays `no_extractable_text`. A client that matches on the message text (instead of the code) would need updating.

## Configuration
None. No Render change.

## Risks and invariants
- Lazy heavy imports: unchanged. Keep `"easyocr"` in the heavy-import list of `tests/pro/test_repositories_and_api.py`: it is a cheap guard against reintroduction.
- Documents that already failed keep their old stored message.

## Acceptance criteria
- A PDF without extractable text ends `failed` with `error_code=no_extractable_text` and the new message.
- Searching for `ocr` (any case) outside `docs/features/` matches only: the "removed on purpose" sentence in `CLAUDE.md` and the guard in `test_repositories_and_api.py`.
- No document claims EasyOCR is a dependency.
- `pytest` passes.

## Tests
- `tests/pro/test_ingestion_and_worker.py` (about line 178): still asserts `"scanned"` in the message; also assert `error_code == "no_extractable_text"` and that "yet" is not in it.

## Out of scope
- Adding OCR or any replacement (including sending page images to Gemini).
- Historical sections of existing specs (analysis, legacy descriptions): they record what was true then.
- The Flutter app.

## Assumptions / open questions
1. "Remove OCR" means cleaning up what remains in this backend. If it means an OCR feature or message in the Flutter app, that is a separate task in that repository.
2. Rewording the message is wanted. If the app shows its own text for `no_extractable_text`, change A is optional.
3. Do you want scanned PDFs rejected earlier, at upload time? Not planned: it needs the file to be opened, which today happens only in the worker.

## Implementation notes
Status: implemented. Deviations:
- `.claude/skills/plan-feature/SKILL.md` also mentioned OCR (missed above) and was updated the same way.
- The "old library service was removed" note in both skill files still names `ocr_service.py`: it records the removal, like the sentence in `CLAUDE.md`.
- Both review agents were rewritten for the current service, not just stripped of OCR lines.
