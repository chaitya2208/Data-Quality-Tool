-- Idempotent migrations for pre-existing DQ_APP deployments.
-- CREATE TABLE IF NOT EXISTS in 02_tables.sql is a no-op when a table already
-- exists, so columns added after a table was first created must be ALTERed in
-- here. ADD COLUMN IF NOT EXISTS is idempotent — safe to re-run.

-- Multi-source support: which saved connection a run/scan targeted.
ALTER TABLE AGENT_RUNS ADD COLUMN IF NOT EXISTS CONNECTION_ID VARCHAR(36);
ALTER TABLE SCANS ADD COLUMN IF NOT EXISTS CONNECTION_ID VARCHAR(36);
