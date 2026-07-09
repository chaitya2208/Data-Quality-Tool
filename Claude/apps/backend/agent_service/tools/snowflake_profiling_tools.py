"""Data Profiling tools -- read-only statistics over source table data:
row/column counts, nulls, distincts, min/max, top values.

These are the tools the Data Profiling Agent (architecture.md) calls during a
scan, after Metadata Discovery. Every query goes through run_query() (the
read-only SOURCE connection) -- no writes to source data.

Deviation from the original spec: functions take (database_name, schema_name,
table_name) instead of a single table_fqn string, matching the convention
already used by snowflake_metadata_tools.py and storage_tools.py. A combined
"TABLE_FQN" string was a convention from the prior session's now-superseded
DQ_TABLE_CATALOG table, not something this project adopted.

Sample-first profiling now exists (architecture.md §10: "Sample-first
profiling by default; full scan only for small or explicitly-important
tables"). A table's row count is checked cheaply first (SHOW TABLES metadata,
via get_table_row_count_estimate() -- no scan) against
_SAMPLE_ROW_THRESHOLD; at or above it, every per-column query runs against a
fixed-size `SAMPLE (n ROWS)` instead of the full table, and count-shaped
stats (null_count, distinct_count) are scaled back up to full-table
estimates. Verified directly against this account's real 31.5M-row table
(PLAYGROUND_DB.BRONZE.REPLAY_BRONZE_INGESTION_RECORDS_TBL) that
`SELECT ... FROM {fqn} SAMPLE (n ROWS)` returns exactly n rows fast, and
composes cleanly with this module's existing null/distinct/top-values query
shapes (SAMPLE goes right after the FROM clause's table reference, before any
WHERE/GROUP BY).

PII/sensitivity classification (IS_PII, PII_TYPE, SENSITIVITY_LEVEL,
LLM_SHARING_POLICY on COLUMN_PROFILES) is agents/pii_agent.py's job -- this
module does not classify columns, only profiles them. store_profile_result()
persists whatever classification is already present on each column dict
(defaulting to unclassified if the caller didn't run the PII agent first).
"""

from __future__ import annotations

from typing import Any

from tools.snowflake_connection import run_query
from tools.snowflake_metadata_tools import (
    _safe_identifier,
    get_table_columns,
    get_table_row_count_estimate,
)
from tools.storage_tools import store_column_profile, store_table_profile

# Snowflake data types for which MIN/MAX is meaningful to compute. Text types
# are included (lexicographic min/max), VARIANT/ARRAY/OBJECT/BOOLEAN are not.
_MIN_MAX_TYPE_PREFIXES = (
    "NUMBER",
    "DECIMAL",
    "INT",
    "FLOAT",
    "DOUBLE",
    "VARCHAR",
    "CHAR",
    "STRING",
    "TEXT",
    "DATE",
    "TIME",
    "TIMESTAMP",
)

# Tables at/above this many rows profile from a fixed-size SAMPLE instead of
# a full scan -- see module docstring. 100k chosen as a round threshold well
# below this account's real large tables (31.5M rows) and comfortably above
# every table this project has tested against so far (largest non-sampled
# table seen: low thousands of rows), so existing small-table behavior is
# unaffected.
_SAMPLE_ROW_THRESHOLD = 100_000
_SAMPLE_SIZE = 50_000


def _fqn(
    database_name: str, schema_name: str, table_name: str, sample_size: int | None = None
) -> str:
    """Fully-qualified table reference for a FROM clause. With sample_size
    set, appends Snowflake's `SAMPLE (n ROWS)` clause -- verified directly
    against this account's real 31.5M-row table that this returns exactly n
    rows (not a full scan) and composes with a trailing WHERE/GROUP BY the
    same as an unsampled table reference would.
    """
    fqn = (
        f"{_safe_identifier(database_name)}."
        f"{_safe_identifier(schema_name)}."
        f"{_safe_identifier(table_name)}"
    )
    if sample_size is not None:
        fqn = f"{fqn} SAMPLE ({int(sample_size)} ROWS)"
    return fqn


def profile_table_basic(
    database_name: str, schema_name: str, table_name: str
) -> dict[str, int]:
    """Row count + column count for a table.

    Column count comes from get_table_columns() (DESCRIBE TABLE), not a
    second distinct query, to reuse existing metadata logic rather than
    duplicate it. Row count always comes from a real full-table COUNT(*) --
    never sampled -- since this is the very number profile_table() uses to
    decide whether everything else should be sampled; a sampled row count
    would be circular.
    """
    fqn = _fqn(database_name, schema_name, table_name)
    rows = run_query(f"SELECT COUNT(*) AS row_count FROM {fqn}")
    row_count = rows[0]["ROW_COUNT"]
    column_count = len(get_table_columns(database_name, schema_name, table_name))
    return {"row_count": row_count, "column_count": column_count}


