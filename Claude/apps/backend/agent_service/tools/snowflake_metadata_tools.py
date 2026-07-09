"""Metadata Discovery tools -- read-only inspection of the source Snowflake
account: databases, schemas, tables, and column definitions.

These are the tools the Metadata Discovery Agent (architecture.md) calls
during a scan. All queries are SHOW/DESCRIBE -- no data is read, only
structure. Every query goes through run_query(), which uses the SOURCE
(read-only) connection.
"""

from __future__ import annotations

import re
from typing import Any

from tools.snowflake_connection import run_query

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def _safe_identifier(name: str) -> str:
    """Guard against malformed/malicious identifiers before they're
    interpolated into SQL (Snowflake object names can't be bind parameters).
    """
    if not _IDENTIFIER_RE.match(name):
        raise ValueError(f"Not a valid Snowflake identifier: {name!r}")
    return name


def list_databases() -> list[dict[str, Any]]:
    """List databases visible to the current role.

    SHOW DATABASES returns ~14 raw columns (created_on, name, is_default,
    is_current, origin, owner, comment, options, retention_time, kind,
    budget, owner_role_type, object_visibility, dropped_on, ...). We only
    keep the 4 fields callers actually need; drop the rest instead of
    leaking Snowflake's raw shape to every caller.
    """
    rows = run_query("SHOW DATABASES")
    return [
        {
            "name": r["name"],
            "created_on": r["created_on"],
            "owner": r["owner"],
            "comment": r["comment"],
        }
        for r in rows
    ]


def list_schemas(database_name: str) -> list[dict[str, Any]]:
    """List schemas within a database.

    SHOW SCHEMAS returns ~13 raw columns (created_on, name, is_default,
    is_current, database_name, owner, comment, options, retention_time,
    ...). Trimmed to the 5 fields we use.
    """
    db = _safe_identifier(database_name)
    rows = run_query(f"SHOW SCHEMAS IN DATABASE {db}")
    return [
        {
            "database_name": r["database_name"],
            "name": r["name"],
            "created_on": r["created_on"],
            "owner": r["owner"],
            "comment": r["comment"],
        }
        for r in rows
    ]


def list_tables(database_name: str, schema_name: str) -> list[dict[str, Any]]:
    """List tables (and views) within a schema.

    SHOW TABLES returns ~27 raw columns -- most are storage/feature flags we
    don't need yet (cluster_by, automatic_clustering, is_iceberg, is_hybrid,
    is_dynamic, search_optimization_bytes, retention_time, ...). Verified
    directly against PLAYGROUND_DB.RAW: raw "rows"/"bytes" become our
    row_count/bytes; the rest of those ~27 columns are dropped here.
    """
    db = _safe_identifier(database_name)
    schema = _safe_identifier(schema_name)
    rows = run_query(f"SHOW TABLES IN SCHEMA {db}.{schema}")
    return [
        {
            "database_name": r["database_name"],
            "schema_name": r["schema_name"],
            "name": r["name"],
            "kind": r["kind"],
            "row_count": r["rows"],
            "bytes": r["bytes"],
            "owner": r["owner"],
            "comment": r["comment"],
        }
        for r in rows
    ]


def get_table_row_count_estimate(
    database_name: str, schema_name: str, table_name: str
) -> int | None:
    """Cheap row-count estimate for one table, from SHOW TABLES metadata --
    no data scan, unlike SELECT COUNT(*). Used to decide whether profiling
    should sample (tools/snowflake_profiling_tools.py) without first paying
    for a full-table COUNT(*) just to make that decision.

    Reuses list_tables() (already trims SHOW TABLES's ~27 raw columns) and
    filters locally rather than a second targeted query -- SHOW TABLES LIKE
    would need its own wildcard-escaping for underscores in table names
    (common in this account's naming, e.g. REPLAY_BRONZE_INGESTION_...),
    and schemas here have at most a few dozen tables, so listing the whole
    schema is cheap and simpler than getting LIKE-escaping right.

    Returns None if the table isn't found (caller falls back to a real
    COUNT(*) in that case) -- Snowflake's own row estimate can also lag
    reality slightly after recent writes, callers should treat this as an
    estimate for a sampling decision, not an exact count.
    """
    tables = list_tables(database_name, schema_name)
    for t in tables:
        if t["name"] == table_name:
            return t["row_count"]
    return None


def describe_table(
    database_name: str, schema_name: str, table_name: str
) -> list[dict[str, Any]]:
    """Describe a table's columns: name, type, nullability, default, comment.

    DESCRIBE TABLE returns one row per column with raw keys "name", "type",
    "kind", "null?", "default", "primary key", "unique key", "check",
    "expression", "comment", "policy name", "privacy domain". We keep the
    columns relevant to rule generation and drop "kind"/"check"/"expression"/
    "policy name"/"privacy domain" (not needed yet); "null?"/"primary key"/
    "unique key" come back as raw 'Y'/'N' strings, converted to booleans here.

    This is the one query that both describe_table() and get_table_columns()
    are built on -- DESCRIBE TABLE already returns full column metadata, so
    get_table_columns() re-shapes this result instead of querying twice.
    """
    db = _safe_identifier(database_name)
    schema = _safe_identifier(schema_name)
    table = _safe_identifier(table_name)
    rows = run_query(f"DESCRIBE TABLE {db}.{schema}.{table}")
    return [
        {
            "column_name": r["name"],
            "data_type": r["type"],
            "is_nullable": r["null?"] == "Y",
            "default": r["default"],
            "primary_key": r["primary key"] == "Y",
            "unique_key": r["unique key"] == "Y",
            "comment": r["comment"],
        }
        for r in rows
    ]


def get_table_columns(
    database_name: str, schema_name: str, table_name: str
) -> list[dict[str, str]]:
    """Simplified column list: just name + data type, for quick lookups."""
    columns = describe_table(database_name, schema_name, table_name)
    return [
        {"column_name": c["column_name"], "data_type": c["data_type"]}
        for c in columns
    ]
