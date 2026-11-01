# Neon setup for the async API

The API now uses SQLAlchemy 2.x `AsyncSession` with the `asyncpg` driver. The
existing PostgreSQL schema is retained; startup runs idempotent `CREATE TABLE IF
NOT EXISTS` statements and adds `study_plans.plan` if it is missing. Existing
users, scrypt password hashes, sessions, resumes, study plans, runs and jobs are
not intentionally dropped or rewritten.

## 1. Neon project

1. In Neon Console, use the project/database already containing the Job Hunt
   data. Do **not** create a fresh database if you need to retain existing data.
2. Keep the API and Neon compute in the same or nearest available region.
3. Open **Connect**, select the correct branch and database, and copy the
   connection URI. For a continuously running hosted API, prefer the **pooled**
   URI (the hostname normally contains `-pooler`).
4. Set `sslmode=require` and store the complete URI in the hosting provider's
   secret/environment-variable settings as `DATABASE_URL`. Never commit it.
5. Use a database role that owns the application schema or has permission to
   create tables/indexes and read/write the application's tables. Startup
   schema initialization needs DDL permission.

## 2. Neon-side settings

- **No manual table creation is required** for a new database: application
  startup initializes the tables. For an existing database, the additive
  startup migration preserves current tables and adds only the missing
  `study_plans.plan` column.
- Do not reset, delete, or recreate the Neon branch/database as a migration
  step. Take a Neon restore point/backup before deploying the new version.
- Configure compute autosuspend to suit your traffic. Autosuspend saves cost
  when idle but the first request after suspension can have cold-start latency.
  For a latency-sensitive always-on app, choose a longer/no autosuspend only if
  the added compute cost is acceptable.
- Keep the API's connection pool modest. This build uses 3 pooled connections
  plus at most 2 overflow connections per API process. If you run multiple
  replicas, multiply that maximum by the replica count and keep it below your
  Neon compute/role connection limits.
- Do not expose the Neon connection string to the React frontend. It belongs
  only in the FastAPI service environment.

## 3. Deployment settings

Required:

- `DATABASE_URL` — Neon pooled PostgreSQL URI.
- `JWT_SECRET` — a long random secret (at least 32 characters).

Set the remaining LLM/provider variables used by your deployment as before.
Install dependencies with `pip install -r requirements.txt`; `asyncpg` is now
required and `psycopg` is no longer used by the API adapter.

## 4. Verify after deployment

1. Check service startup logs for successful lifespan/schema initialization.
2. Call `GET /health`.
3. Sign in with an existing account. Existing scrypt password hashes are
   intentionally retained and should continue to verify.
4. Check `/auth/me`, `/profile`, `/resumes`, `/study-plan`, `/runs/latest`,
   `/jobs` and `/stats` with a valid bearer token.
5. Upload a small test resume and run a search. Confirm progress updates and
   final run status, then verify jobs are visible after a service restart.

If the API cannot initialize the schema, check that `DATABASE_URL` points to
 the intended Neon branch/database and that the role has DDL permissions. Do
not solve permission errors by granting broad privileges to `PUBLIC`; use a
specific application role.

## Notes / limitations

- `db_path` is retained in the `create_app` signature for source compatibility
  but this deployment uses PostgreSQL; it is not a SQLite test database path.
- The background pipeline remains synchronous business logic, but it runs in
  FastAPI's worker thread pool. Database progress writes are awaited through
  the event loop and use `AsyncSession`.
- This repo's legacy API tests still assume SQLite fixtures. They need a
  PostgreSQL test database (or a separately maintained SQLite adapter) before
  they can serve as end-to-end regression tests for the Neon implementation.
