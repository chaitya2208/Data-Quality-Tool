# SQLite/Postgres → Snowflake Migration — Handoff

> Read this first in a fresh session continuing this migration. Written 2026-07-09.

## What was asked

Remove SQLite/SQLAlchemy entirely from the backend (`Data-Quality-Tool/backend`).
App storage (assets/scans/findings/rules/agent_runs/agent_tasks/recommendation_cache)
now lives in **Snowflake itself** — one schema (`DQ_APP`) inside the target database
(`PLAYGROUND_DB` by default), reusing the *same* SSO connection the app already used
for scanning source tables (`app/services/snowflake_session.py`). No second login,
no separate app database — just one schema.

This was a full rewrite, not a dialect swap: all 6 SQLAlchemy models, Alembic, and
every `db.query()`/`Session` call site across services/agents/API routes were
replaced with raw SQL through a new `app/services/storage.py` module.

## Current status: migration code-complete AND verified against real Snowflake

- `backend/snowflake/01_create_schema.sql`, `02_create_tables.sql`,
  `03_seed_default_rules.sql` — DDL, already run successfully via
  `python setup_db.py` against the user's real Snowflake account. All 7 tables
  exist, 15 default rules seeded.
- `backend/app/services/storage.py` — the raw-SQL data layer. Every entity has
  `_from_row()` → `SimpleNamespace` with lowercase snake_case attrs (mimics the old
  ORM model attribute names) so call sites read almost identically to before.
- All services (`rule_engine.py`, `dynamic_rules.py`, `scan_service.py`,
  `recommendation_cache_service.py`), all 7 agent files, all 6 API route files —
  rewritten off `db.query()`/`Session` onto `storage.*` calls.
- Deleted: `app/models/`, `alembic/`, `alembic.ini`, `docker-compose.yml`,
  `quick_start_sqlite.bat`, `demo_data.py`, `app/core/database.py`.
- `app/core/enums.py` (new) — plain Python enums for pydantic validation, moved out
  of the deleted models. **All enum values are lowercase** (`"active"`, `"pending"`,
  `"detected"`, etc.) — this matches the original SQLAlchemy enum convention and is
  what the frontend expects. Watch for this: mid-rewrite some code accidentally used
  UPPERCASE status strings (`"ACTIVE"`, `"PENDING"`) — this was caught and fixed with
  a bulk find/replace across `storage.py`, `scan_service.py`, `coordinator.py`,
  `auto_verify_scheduler.py`, `findings_agent.py`, `rule_intelligence_agent.py`,
  `agent_runs.py`, `rules.py`. If you see 400s/validation errors or rules silently
  not matching, check for a stray uppercase status literal first.

## Real bugs found and fixed by actually running this against Snowflake

1. **`CREATE INDEX` isn't supported on standard Snowflake tables** (only Hybrid
   Tables support secondary indexes). All `CREATE INDEX` statements were removed
   from `02_create_tables.sql` — Snowflake relies on automatic micro-partition
   pruning instead. If you're tempted to add indexes back, don't — it'll fail with
   `Unsupported feature 'Secondary index on non-hybrid tables is not supported'`.
2. **Inline SQL comments containing a semicolon broke statement splitting.**
   `setup_db.py`'s naive `.sql` file runner originally only stripped *whole-line*
   `--` comments before splitting on `;`. Two DDL lines had inline comments with a
   semicolon inside them (`-- "DATABASE" is reserved; column is DATABASE_NAME`),
   which got misread as a statement terminator and broke the `CREATE TABLE
   AGENT_RUNS` statement into garbage fragments. Fixed by stripping everything
   after `--` on *every* line, not just whole-comment lines (see
   `setup_db.py::_strip_line_comment`). If you add more DDL files, avoid inline
   comments with semicolons, or trust the current stripper (it handles it now).
3. **Insufficient privileges** — the initial Snowflake role (`PUBLIC`) lacked
   `CREATE SCHEMA` on `PLAYGROUND_DB`. User switched `SNOWFLAKE_ROLE` in `.env` to a
   role with rights. Not a code issue, but expect this on a fresh Snowflake account —
   `setup_db.py` will fail immediately with a clear `SQL access control error` if so.
4. **Frontend/backend port mismatch.** The frontend (`frontend/src/api/client.ts`)
   had `API_BASE_URL` hardcoded to `localhost:8000`, but the user's backend runs on
   `8001`. Fixed by making it read `import.meta.env.VITE_API_BASE_URL` (see
   `frontend/.env`, `frontend/.env.example`, `frontend/src/vite-env.d.ts`,
   `frontend/vite.config.ts`'s dev proxy). **`frontend/.env` is gitignored** and
   currently set to `http://localhost:8001/api/v1` on this machine — if the backend
   port changes, update that file, not the source.
5. **Stale localStorage run/batch IDs.** The Workflow page
   (`frontend/src/pages/AgentWorkflow.tsx`) persists the last run/batch ID in
   `localStorage` and tries to resume it on page load. After the DB migration wiped
   `AGENT_RUNS`, old IDs from before the migration caused 404 console spam on page
   load — not a backend bug, just stale client-side state. User is clearing it
   manually via DevTools. **Not yet fixed in code** — a good next task would be
   making `AgentWorkflow.tsx` auto-clear the stored key on a 404 instead of erroring
   forever (user explicitly declined this for now, chose manual clear instead — see
   if they want it done next session).

## Environment specifics (this user's machine)

- Backend runs from `backend/.venv` (NOT the system Python — system Python doesn't
  have `snowflake-connector-python` installed). Always run backend scripts as
  `./.venv/Scripts/python.exe <script>.py` from `backend/`, not bare `python`.
- Backend currently run on port **8001** (not FastAPI's default 8000).
- `.env` values in use: `SNOWFLAKE_DATABASE=PLAYGROUND_DB`,
  `SNOWFLAKE_APP_SCHEMA=DQ_APP`, `SNOWFLAKE_AUTH_METHOD=externalbrowser`,
  `SNOWFLAKE_ROLE=<a role with CREATE SCHEMA rights on PLAYGROUND_DB>` (not PUBLIC).

## Not yet done / possible next steps

- No data migration was needed (previous SQLite dev DB was disposable/empty) — this
  was a fresh schema, not a data carryover. If that ever changes, there's no
  migration-from-old-data path built.
- `AgentWorkflow.tsx` stale-localStorage-ID auto-recovery (see bug #5 above) —
  deferred, not fixed.
- The broader `MERGE_HANDOFF.md` (combining this codebase with a donor project
  `Claude/agentic-dq-platform`) is a **separate, larger, not-yet-started** effort —
  see that file for the 7 donor capabilities to port (SQL safety validator, PII
  classification, sample-first profiling, execution/alerts pipeline, feedback loop,
  scheduler, LangSmith tracing) and the decisions already made there. This
  SQLite→Snowflake migration was explicitly step 1 of that larger plan (per
  MERGE_HANDOFF.md's "Decisions already made #1": switch app storage from
  Postgres/SQLite to Snowflake). That larger merge has NOT been started beyond this
  storage-layer change.
