# Refactor status — async Neon migration

## Implemented

- Replaced the synchronous psycopg database adapter with SQLAlchemy 2.x
  `AsyncSession` and `asyncpg`.
- Converted persistence operations to awaited async methods with short-lived
  sessions and explicit transaction scopes.
- Preserved the PostgreSQL table/column layout and additive `plan` column
  migration; no destructive migration or password-hash rewrite is performed.
- Converted API handlers and the auth service to await database operations.
- Moved password hashing/verification, resume extraction, study-plan generation,
  and the synchronous job pipeline off the event loop.
- Kept the pipeline's progress callback safe by scheduling async DB updates on
  the running event loop from its worker thread.
- Removed abandoned, unwired auth repository/dependency/route scaffolding that
  imported nonexistent modules and was not used by the active app.
- Added `NEON_SETUP.md` with Neon connection, permissions, pooling, backup,
  compute, and deployment guidance.

## Validation

- `python -m compileall -q jobhunt` — passed.
- `PYTHONPATH=. python -m pytest -q tests/test_auth_service.py tests/test_llm.py tests/test_skills.py`
  — 90 passed, 1 skipped.
- Full API integration tests have **not** been run: the existing tests use a
  legacy SQLite fixture, while this application now targets PostgreSQL/Neon.
- A live Neon connection was not available in this environment. Install the
  updated requirements (including `asyncpg`) and run the integration checks
  against a staging Neon branch before production deployment.

## Deployment caveats

- Set `DATABASE_URL` only on the FastAPI service; never in the React frontend.
- Use the existing Neon database/branch if preserving current data. Take a
  restore point before rollout.
- `create_app(db_path=...)` remains in the signature for source compatibility;
  it does not provide SQLite storage. Integration tests should use a dedicated
  PostgreSQL test database or an explicit separate SQLite adapter.
