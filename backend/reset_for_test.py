"""
Selective reset for testing the rebalanced RuleIntelligence pipeline.

Clears per-run artifacts + instances for ONE target table so a fresh scan
shows the new behavior clearly. Keeps RULE_DEFINITIONS intact so the
novel-vs-reuse routing has something to route against.

Usage (from backend/):
    python reset_for_test.py <DATABASE>.<SCHEMA>.<TABLE>
        # e.g. python reset_for_test.py MY_DB.PUBLIC.CUSTOMERS

    python reset_for_test.py --all
        # nuclear option: truncate every runtime table, keep RULE_DEFINITIONS

What it CLEARS (always):
- RULE_INTELLIGENCE_LOGS  — per-run reasoning artifacts
- RULE_CRITIQUE_DROPS     — per-run dropped-proposal log
- RULE_REVIEW_LESSONS     — past-context lessons that would bias new run
- RULE_FEEDBACK_MEMOS     — synthesised memos (same reason)

What it CLEARS when a target table is given (scoped to that table):
- RULE_INSTANCES for the target table — otherwise fingerprint dedup will
  suppress every re-proposal and mask the rebalance behavior
- FINDINGS for scans against that table (via ASSET_ID join)
- RULE_EXECUTIONS for those findings' scans

What it KEEPS:
- RULE_DEFINITIONS (the library — the whole point is to see reuse-vs-novel
  routing pick between real options)
- CONNECTIONS, APP_SETTINGS, ASSETS (schema catalog)
- SCANS, AGENT_RUNS, AGENT_TASKS (run history; harmless to keep)

Safe to run multiple times.
"""
import argparse
import logging
import sys

from app.services.snowflake_session import session as sf_session

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# Tables cleared unconditionally — per-run artifacts that would bias
# the next scan's "past context" injection.
_ALWAYS_CLEAR = [
    "RULE_INTELLIGENCE_LOGS",
    "RULE_CRITIQUE_DROPS",
    "RULE_REVIEW_LESSONS",
    "RULE_FEEDBACK_MEMOS",
]

# Tables truncated only with --all (kept when a target table is given, so
# scans against other tables aren't disturbed).
_TRUNCATE_ALL_EXTRA = [
    "FINDINGS",
    "RULE_EXECUTIONS",
    "RULE_INSTANCES",
    "SCANS",
    "AGENT_TASKS",
    "AGENT_RUNS",
    "RECOMMENDATION_CACHE",
]


def _table_exists(name: str) -> bool:
    try:
        sf_session.query(f"SELECT 1 FROM {name} LIMIT 0")
        return True
    except Exception:
        return False


def _truncate(name: str) -> None:
    if not _table_exists(name):
        logger.info(f"  skip {name} (does not exist)")
        return
    sf_session.execute(f"TRUNCATE TABLE {name}")
    logger.info(f"  truncated {name}")


def clear_always() -> None:
    logger.info("Clearing per-run artifact tables:")
    for t in _ALWAYS_CLEAR:
        _truncate(t)


def clear_for_table(fqn: str) -> None:
    parts = fqn.upper().split(".")
    if len(parts) != 3:
        raise ValueError(f"Expected DATABASE.SCHEMA.TABLE, got: {fqn!r}")
    database, schema, table = parts
    logger.info(f"Scoped clear for target table {database}.{schema}.{table}:")

    # Look up the asset id first — findings/scans join through it.
    asset_rows = sf_session.query(
        """
        SELECT ID FROM ASSETS
        WHERE UPPER(DATABASE_NAME) = %(db)s
          AND UPPER(SCHEMA_NAME)   = %(sc)s
          AND UPPER(TABLE_NAME)    = %(tb)s
        """,
        {"db": database, "sc": schema, "tb": table},
    )
    asset_ids = [r.get("ID") for r in asset_rows]
    if not asset_ids:
        logger.warning(
            f"  no ASSET row for {fqn} — nothing to scope-clear. "
            "Only per-run artifact tables were truncated."
        )
        return

    logger.info(f"  target asset_id(s): {asset_ids}")

    # Rule instances scoped to this table (either by asset_id or by name match)
    sf_session.execute(
        """
        DELETE FROM RULE_INSTANCES
        WHERE UPPER(DATABASE_NAME) = %(db)s
          AND UPPER(SCHEMA_NAME)   = %(sc)s
          AND UPPER(TABLE_NAME)    = %(tb)s
        """,
        {"db": database, "sc": schema, "tb": table},
    )
    logger.info(f"  deleted RULE_INSTANCES for {fqn}")

    # Findings + executions for scans against this asset
    sf_session.execute(
        """
        DELETE FROM RULE_EXECUTIONS
        WHERE SCAN_ID IN (SELECT ID FROM SCANS WHERE ASSET_ID IN (
            SELECT VALUE FROM TABLE(FLATTEN(INPUT => PARSE_JSON(%(ids)s)))
        ))
        """,
        {"ids": str(asset_ids).replace("'", '"')},
    )
    logger.info(f"  deleted RULE_EXECUTIONS for scans of {fqn}")

    sf_session.execute(
        """
        DELETE FROM FINDINGS
        WHERE ASSET_ID IN (
            SELECT VALUE FROM TABLE(FLATTEN(INPUT => PARSE_JSON(%(ids)s)))
        )
        """,
        {"ids": str(asset_ids).replace("'", '"')},
    )
    logger.info(f"  deleted FINDINGS for {fqn}")


def clear_all_runtime() -> None:
    logger.warning("=== NUCLEAR OPTION — truncating ALL runtime tables ===")
    logger.warning("Keeping: RULE_DEFINITIONS, CONNECTIONS, APP_SETTINGS, ASSETS")
    for t in _ALWAYS_CLEAR + _TRUNCATE_ALL_EXTRA:
        _truncate(t)


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument(
        "target",
        nargs="?",
        help="DATABASE.SCHEMA.TABLE to scope-clear (omit if using --all)",
    )
    ap.add_argument(
        "--all",
        action="store_true",
        help="Nuclear: truncate every runtime table (keeps RULE_DEFINITIONS, "
        "CONNECTIONS, APP_SETTINGS, ASSETS).",
    )
    args = ap.parse_args()

    if args.all:
        clear_all_runtime()
        return 0

    if not args.target:
        ap.print_help()
        return 1

    clear_always()
    clear_for_table(args.target)
    logger.info("Done. RULE_DEFINITIONS untouched.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
