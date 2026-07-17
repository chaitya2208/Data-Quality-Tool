"""
One-shot library cleanup:

1. Delete DEAD sql_template definitions (template_shape=None AND no draft_sql
   equivalent — these can never actually execute, they're placeholder
   business-concept names left over from earlier seeding).

2. Delete the duplicate historical_deviation python_handler (two rows for the
   same handler key).

3. Delete low-signal metadata python_handlers we're pruning:
   missing_table_comment, missing_column_comment, missing_table_owner,
   too_many_columns, inconsistent_column_naming, generic_column_name,
   missing_created_at, missing_updated_at.

4. Delete every RULE_INSTANCES row with DATABASE_NAME='*' — the "global
   instance every table sees" model is being replaced by "per-table instance
   Claude proposes when relevant." The 5 kept python_handlers
   (nullable_id_column, no_primary_key_hint, pii_column_no_masking,
   boolean_stored_as_varchar, date_stored_as_varchar) will be proposed by
   Claude per-table when they apply, not injected globally.

Safe to re-run. Prints counts before/after.
"""
import logging
import sys

from app.services.snowflake_session import session as sf_session

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# Definitions we want gone. First 8: dead sql_templates. Next 1: duplicate
# handler. Next 8: low-signal metadata handlers.
_DEFS_TO_DELETE_BY_NAME = [
    # Dead sql_template rows (shape=None, no draft_sql, never executable):
    "OHLC High/Low Envelope Consistency",
    "OHLC Range Consistency (HIGH >= LOW)",
    "Bid/Ask Price Ordering Consistency (ASK >= BID)",
    "Revenue Below Cost (Negative Margin)",
    "Discount Percentage Out of Valid Range",
    "Negative Stock Quantity",
    "Non-Positive Value",
    "Batch End Timestamp Before Start Timestamp",
    "Ingestion Batch Time Ordering (END_TS >= START_TS)",
    # Low-signal metadata handlers (kept: nullable_id_column, no_primary_key_hint,
    # pii_column_no_masking, boolean_stored_as_varchar, date_stored_as_varchar):
    "Missing Table Comment",
    "Missing Column Comment",
    "Missing Table Owner",
    "Table Has Too Many Columns",
    "Inconsistent Column Naming Style",
    "Generic / Uninformative Column Name",
    "Missing Row Creation Timestamp",
    "Missing Row Updated Timestamp",
    "Foreign Key Column Without FK Constraint",
    # AI-synthesized def from earlier test scans — kill it so a fresh scan
    # doesn't reuse a bad synthesized ancestor and can either propose the
    # canonical range template or re-synthesize cleanly.
    "AI: range on PRICE",
]

# Handler-key duplicates: keep one row per key. This kills the duplicated
# historical_deviation row while keeping its canonical twin.
_HANDLER_KEYS_TO_DEDUPE = ["historical_deviation_95pct"]


def _count(sql: str, label: str) -> int:
    rows = sf_session.query(sql)
    n = rows[0].get("N") or 0
    print(f"  {label}: {n}")
    return n


def snapshot(when: str) -> None:
    print(f"\n=== {when} ===")
    _count("SELECT COUNT(*) AS N FROM RULE_DEFINITIONS", "RULE_DEFINITIONS")
    _count("SELECT COUNT(*) AS N FROM RULE_INSTANCES", "RULE_INSTANCES")
    _count(
        "SELECT COUNT(*) AS N FROM RULE_INSTANCES WHERE DATABASE_NAME='*'",
        "  global instances (DATABASE_NAME='*')",
    )


