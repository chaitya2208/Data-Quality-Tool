-- App storage schema. All app-owned tables live here, separate from the
-- source schemas being scanned. Idempotent — safe to re-run.
CREATE SCHEMA IF NOT EXISTS PLAYGROUND_DB.DQ_APP; -- substituted at runtime by setup_db.py
