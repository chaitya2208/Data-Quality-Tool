"""
SQL templates for data-level checks (check_kind="sql_template" on
RULE_DEFINITIONS). Every function returns a single SELECT that yields
exactly one row shaped (FAILED_COUNT, TOTAL_COUNT) — the shape RuleEngine's
sql_template dispatch expects: FAILED_COUNT > 0 means the instance fires a
finding, and both counts are stored as evidence.

These are fixed, hand-written strings with only identifiers/values
substituted via _safe_identifier()/_quoted_list() — no free-form SQL path
here, so every template is SELECT-only and safe by construction. This is
the deterministic side of the trust chain (see rule_intelligence_agent.py's
docstring): Claude never has to write SQL for anything that fits one of
these shapes, it just picks the shape + parameters.

For checks that genuinely don't fit any shape here, Claude may supply
draft_sql — that path goes through sql_validation.validate_sql() before it
is ever allowed to become RULE_INSTANCES.rule_sql (see
rule_intelligence_agent.py._validate_draft_sql()).
"""
from __future__ import annotations

from typing import Any, Optional

_IDENT_SAFE = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_"
)


def _safe_identifier(name: str) -> str:
    """Reject anything that isn't a plain identifier — no quoting tricks,
    no injection surface. Snowflake identifiers here are always our own
    discovered database/schema/table/column names, never raw user input."""
    if not name or not all(c in _IDENT_SAFE for c in name):
        raise ValueError(f"Unsafe identifier: {name!r}")
    return name.upper()


def _fqn(database_name: str, schema_name: str, table_name: str) -> str:
    return f"{_safe_identifier(database_name)}.{_safe_identifier(schema_name)}.{_safe_identifier(table_name)}"


def _quoted_list(values: list[Any]) -> str:
    return ", ".join("'{}'".format(str(v).replace("'", "''")) for v in values)


def not_null_sql(database_name: str, schema_name: str, table_name: str, column_name: str) -> str:
    table = _fqn(database_name, schema_name, table_name)
    col = _safe_identifier(column_name)
    return (
        "SELECT\n"
        "    COUNT(*) AS FAILED_COUNT,\n"
        f"    (SELECT COUNT(*) FROM {table}) AS TOTAL_COUNT\n"
        f"FROM {table}\n"
        f"WHERE {col} IS NULL"
    )


def uniqueness_sql(database_name: str, schema_name: str, table_name: str, column_name: str) -> str:
    table = _fqn(database_name, schema_name, table_name)
    col = _safe_identifier(column_name)
    return (
        "SELECT\n"
        "    COUNT(*) AS FAILED_COUNT,\n"
        f"    (SELECT COUNT(*) FROM {table}) AS TOTAL_COUNT\n"
        "FROM (\n"
        f"    SELECT {col}, COUNT(*) AS CNT\n"
        f"    FROM {table}\n"
        f"    WHERE {col} IS NOT NULL\n"
        f"    GROUP BY {col}\n"
        "    HAVING COUNT(*) > 1\n"
        ")"
    )


def accepted_values_sql(
    database_name: str, schema_name: str, table_name: str, column_name: str,
    accepted_values: list[Any],
) -> str:
    if not accepted_values:
        raise ValueError("accepted_values must not be empty")
    table = _fqn(database_name, schema_name, table_name)
    col = _safe_identifier(column_name)
    values_sql = _quoted_list(accepted_values)
    return (
        "SELECT\n"
        "    COUNT(*) AS FAILED_COUNT,\n"
        f"    (SELECT COUNT(*) FROM {table} WHERE {col} IS NOT NULL) AS TOTAL_COUNT\n"
        f"FROM {table}\n"
        f"WHERE {col} IS NOT NULL AND {col} NOT IN ({values_sql})"
    )


def positive_value_sql(database_name: str, schema_name: str, table_name: str, column_name: str) -> str:
    table = _fqn(database_name, schema_name, table_name)
    col = _safe_identifier(column_name)
    return (
        "SELECT\n"
        "    COUNT(*) AS FAILED_COUNT,\n"
        f"    (SELECT COUNT(*) FROM {table} WHERE {col} IS NOT NULL) AS TOTAL_COUNT\n"
        f"FROM {table}\n"
        f"WHERE {col} IS NOT NULL AND {col} <= 0"
    )


def email_format_sql(database_name: str, schema_name: str, table_name: str, column_name: str) -> str:
    table = _fqn(database_name, schema_name, table_name)
    col = _safe_identifier(column_name)
    pattern = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
    return (
        "SELECT\n"
        "    COUNT(*) AS FAILED_COUNT,\n"
        f"    (SELECT COUNT(*) FROM {table} WHERE {col} IS NOT NULL) AS TOTAL_COUNT\n"
        f"FROM {table}\n"
        f"WHERE {col} IS NOT NULL AND NOT REGEXP_LIKE({col}, '{pattern}')"
    )


def regex_match_sql(
    database_name: str, schema_name: str, table_name: str, column_name: str, pattern: str,
) -> str:
    """Generic 'column must match this regex' check — used for phone
    numbers, country codes, or any other Claude-supplied pattern that maps
    cleanly onto REGEXP_LIKE. pattern is embedded as a string literal, not
    interpolated as executable SQL, so no injection surface beyond a
    malformed regex (which just fails to match, not fails safe/unsafe)."""
    table = _fqn(database_name, schema_name, table_name)
    col = _safe_identifier(column_name)
    escaped = pattern.replace("\\", "\\\\").replace("'", "''")
    return (
        "SELECT\n"
        "    COUNT(*) AS FAILED_COUNT,\n"
        f"    (SELECT COUNT(*) FROM {table} WHERE {col} IS NOT NULL) AS TOTAL_COUNT\n"
        f"FROM {table}\n"
        f"WHERE {col} IS NOT NULL AND NOT REGEXP_LIKE({col}, '{escaped}')"
    )


