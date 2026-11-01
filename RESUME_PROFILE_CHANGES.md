# Resume-driven profile backend changes

## What changed

- Added DOCX text extraction with `python-docx`, including paragraphs and tables.
- Added DOCX ZIP-signature validation and clear errors for malformed/empty DOCX files.
- Added a `manual_fields` column to the profile table. Startup applies an additive migration, so existing Neon profile rows are retained.
- `PUT /profile` records which fields the user manually changed.
- Uploading a newer resume refreshes AI-extracted fields while retaining manually edited fields.
- `POST /resume/reextract` is treated as an explicit reset: it refreshes all fields from the current resume and clears the manual-override markers.
- Re-extraction now runs in FastAPI's thread pool so synchronous LLM work does not block the event loop.
- Added `python-docx` to `requirements.txt`.

## Deployment

1. Deploy the updated backend code.
2. Install updated dependencies (`pip install -r requirements.txt`).
3. Restart the API. `Database.init()` adds `profile.manual_fields` with `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`; no manual Neon SQL step is required.

## API behaviour

- `POST /resume`: extracts profile, stores resume, preserves fields previously edited via `PUT /profile`.
- `GET /profile`: returns structured profile and current resume metadata.
- `PUT /profile`: partially updates supplied fields and marks them as user-managed.
- `POST /resume/reextract`: intentionally replaces all profile fields with a fresh extraction from the latest uploaded resume.

## Validation status

Python compilation passed. The repository's existing API test fixture is from an older SQLite-based implementation and cannot run against this async Neon version: `create_app()` requires `DATABASE_URL`, and the test fixture passes a local SQLite path. A live Neon/LLM integration test was not run in this environment.
