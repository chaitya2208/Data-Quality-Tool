"""One-shot script to truncate all app-owned data. Run to start fresh.

    python infra/truncate_app_data.py

Uses the existing app-DB connection (same credentials as the backend).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/backend/agent_service"))

from tools.snowflake_connection import run_app_query

TABLES_TO_TRUNCATE = [
    # RULES
    "PLAYGROUND_DB.RULES.USER_FEEDBACK",
    "PLAYGROUND_DB.RULES.RULE_EXECUTION_HISTORY",
    "PLAYGROUND_DB.RULES.REJECTED_INSTANCES",
    "PLAYGROUND_DB.RULES.RULE_INSTANCES",
    "PLAYGROUND_DB.RULES.RECOMMENDED_INSTANCES",
    # ALERTS
    "PLAYGROUND_DB.ALERTS.ALERT_VIOLATION_SAMPLES",
    "PLAYGROUND_DB.ALERTS.ALERTS",
    # LOGS
    "PLAYGROUND_DB.LOGS.AGENT_RUN_LOGS",
    # CORE
    "PLAYGROUND_DB.CORE.SCAN_RUNS",
    # PROFILING
    "PLAYGROUND_DB.PROFILING.COLUMN_PROFILES",
    "PLAYGROUND_DB.PROFILING.TABLE_PROFILES",
]

# RULE_DEFINITIONS and RULE_GROUPS are kept — RULE_DEFINITIONS is seeded
# system data that the backend re-seeds on startup anyway; clearing it here
# would just trigger an immediate re-seed on next boot with no net effect.


def main() -> None:
    print("Truncating all app-owned data...")
    print()

    for table in TABLES_TO_TRUNCATE:
        try:
            run_app_query(f"TRUNCATE TABLE IF EXISTS {table}")
            print(f"  TRUNCATED  {table}")
        except Exception as exc:
            print(f"  ERROR      {table}: {exc}")
            sys.exit(1)

    print()
    print("Done. Restart the backend to re-seed RULE_DEFINITIONS.")


if __name__ == "__main__":
    main()