def profile_column_nulls(
    database_name: str,
    schema_name: str,
    table_name: str,
    column_name: str,
    sample_size: int | None = None,
) -> dict[str, Any]:
    """Null count and null percentage for one column.

    One query returns both COUNT(*) and COUNT(column) together (COUNT(col)
    skips NULLs by SQL semantics) so total/non-null/null counts are always
    consistent with each other, rather than reusing a row count computed by
    a separate, possibly-stale call.

    With sample_size set, `total`/`non_null` are sample-scoped counts (e.g.
    "9800 of 10000 sampled rows non-null") -- profile_table() scales
    null_count back up to a full-table estimate using the real row_count
    from profile_table_basic(); null_percentage is unaffected by sampling
    since it's already a ratio.
    """
    fqn = _fqn(database_name, schema_name, table_name, sample_size)
    col = _safe_identifier(column_name)
    rows = run_query(
        f"SELECT COUNT(*) AS total, COUNT({col}) AS non_null FROM {fqn}"
    )
    total = rows[0]["TOTAL"]
    non_null = rows[0]["NON_NULL"]
    null_count = total - non_null
    null_percentage = (null_count / total * 100) if total > 0 else 0.0
    return {
        "null_count": null_count,
        "null_percentage": round(null_percentage, 4),
        "sample_rows_seen": total,
    }


def profile_column_distincts(
    database_name: str,
    schema_name: str,
    table_name: str,
    column_name: str,
    sample_size: int | None = None,
) -> dict[str, int]:
    """Distinct value count for one column. With sample_size set, this is a
    sample-scoped count -- profile_table() scales it to a full-table
    estimate. Scaling distinct counts from a sample is an approximation
    (distinct counts don't scale linearly the way total/null counts do --
    a sample can't see every distinct value a full scan would), flagged as
    such in the returned column dict's is_sampled flag rather than
    presented as exact.
    """
    fqn = _fqn(database_name, schema_name, table_name, sample_size)
    col = _safe_identifier(column_name)
    rows = run_query(f"SELECT COUNT(DISTINCT {col}) AS distinct_count FROM {fqn}")
    return {"distinct_count": rows[0]["DISTINCT_COUNT"]}


def profile_numeric_min_max(
    database_name: str,
    schema_name: str,
    table_name: str,
    column_name: str,
    sample_size: int | None = None,
) -> dict[str, Any]:
    """Min/max for one column. Works on any orderable type (numeric, text,
    date/time) -- despite the name (kept per spec), this isn't numeric-only;
    it's SQL MIN/MAX, which Snowflake supports on text (lexicographic) and
    date/time types too. Caller (profile_and_store_table) decides which
    columns are worth calling this on.

    With sample_size set, min/max are the sample's min/max, not necessarily
    the true full-table extremes -- there's no way to "scale" an extremum,
    so this is reported as-observed-in-the-sample (flagged via the column
    dict's is_sampled flag), not corrected.
    """
    fqn = _fqn(database_name, schema_name, table_name, sample_size)
    col = _safe_identifier(column_name)
    rows = run_query(f"SELECT MIN({col}) AS min_value, MAX({col}) AS max_value FROM {fqn}")
    return {"min_value": rows[0]["MIN_VALUE"], "max_value": rows[0]["MAX_VALUE"]}


def profile_top_values(
    database_name: str,
    schema_name: str,
    table_name: str,
    column_name: str,
    limit: int = 5,
    sample_size: int | None = None,
) -> list[dict[str, Any]]:
    """Top N most frequent values in one column (default 5), excluding NULLs.
    With sample_size set, frequencies are counted within the sample only.
    """
    fqn = _fqn(database_name, schema_name, table_name, sample_size)
    col = _safe_identifier(column_name)
    rows = run_query(
        f"""
        SELECT {col} AS value, COUNT(*) AS occurrences
        FROM {fqn}
        WHERE {col} IS NOT NULL
        GROUP BY {col}
        ORDER BY occurrences DESC
        LIMIT {int(limit)}
        """
    )
    return [{"value": r["VALUE"], "count": r["OCCURRENCES"]} for r in rows]