def delete_defs_by_name(names: list) -> None:
    if not names:
        return
    # Two-step: gather ids, delete their instances first (FK safety), then
    # delete the defs. Snowflake has no FK enforcement but keeping the order
    # keeps the intent readable.
    rows = sf_session.query(
        f"SELECT ID, NAME FROM RULE_DEFINITIONS WHERE NAME IN ({','.join(['%(n' + str(i) + ')s' for i in range(len(names))])})",
        {f"n{i}": n for i, n in enumerate(names)},
    )
    if not rows:
        logger.info("  no matching definitions to delete")
        return
    ids = [r["ID"] for r in rows]
    logger.info(f"  found {len(ids)} definition(s) to delete: {[r['NAME'] for r in rows]}")

    # Delete related instances (any status)
    id_params = {f"i{i}": _id for i, _id in enumerate(ids)}
    id_placeholders = ", ".join([f"%(i{i})s" for i in range(len(ids))])
    sf_session.execute(
        f"DELETE FROM RULE_INSTANCES WHERE DEFINITION_ID IN ({id_placeholders})",
        id_params,
    )
    logger.info(f"    deleted RULE_INSTANCES for these definitions")

    sf_session.execute(
        f"DELETE FROM RULE_DEFINITIONS WHERE ID IN ({id_placeholders})",
        id_params,
    )
    logger.info(f"    deleted RULE_DEFINITIONS")


def dedupe_handlers(handler_keys: list) -> None:
    """Where multiple rows share HANDLER_KEY, keep the oldest one and delete
    the rest (plus their instances)."""
    for key in handler_keys:
        rows = sf_session.query(
            "SELECT ID, NAME, CREATED_AT FROM RULE_DEFINITIONS "
            "WHERE HANDLER_KEY = %(k)s ORDER BY CREATED_AT ASC",
            {"k": key},
        )
        if len(rows) <= 1:
            continue
        keep = rows[0]
        drop = rows[1:]
        logger.info(f"  handler_key='{key}': keeping {keep['ID']}, deleting {len(drop)} dup(s)")
        drop_ids = [r["ID"] for r in drop]
        id_params = {f"d{i}": _id for i, _id in enumerate(drop_ids)}
        id_placeholders = ", ".join([f"%(d{i})s" for i in range(len(drop_ids))])
        sf_session.execute(
            f"DELETE FROM RULE_INSTANCES WHERE DEFINITION_ID IN ({id_placeholders})",
            id_params,
        )
        sf_session.execute(
            f"DELETE FROM RULE_DEFINITIONS WHERE ID IN ({id_placeholders})",
            id_params,
        )


def kill_globals() -> None:
    """Every DATABASE_NAME='*' instance dies. The 5 kept metadata handlers
    are now Claude-proposed per-table, not universally applied."""
    rows = sf_session.query(
        "SELECT COUNT(*) AS N FROM RULE_INSTANCES WHERE DATABASE_NAME='*'"
    )
    n = rows[0].get("N") or 0
    if n == 0:
        logger.info("  no global instances to delete")
        return
    logger.info(f"  deleting {n} global instance(s)")
    sf_session.execute("DELETE FROM RULE_INSTANCES WHERE DATABASE_NAME='*'")


def show_remaining() -> None:
    print("\n=== Remaining definitions ===")
    rows = sf_session.query(
        "SELECT NAME, CHECK_KIND, TEMPLATE_SHAPE, HANDLER_KEY, DEFAULT_SEVERITY "
        "FROM RULE_DEFINITIONS ORDER BY CHECK_KIND, NAME"
    )
    for r in rows:
        shape = r.get("TEMPLATE_SHAPE") or ""
        handler = r.get("HANDLER_KEY") or ""
        marker = shape or f"handler:{handler}" or "(neither)"
        print(f"  {r['NAME']:<48} {r['CHECK_KIND']:<15} {marker}")
    print(f"\n  TOTAL: {len(rows)}")


def main() -> int:
    snapshot("BEFORE")

    print("\n=== 1. Deleting definitions by name ===")
    delete_defs_by_name(_DEFS_TO_DELETE_BY_NAME)

    print("\n=== 2. Deduplicating handler_keys ===")
    dedupe_handlers(_HANDLER_KEYS_TO_DEDUPE)

    print("\n=== 3. Killing global instances ===")
    kill_globals()

    snapshot("AFTER")
    show_remaining()
    return 0


if __name__ == "__main__":
    sys.exit(main())