def duplicate_key_sql(
    database_name: str, schema_name: str, table_name: str, columns: list[str],
) -> str:
    """MULTI_COLUMN uniqueness — e.g. (FIRST_NAME, LAST_NAME, EMAIL) should
    not repeat. FAILED_COUNT = rows belonging to a duplicated combination."""
    table = _fqn(database_name, schema_name, table_name)
    cols = [_safe_identifier(c) for c in columns]
    col_list = ", ".join(cols)
    not_null = " AND ".join(f"{c} IS NOT NULL" for c in cols)
    return (
        "SELECT\n"
        "    COUNT(*) AS FAILED_COUNT,\n"
        f"    (SELECT COUNT(*) FROM {table}) AS TOTAL_COUNT\n"
        "FROM (\n"
        f"    SELECT {col_list}, COUNT(*) AS CNT\n"
        f"    FROM {table}\n"
        f"    WHERE {not_null}\n"
        f"    GROUP BY {col_list}\n"
        "    HAVING COUNT(*) > 1\n"
        ")"
    )


def freshness_sql(
    database_name: str, schema_name: str, table_name: str, column_name: str, max_age_hours: int = 24,
) -> str:
    table = _fqn(database_name, schema_name, table_name)
    col = _safe_identifier(column_name)
    hours = int(max_age_hours)
    return (
        "SELECT\n"
        f"    CASE WHEN MAX({col}) < DATEADD('hour', -{hours}, CURRENT_TIMESTAMP())\n"
        "         THEN 1 ELSE 0 END AS FAILED_COUNT,\n"
        "    1 AS TOTAL_COUNT\n"
        f"FROM {table}"
    )


def referential_integrity_sql(
    database_name: str, schema_name: str, table_name: str, column_name: str,
    ref_database: str, ref_schema: str, ref_table: str, ref_column: str,
) -> str:
    """CROSS_TABLE — rows whose column has no matching row in the
    referenced table's ref_column."""
    primary = _fqn(database_name, schema_name, table_name)
    ref = _fqn(ref_database, ref_schema, ref_table)
    col = _safe_identifier(column_name)
    ref_col = _safe_identifier(ref_column)
    return (
        "SELECT\n"
        "    COUNT(*) AS FAILED_COUNT,\n"
        f"    (SELECT COUNT(*) FROM {primary}) AS TOTAL_COUNT\n"
        f"FROM {primary} t\n"
        f"WHERE t.{col} IS NOT NULL\n"
        "  AND NOT EXISTS (\n"
        f"    SELECT 1 FROM {ref} r WHERE r.{ref_col} = t.{col}\n"
        "  )"
    )


# Shapes Claude can pick by name instead of writing SQL. Each maps to a
# function above; `params` names the threshold_config keys it needs.
TEMPLATE_SHAPES = {
    "not_null":               {"fn": not_null_sql,               "scope": "column", "params": []},
    "uniqueness":              {"fn": uniqueness_sql,             "scope": "column", "params": []},
    "accepted_values":         {"fn": accepted_values_sql,        "scope": "column", "params": ["accepted_values"]},
    "positive_value":          {"fn": positive_value_sql,         "scope": "column", "params": []},
    "email_format":            {"fn": email_format_sql,           "scope": "column", "params": []},
    "regex_match":             {"fn": regex_match_sql,            "scope": "column", "params": ["pattern"]},
    "freshness":               {"fn": freshness_sql,              "scope": "column", "params": ["max_age_hours"]},
    "duplicate_key":           {"fn": duplicate_key_sql,           "scope": "multi_column", "params": []},
    "referential_integrity":   {"fn": referential_integrity_sql,   "scope": "cross_table",
                                 "params": ["ref_database", "ref_schema", "ref_table", "ref_column"]},
}


def render_template(
    shape: str,
    database_name: str,
    schema_name: str,
    table_name: str,
    target_config: dict[str, Any],
    threshold_config: Optional[dict[str, Any]] = None,
) -> str:
    """Render one of TEMPLATE_SHAPES against a specific instance's target +
    threshold config. Raises ValueError on an unknown shape or missing
    target_config/threshold_config keys — callers should treat that as
    'this candidate needs draft_sql instead', not retry with bad input."""
    spec = TEMPLATE_SHAPES.get(shape)
    if not spec:
        raise ValueError(f"Unknown template shape: {shape!r}")
    threshold_config = threshold_config or {}

    if spec["scope"] == "column":
        column = target_config["column"]
        kwargs = {k: threshold_config[k] for k in spec["params"]}
        return spec["fn"](database_name, schema_name, table_name, column, **kwargs)

    if spec["scope"] == "multi_column":
        columns = target_config["columns"]
        return spec["fn"](database_name, schema_name, table_name, columns)

    if spec["scope"] == "cross_table":
        column = target_config["column"]
        kwargs = {k: target_config[k] for k in spec["params"]}
        return spec["fn"](database_name, schema_name, table_name, column, **kwargs)

    raise ValueError(f"Unhandled template scope: {spec['scope']!r}")