def profile_table(
    database_name: str, schema_name: str, table_name: str
) -> dict[str, Any]:
    """Profile every column of one table. Pure computation, no storage.

    Extracted out of profile_and_store_table() (below), which used to run
    this same per-column loop and call storage_tools in the same pass --
    split apart so the Profiling Agent (agents/profiling_agent.py) can get
    profiling results without a storage dependency, and so this loop (which
    means a full-table scan per column) isn't duplicated if a caller needs
    both the pure numbers and, separately, persistence. profile_and_store_table()
    below is now just this function's result fed into storage_tools.

    Sample-first: the sampling decision uses get_table_row_count_estimate()
    (cheap SHOW TABLES metadata, no scan) rather than profile_table_basic()'s
    real COUNT(*) -- for a table already known to be huge, paying for one
    more full-table COUNT(*) just to decide to sample would defeat the
    purpose. If the cheap estimate is at/above _SAMPLE_ROW_THRESHOLD, every
    per-column query runs against a fixed SAMPLE (_SAMPLE_SIZE ROWS) and
    count-shaped stats are scaled back up to full-table estimates using the
    sample's *actual* returned row count (not the requested sample size --
    SAMPLE (n ROWS) returns min(n, table_size) rows, and a table just over
    the threshold could return fewer than requested). Below the threshold
    (or if the cheap estimate is unavailable), behavior is unchanged from
    before: a real profile_table_basic() COUNT(*) and unsampled per-column
    queries.
    """
    row_count_estimate = get_table_row_count_estimate(database_name, schema_name, table_name)
    use_sample = row_count_estimate is not None and row_count_estimate >= _SAMPLE_ROW_THRESHOLD
    columns = get_table_columns(database_name, schema_name, table_name)

    if use_sample:
        basic = {"row_count": row_count_estimate, "column_count": len(columns)}
        sample_size: int | None = _SAMPLE_SIZE
    else:
        basic = profile_table_basic(database_name, schema_name, table_name)
        sample_size = None

    row_count = basic["row_count"] or 0
    column_profiles = []

    for col in columns:
        column_name = col["column_name"]
        data_type = col["data_type"]

        nulls = profile_column_nulls(
            database_name, schema_name, table_name, column_name, sample_size=sample_size
        )
        distincts = profile_column_distincts(
            database_name, schema_name, table_name, column_name, sample_size=sample_size
        )
        top_values = profile_top_values(
            database_name, schema_name, table_name, column_name, sample_size=sample_size
        )

        null_count = nulls["null_count"]
        distinct_count = distincts["distinct_count"]
        if use_sample:
            sample_rows_seen = nulls["sample_rows_seen"] or 0
            if sample_rows_seen > 0:
                scale = row_count / sample_rows_seen
                null_count = round(null_count * scale)
                distinct_count = round(distinct_count * scale)

        min_value = max_value = None
        if data_type.split("(")[0].upper() in _MIN_MAX_TYPE_PREFIXES:
            min_max = profile_numeric_min_max(
                database_name, schema_name, table_name, column_name, sample_size=sample_size
            )
            min_value = min_max["min_value"]
            max_value = min_max["max_value"]

        column_profiles.append(
            {
                "column_name": column_name,
                "data_type": data_type,
                "null_count": null_count,
                "null_percentage": nulls["null_percentage"],
                "distinct_count": distinct_count,
                "min_value": min_value,
                "max_value": max_value,
                "top_values": top_values,
                "is_sampled": use_sample,
            }
        )

    basic["is_sampled"] = use_sample
    basic["sample_size"] = sample_size if use_sample else None

    return {
        "table": basic,
        "columns": column_profiles,
    }


def store_profile_result(
    scan_id: str,
    database_name: str,
    schema_name: str,
    table_name: str,
    result: dict[str, Any],
) -> None:
    """Persist an already-computed profile_table()-shaped result via
    storage_tools.store_table_profile()/store_column_profile().

    Split out of profile_and_store_table() (below) so a caller that already
    has a profile_table() result in hand (e.g. main.py's recommend-rules
    route, which gets one from profiling_agent.py as part of the LangGraph
    run) can persist it without re-running the full-table scan a second
    time just to get something to store.

    PII/sensitivity fields (is_pii/pii_type/sensitivity_level/
    llm_sharing_policy) are read from each column dict via .get() rather
    than required keys -- a caller that ran agents/pii_agent.py first (the
    normal path, via graphs/dq_workflow_graph.py's pii_agent_node) has them
    populated; a caller that only ran profile_table() without the PII agent
    (e.g. the standalone /profile route) gets None/False defaults, same as
    before this feature existed -- store_column_profile() already defaults
    every one of these kwargs, so an absent key is not a new failure mode.
    """
    basic = result["table"]

    store_table_profile(
        scan_id,
        database_name,
        schema_name,
        table_name,
        basic["row_count"],
        basic["column_count"],
        is_sampled=basic.get("is_sampled", False),
        sample_size=basic.get("sample_size"),
    )

    for col in result["columns"]:
        store_column_profile(
            scan_id,
            database_name,
            schema_name,
            table_name,
            col["column_name"],
            col["data_type"],
            null_count=col["null_count"],
            null_percentage=col["null_percentage"],
            distinct_count=col["distinct_count"],
            min_value=str(col["min_value"]) if col["min_value"] is not None else None,
            max_value=str(col["max_value"]) if col["max_value"] is not None else None,
            top_values=col["top_values"],
            is_pii=col.get("is_pii", False),
            pii_type=col.get("pii_type"),
            sensitivity_level=col.get("sensitivity_level"),
            llm_sharing_policy=col.get("llm_sharing_policy"),
        )


def profile_and_store_table(
    scan_id: str, database_name: str, schema_name: str, table_name: str
) -> dict[str, Any]:
    """Profile every column of one table (via profile_table()) and store the
    results (via store_profile_result()). Sample-first for tables at/above
    _SAMPLE_ROW_THRESHOLD rows (see profile_table()'s docstring); does not
    run the PII agent -- callers that need PII classification persisted
    should run agents/pii_agent.py on the result's columns before calling
    store_profile_result() themselves (this is what
    graphs/dq_workflow_graph.py's node chain does; this convenience function
    is used by the standalone /profile route, which has no PII step).
    """
    result = profile_table(database_name, schema_name, table_name)
    store_profile_result(scan_id, database_name, schema_name, table_name, result)
    return result
