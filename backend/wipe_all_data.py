"""
Full DB wipe — truncates EVERY app table in DQ_APP, including RULE_DEFINITIONS,
CONNECTIONS, and APP_SETTINGS. Nothing survives.

This is the "start completely fresh" nuclear option — one level past
reset_for_test.py --all (which deliberately keeps RULE_DEFINITIONS,
CONNECTIONS, APP_SETTINGS, ASSETS).

After running this, the app has NO connection configured and NO rule
definitions. Re-run `python setup_db.py` to recreate the schema/tables
(idempotent CREATE IF NOT EXISTS — a no-op here since tables still exist,
just empty) and re-seed default rules + the default Snowflake connection
from .env.

Usage (from backend/):
    python wipe_all_data.py --yes-really-wipe-everything
"""
import argparse
import logging
import sys

from app.services.snowflake_session import session as sf_session

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Order matters for readability only — Snowflake doesn't enforce FK
# constraints at DML time in this schema, but truncating in roughly
# child-to-parent order keeps the intent obvious.
_ALL_TABLES = [
    "RULE_EXECUTIONS",
    "FINDINGS",
    "RULE_INSTANCES",
    "RULE_CRITIQUE_DROPS",
    "RULE_INTELLIGENCE_LOGS",
    "RULE_REVIEW_LESSONS",
    "RULE_FEEDBACK_MEMOS",
    "RECOMMENDATION_CACHE",
    "AGENT_TASKS",
    "AGENT_RUNS",
    "SCANS",
    "SCHEDULES",
    "WORKFLOW_TEMPLATES",
    "RULE_DEFINITIONS",
    "ASSETS",
    "CONNECTIONS",
    "APP_SETTINGS",
]


def _table_exists(name: str) -> bool:
    try:
        sf_session.query(f"SELECT 1 FROM {name} LIMIT 0")
        return True
    except Exception:
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument(
        "--yes-really-wipe-everything",
        action="store_true",
        help="Required confirmation flag — without it, nothing is touched.",
    )
    args = ap.parse_args()

    if not args.yes_really_wipe_everything:
        logger.error(
            "Refusing to run without --yes-really-wipe-everything. "
            "This truncates EVERY app table including RULE_DEFINITIONS and "
            "CONNECTIONS. Re-run with that flag to proceed."
        )
        return 1

    logger.warning("=== FULL WIPE — truncating ALL app tables ===")
    for t in _ALL_TABLES:
        if not _table_exists(t):
            logger.info(f"  skip {t} (does not exist)")
            continue
        sf_session.execute(f"TRUNCATE TABLE {t}")
        logger.info(f"  truncated {t}")

    logger.info(
        "Done. Everything is empty — including RULE_DEFINITIONS and "
        "CONNECTIONS. Run `python setup_db.py` next to recreate the schema "
        "and re-seed default rules + the default Snowflake connection."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
