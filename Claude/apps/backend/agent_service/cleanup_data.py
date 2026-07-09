"""One-shot script to truncate all app data tables for a fresh start.

Run from repo root:
    ./.venv/Scripts/python.exe apps/backend/agent_service/cleanup_data.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tools.snowflake_connection import run_app_query

TABLES = [
    # alerts
    "PLAYGROUND_DB.ALERTS.ALERT_VIOLATION_SAMPLES",
    "PLAYGROUND_DB.ALERTS.ALERTS",
    # rules
    "PLAYGROUND_DB.RULES.RULE_EXECUTION_HISTORY",
    "PLAYGROUND_DB.RULES.USER_FEEDBACK",
    "PLAYGROUND_DB.RULES.REJECTED_INSTANCES",
    "PLAYGROUND_DB.RULES.RULE_INSTANCES",
    "PLAYGROUND_DB.RULES.RECOMMENDED_INSTANCES",
    "PLAYGROUND_DB.RULES.RULE_GROUPS",
    # profiling / table health
    "PLAYGROUND_DB.PROFILING.COLUMN_PROFILES",
    "PLAYGROUND_DB.PROFILING.METADATA_SNAPSHOTS",
    "PLAYGROUND_DB.PROFILING.TABLE_PROFILES",
    # logs + scan runs
    "PLAYGROUND_DB.LOGS.AGENT_RUN_LOGS",
    "PLAYGROUND_DB.CORE.SCAN_SCHEDULES",
    "PLAYGROUND_DB.CORE.SCAN_RUNS",
]

# RULES.RULE_DEFINITIONS is deliberately NOT truncated here -- it holds the
# reusable SYSTEM check library (seeded once by infra/snowflake/
# 14_seed_rule_definitions.sql), not scan-produced data, and the app's
# startup lifespan (see tools.storage_tools.ensure_system_rule_definitions_
# seeded(), called from apps/backend/agent_service/src/main.py) already
# self-heals it to exactly the 11 SYSTEM rows if it's ever found empty. If a
# fresh start needs to clear USER/CLAUDE-sourced definitions too (e.g. ones
# created via the definitions-library UI), truncate that table separately
# and restart the backend once to trigger the reseed.

for table in TABLES:
    print(f"Truncating {table} ...", end=" ", flush=True)
    run_app_query(f"TRUNCATE TABLE {table}")
    print("done")

print("\nAll tables cleared.")
