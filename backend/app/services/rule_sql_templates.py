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
    # FAILED_COUNT must be rows-in-duplicate-groups, NOT the number of
    # duplicate groups — otherwise "1 of 15 fail" is reported when a single
    # duplicated value covers 15 rows, and the sample-rows SELECT (which
    # returns rows) disagrees with the count.
    return (
        "SELECT\n"
        f"    COALESCE(SUM(CNT), 0) AS FAILED_COUNT,\n"
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


def range_sql(
    database_name: str, schema_name: str, table_name: str, column_name: str,
    min_value: Optional[float] = None, max_value: Optional[float] = None,
) -> str:
    """Numeric bounds check. Rows where column is outside [min_value, max_value]
    fail — either bound may be omitted for one-sided constraints. Callers must
    supply at least one bound; a range with neither is not a check."""
    if min_value is None and max_value is None:
        raise ValueError("range_sql needs at least one of min_value/max_value")
    table = _fqn(database_name, schema_name, table_name)
    col = _safe_identifier(column_name)
    predicates = []
    if min_value is not None:
        predicates.append(f"{col} < {float(min_value)}")
    if max_value is not None:
        predicates.append(f"{col} > {float(max_value)}")
    failed_predicate = " OR ".join(predicates)
    return (
        "SELECT\n"
        "    COUNT(*) AS FAILED_COUNT,\n"
        f"    (SELECT COUNT(*) FROM {table} WHERE {col} IS NOT NULL) AS TOTAL_COUNT\n"
        f"FROM {table}\n"
        f"WHERE {col} IS NOT NULL AND ({failed_predicate})"
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
    # FAILED_COUNT is rows-in-duplicate-groups (same reason as uniqueness_sql).
    return (
        "SELECT\n"
        f"    COALESCE(SUM(CNT), 0) AS FAILED_COUNT,\n"
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


def _app_fqn(table: str) -> str:
    """Fully-qualify a DQ_APP table for anomaly checks.

    Anomaly template SQL queries METRIC_SNAPSHOTS / METRIC_BASELINES,
    which live in the app's own schema, not the target table's data
    source. Even when the rule executes against a Snowflake source
    connection, we hit those tables through their fully-qualified path
    so the query works regardless of the connection's current
    database/schema.
    """
    from app.core.config import settings
    db = settings.SNOWFLAKE_DATABASE
    sch = settings.SNOWFLAKE_APP_SCHEMA or "DQ_APP"
    return f"{_safe_identifier(db)}.{_safe_identifier(sch)}.{_safe_identifier(table)}"


def metric_anomaly_sql(
    asset_id: str,
    metric_name: str,
    column_name: Optional[str] = None,
    deviations: float = 3.0,
) -> str:
    """Deviation-based anomaly check: flags when the latest snapshot value
    for (asset, column, metric) is more than `deviations` MADs from the
    stored baseline median.

    Returns (1, 1) on breach, (0, 1) otherwise so the finding shape mirrors
    the metadata-shape contract (fail_count=1, total_count=1, sample_rows=[])
    the frontend already renders correctly.

    Baseline maturity (SAMPLE_COUNT >= 14) is enforced by
    AnomalyProposalAgent at proposal time, so we don't re-gate here — if
    a rule instance exists, the baseline was mature when it was approved.
    """
    snap = _app_fqn("METRIC_SNAPSHOTS")
    base = _app_fqn("METRIC_BASELINES")
    asset_lit = str(asset_id).replace("'", "''")
    metric_lit = str(metric_name).replace("'", "''")
    dev = float(deviations)
    col_pred = (f"COLUMN_NAME = '{str(column_name).replace(chr(39), chr(39) * 2)}'"
                if column_name else "COLUMN_NAME IS NULL")
    return (
        "WITH latest AS (\n"
        "  SELECT METRIC_VALUE\n"
        f"  FROM {snap}\n"
        f"  WHERE ASSET_ID = '{asset_lit}' AND METRIC_NAME = '{metric_lit}' AND {col_pred}\n"
        "  ORDER BY CAPTURED_AT DESC LIMIT 1\n"
        "), baseline AS (\n"
        "  SELECT MEDIAN_VALUE, MAD_VALUE\n"
        f"  FROM {base}\n"
        f"  WHERE ASSET_ID = '{asset_lit}' AND METRIC_NAME = '{metric_lit}' AND {col_pred}\n"
        "  ORDER BY UPDATED_AT DESC LIMIT 1\n"
        ")\n"
        "SELECT\n"
        "  CASE\n"
        "    WHEN (SELECT MAD_VALUE FROM baseline) IS NULL THEN 0\n"
        "    WHEN (SELECT METRIC_VALUE FROM latest) IS NULL THEN 0\n"
        "    WHEN (SELECT MAD_VALUE FROM baseline) = 0\n"
        "         AND (SELECT METRIC_VALUE FROM latest) = (SELECT MEDIAN_VALUE FROM baseline) THEN 0\n"
        "    WHEN (SELECT MAD_VALUE FROM baseline) = 0 THEN 1\n"
        "    WHEN ABS((SELECT METRIC_VALUE FROM latest) - (SELECT MEDIAN_VALUE FROM baseline))\n"
        f"         / (SELECT MAD_VALUE FROM baseline) > {dev} THEN 1\n"
        "    ELSE 0\n"
        "  END AS FAILED_COUNT,\n"
        "  1 AS TOTAL_COUNT"
    )


def metric_relative_change_sql(
    asset_id: str,
    metric_name: str,
    column_name: Optional[str] = None,
    max_pct_change: float = 25.0,
) -> str:
    """Percentage-change anomaly: flags when the latest snapshot differs
    from the previous snapshot for the same (asset, column, metric) by
    more than `max_pct_change` percent. Complements MAD-based detection
    for cases where the baseline is noisy but a sudden jump/drop still
    matters."""
    snap = _app_fqn("METRIC_SNAPSHOTS")
    asset_lit = str(asset_id).replace("'", "''")
    metric_lit = str(metric_name).replace("'", "''")
    pct = float(max_pct_change)
    col_pred = (f"COLUMN_NAME = '{str(column_name).replace(chr(39), chr(39) * 2)}'"
                if column_name else "COLUMN_NAME IS NULL")
    return (
        "WITH ranked AS (\n"
        "  SELECT METRIC_VALUE, ROW_NUMBER() OVER (ORDER BY CAPTURED_AT DESC) AS RN\n"
        f"  FROM {snap}\n"
        f"  WHERE ASSET_ID = '{asset_lit}' AND METRIC_NAME = '{metric_lit}' AND {col_pred}\n"
        "), latest AS (SELECT METRIC_VALUE AS V FROM ranked WHERE RN = 1),\n"
        "prev   AS (SELECT METRIC_VALUE AS V FROM ranked WHERE RN = 2)\n"
        "SELECT\n"
        "  CASE\n"
        "    WHEN (SELECT V FROM prev) IS NULL THEN 0\n"
        "    WHEN (SELECT V FROM latest) IS NULL THEN 0\n"
        "    WHEN (SELECT V FROM prev) = 0 AND (SELECT V FROM latest) = 0 THEN 0\n"
        "    WHEN (SELECT V FROM prev) = 0 THEN 1\n"
        "    WHEN ABS((SELECT V FROM latest) - (SELECT V FROM prev)) * 100.0\n"
        f"         / ABS((SELECT V FROM prev)) > {pct} THEN 1\n"
        "    ELSE 0\n"
        "  END AS FAILED_COUNT,\n"
        "  1 AS TOTAL_COUNT"
    )


def category_disappeared_sql(
    asset_id: str,
    column_name: str,
) -> str:
    """Set-membership drop: flags when a value present in the baseline's
    OBSERVED_SET is missing from the latest snapshot's observed values.

    Both sides are stored under metric_name='observed_categories' — the
    latest snapshot's METRIC_META.values is compared against the baseline's
    OBSERVED_SET (union across the rolling window). Returns (1, 1) if any
    baseline value is absent from the latest observation.
    """
    snap = _app_fqn("METRIC_SNAPSHOTS")
    base = _app_fqn("METRIC_BASELINES")
    asset_lit = str(asset_id).replace("'", "''")
    col_lit = str(column_name).replace("'", "''")
    return (
        "WITH latest AS (\n"
        "  SELECT METRIC_META\n"
        f"  FROM {snap}\n"
        f"  WHERE ASSET_ID = '{asset_lit}' AND METRIC_NAME = 'observed_categories'\n"
        f"    AND COLUMN_NAME = '{col_lit}'\n"
        "  ORDER BY CAPTURED_AT DESC LIMIT 1\n"
        "), baseline AS (\n"
        "  SELECT OBSERVED_SET\n"
        f"  FROM {base}\n"
        f"  WHERE ASSET_ID = '{asset_lit}' AND METRIC_NAME = 'observed_categories'\n"
        f"    AND COLUMN_NAME = '{col_lit}'\n"
        "  ORDER BY UPDATED_AT DESC LIMIT 1\n"
        "), missing AS (\n"
        "  SELECT B.VALUE::STRING AS V\n"
        "  FROM baseline, LATERAL FLATTEN(input => OBSERVED_SET) B\n"
        "  WHERE B.VALUE::STRING NOT IN (\n"
        "    SELECT L.VALUE::STRING\n"
        "    FROM latest, LATERAL FLATTEN(input => METRIC_META:values) L\n"
        "  )\n"
        ")\n"
        "SELECT\n"
        "  CASE WHEN EXISTS (SELECT 1 FROM missing) THEN 1 ELSE 0 END AS FAILED_COUNT,\n"
        "  1 AS TOTAL_COUNT"
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


# Core template shapes Claude can pick by name instead of writing SQL. The
# long tail (positive_value, email_format, ad-hoc constraints, etc.) lives in
# draft_sql — validate_sql + _repair_draft_sql handle it. Keeping this set
# small also keeps the prompt small and prevents Claude from getting anchored
# on a specific check just because a matching shape name exists.
#
# `params` names the threshold_config keys the fn needs; `optional_params`
# names keys the fn also accepts but has defaults for (used by `range` where
# either bound alone is a valid check).
TEMPLATE_SHAPES = {
    "not_null":               {"fn": not_null_sql,               "scope": "column", "params": []},
    "uniqueness":              {"fn": uniqueness_sql,             "scope": "column", "params": []},
    "accepted_values":         {"fn": accepted_values_sql,        "scope": "column", "params": ["accepted_values"]},
    "range":                   {"fn": range_sql,                  "scope": "column", "params": [],
                                 "optional_params": ["min_value", "max_value"]},
    "regex_match":             {"fn": regex_match_sql,            "scope": "column", "params": ["pattern"]},
    "freshness":               {"fn": freshness_sql,              "scope": "column", "params": ["max_age_hours"]},
    "duplicate_key":           {"fn": duplicate_key_sql,           "scope": "multi_column", "params": []},
    "referential_integrity":   {"fn": referential_integrity_sql,   "scope": "cross_table",
                                 "params": ["ref_database", "ref_schema", "ref_table", "ref_column"]},
    # ── Anomaly Tier A shapes ──────────────────────────────────────────
    # These read the app's own METRIC_SNAPSHOTS / METRIC_BASELINES rather
    # than the target table's data, so they use scope="anomaly" — the
    # render path passes asset_id + metric_name (+ optional column) from
    # target_config instead of the target FQN.
    "metric_anomaly":          {"fn": metric_anomaly_sql,          "scope": "anomaly",
                                 "params": ["metric_name"],
                                 "optional_params": ["deviations"]},
    "metric_relative_change":  {"fn": metric_relative_change_sql,  "scope": "anomaly",
                                 "params": ["metric_name"],
                                 "optional_params": ["max_pct_change"]},
    "category_disappeared":    {"fn": category_disappeared_sql,    "scope": "anomaly_column",
                                 "params": []},
}


# Common short aliases Claude tends to use for threshold_config keys. Normalising
# here (in one place) means every render path benefits, and callers don't have
# to teach the model exact param names. Only aliases where the intent is
# unambiguous — no synonyms that could refer to two different keys.
_THRESHOLD_KEY_ALIASES = {
    "min": "min_value",
    "max": "max_value",
    "minimum": "min_value",
    "maximum": "max_value",
    "pattern_regex": "pattern",
    "regex": "pattern",
    "values": "accepted_values",
    "allowed_values": "accepted_values",
}


def _normalize_threshold_config(threshold_config: dict[str, Any]) -> dict[str, Any]:
    """Map short aliases to their canonical keys. On an alias-vs-canonical
    collision, the canonical wins (the model asked for both, honor the
    specific one)."""
    if not threshold_config:
        return {}
    out = dict(threshold_config)
    for alias, canonical in _THRESHOLD_KEY_ALIASES.items():
        if alias in out and canonical not in out:
            out[canonical] = out.pop(alias)
        elif alias in out:
            out.pop(alias)  # canonical already present, drop the alias
    return out


def failing_rows_sample_sql(
    shape: str,
    database_name: str,
    schema_name: str,
    table_name: str,
    target_config: dict[str, Any],
    threshold_config: Optional[dict[str, Any]] = None,
    limit: int = 10,
) -> Optional[str]:
    """Return a SELECT that yields up to `limit` failing rows for the given
    template shape — used to fetch evidence.sample_rows so findings can show
    concrete violating data. Returns None for shapes where "failing rows"
    isn't meaningful (freshness is aggregate; referential_integrity's failing
    rows live in the primary table but the cross-table shape here already
    covers it).

    Uses the same predicate as the count SQL, so numbers and samples agree.
    """
    threshold_config = _normalize_threshold_config(threshold_config or {})
    table = _fqn(database_name, schema_name, table_name)
    n = max(1, int(limit))

    if shape == "not_null":
        col = _safe_identifier(target_config["column"])
        return f"SELECT * FROM {table} WHERE {col} IS NULL LIMIT {n}"

    if shape == "uniqueness":
        col = _safe_identifier(target_config["column"])
        return (
            f"SELECT * FROM {table} WHERE {col} IN (\n"
            f"  SELECT {col} FROM {table} WHERE {col} IS NOT NULL\n"
            f"  GROUP BY {col} HAVING COUNT(*) > 1\n"
            f") LIMIT {n}"
        )

    if shape == "accepted_values":
        col = _safe_identifier(target_config["column"])
        values = threshold_config.get("accepted_values") or []
        if not values:
            return None
        values_sql = _quoted_list(values)
        return (
            f"SELECT * FROM {table} "
            f"WHERE {col} IS NOT NULL AND {col} NOT IN ({values_sql}) LIMIT {n}"
        )

    if shape == "range":
        col = _safe_identifier(target_config["column"])
        min_value = threshold_config.get("min_value")
        max_value = threshold_config.get("max_value")
        predicates = []
        if min_value is not None:
            predicates.append(f"{col} < {float(min_value)}")
        if max_value is not None:
            predicates.append(f"{col} > {float(max_value)}")
        if not predicates:
            return None
        return (
            f"SELECT * FROM {table} "
            f"WHERE {col} IS NOT NULL AND ({' OR '.join(predicates)}) LIMIT {n}"
        )

    if shape == "regex_match":
        col = _safe_identifier(target_config["column"])
        pattern = threshold_config.get("pattern") or ""
        escaped = pattern.replace("\\", "\\\\").replace("'", "''")
        return (
            f"SELECT * FROM {table} "
            f"WHERE {col} IS NOT NULL AND NOT REGEXP_LIKE({col}, '{escaped}') LIMIT {n}"
        )

    if shape == "duplicate_key":
        cols = [_safe_identifier(c) for c in target_config.get("columns", [])]
        if not cols:
            return None
        col_list = ", ".join(cols)
        not_null = " AND ".join(f"{c} IS NOT NULL" for c in cols)
        return (
            f"SELECT * FROM {table} t WHERE ({col_list}) IN (\n"
            f"  SELECT {col_list} FROM {table} WHERE {not_null}\n"
            f"  GROUP BY {col_list} HAVING COUNT(*) > 1\n"
            f") LIMIT {n}"
        )

    if shape == "referential_integrity":
        col = _safe_identifier(target_config["column"])
        ref = _fqn(target_config["ref_database"], target_config["ref_schema"], target_config["ref_table"])
        ref_col = _safe_identifier(target_config["ref_column"])
        return (
            f"SELECT * FROM {table} t "
            f"WHERE t.{col} IS NOT NULL AND NOT EXISTS "
            f"(SELECT 1 FROM {ref} r WHERE r.{ref_col} = t.{col}) LIMIT {n}"
        )

    # freshness is aggregate — nothing to sample.
    return None


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
    threshold_config = _normalize_threshold_config(threshold_config or {})

    if spec["scope"] == "column":
        column = target_config["column"]
        kwargs = {k: threshold_config[k] for k in spec["params"]}
        for k in spec.get("optional_params", []):
            if threshold_config.get(k) is not None:
                kwargs[k] = threshold_config[k]
        return spec["fn"](database_name, schema_name, table_name, column, **kwargs)

    if spec["scope"] == "multi_column":
        columns = target_config["columns"]
        return spec["fn"](database_name, schema_name, table_name, columns)

    if spec["scope"] == "cross_table":
        column = target_config["column"]
        kwargs = {k: target_config[k] for k in spec["params"]}
        return spec["fn"](database_name, schema_name, table_name, column, **kwargs)

    if spec["scope"] == "anomaly":
        # target_config: {asset_id, metric_name, column?}
        # threshold_config: {deviations?} or {max_pct_change?}
        kwargs = {"asset_id": target_config["asset_id"],
                  "metric_name": target_config["metric_name"]}
        if target_config.get("column"):
            kwargs["column_name"] = target_config["column"]
        for k in spec.get("optional_params", []):
            if threshold_config.get(k) is not None:
                kwargs[k] = threshold_config[k]
        return spec["fn"](**kwargs)

    if spec["scope"] == "anomaly_column":
        # target_config: {asset_id, column}
        return spec["fn"](
            asset_id=target_config["asset_id"],
            column_name=target_config["column"],
        )

    raise ValueError(f"Unhandled template scope: {spec['scope']!r}")
