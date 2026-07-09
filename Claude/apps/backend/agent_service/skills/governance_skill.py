"""Governance Skill -- suggests structural/naming/type governance rules.

rule_type: GOVERNANCE

Three checks, all driven purely from column_profiles (no extra metadata fetch
or SQL execution needed -- just column names and data types):

1. DATE_AS_VARCHAR   -- a column clearly named as a date/timestamp is stored
                        as VARCHAR, making date comparisons unreliable.
2. BOOLEAN_AS_VARCHAR -- a column clearly named as a boolean flag is stored as
                        VARCHAR, letting arbitrary strings in where only
                        Y/N/TRUE/FALSE belong.
3. ID_WRONG_TYPE     -- a column clearly named as a primary/foreign key stores
                        values as a non-numeric type, risking accidental
                        string comparisons on join columns.

Unlike the data-quality skills (completeness, freshness, etc.) which fire on
*values* inside the table, these governance checks fire on the *shape* of the
table itself. They are fast (one INFORMATION_SCHEMA query per check, not a
full-table scan), show up with an amber "Governance" badge in the UI, and
represent the category of findings a data governance audit would raise first.

Pure function -- no I/O, no imports from storage.
"""

from __future__ import annotations

from typing import Any

from skills._shared import build_candidate, name_tokens
from tools.rule_template_tools import (
    boolean_as_varchar_sql,
    column_id_wrong_type_sql,
    date_as_varchar_sql,
)

# Token sets for each check -- matched against name_tokens(column_name) so
# "CANDIDATE" (no "DATE" token) and "UPDATED_AT" (no _DATE suffix) are
# correctly excluded.
_DATE_SUFFIX_TOKENS = frozenset({"DATE", "DT", "DAY", "TS", "TIME", "TIMESTAMP"})
_BOOL_PREFIX_TOKENS = frozenset({"IS", "HAS", "CAN", "WAS", "SHOULD", "WILL"})
_BOOL_SUFFIX_TOKENS = frozenset({"FLAG", "FL", "YN", "IND", "INDICATOR", "BOOL"})
_ID_SUFFIX_TOKENS = frozenset({"ID", "KEY", "FK", "PK", "SEQ", "NUM"})

# VARCHAR base types -- data_type is normalised to base type (strips precision)
# before comparing. A column "DATE_STORED" typed VARCHAR(256) normalises to
# "VARCHAR"; "CHARACTER VARYING" also maps to VARCHAR via Snowflake aliases.
_VARCHAR_BASES = frozenset({"VARCHAR", "TEXT", "STRING", "CHAR", "CHARACTER", "NVARCHAR", "NCHAR"})

# Numeric base types -- ID columns should be one of these.
_NUMERIC_BASES = frozenset({
    "NUMBER", "NUMERIC", "DECIMAL", "INT", "INTEGER", "BIGINT",
    "SMALLINT", "TINYINT", "BYTEINT", "FLOAT", "FLOAT4", "FLOAT8",
    "DOUBLE", "REAL",
})


def _base_type(data_type: str) -> str:
    """Normalise a Snowflake data type to its base name (no precision/scale).

    Examples:
        VARCHAR(256)   -> VARCHAR
        NUMBER(38,0)   -> NUMBER
        TIMESTAMP_NTZ(9) -> TIMESTAMP_NTZ
    """
    return data_type.split("(")[0].strip().upper()


def suggest_governance_rules(
    database_name: str,
    schema_name: str,
    table_name: str,
    column_profiles: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return governance rule candidates for one table.

    Each candidate uses rule_fingerprint="source:governance" so the UI can
    render an amber "Governance" badge distinct from "Template" and "Claude".
    """
    candidates: list[dict[str, Any]] = []

    for col in column_profiles:
        column_name: str = col["column_name"]
        data_type: str = col.get("data_type", "") or ""
        base = _base_type(data_type)
        tokens = set(name_tokens(column_name))

        # ── 1. DATE_AS_VARCHAR ────────────────────────────────────────────────
        # Column name ends in a date/time token AND the type is VARCHAR-family.
        if tokens & _DATE_SUFFIX_TOKENS and base in _VARCHAR_BASES:
            rule = build_candidate(
                rule_name=f"{column_name} should not be stored as {base} (date column)",
                rule_type="GOVERNANCE",
                database_name=database_name,
                schema_name=schema_name,
                table_name=table_name,
                column_name=column_name,
                description=(
                    f"{column_name} is named like a date/timestamp column but is stored "
                    f"as {data_type}. Date comparisons (BETWEEN, >, <) on VARCHAR columns "
                    "are lexicographic, not chronological, leading to silent incorrect results."
                ),
                reason=(
                    f"Column name contains a date/time token ({', '.join(sorted(tokens & _DATE_SUFFIX_TOKENS))}) "
                    f"but data type is {data_type} (varchar family)."
                ),
                evidence=[
                    f"Column name: {column_name}",
                    f"Stored type: {data_type}",
                    "Expected type: DATE, TIMESTAMP_NTZ, TIMESTAMP_LTZ, or similar",
                ],
                severity="CRITICAL",
                confidence=0.85,
                threshold_config={"governance_check": "date_as_varchar"},
                generated_sql=date_as_varchar_sql(database_name, schema_name, table_name, column_name),
            )
            rule["rule_fingerprint"] = "source:governance"
            candidates.append(rule)

        # ── 2. BOOLEAN_AS_VARCHAR ─────────────────────────────────────────────
        # Column name starts with IS_/HAS_/CAN_/etc OR ends in _FLAG/_FL/_YN
        # AND the type is VARCHAR-family.
        is_bool_name = bool(
            (tokens & _BOOL_PREFIX_TOKENS and len(tokens) > 1)  # IS_ACTIVE, HAS_CHILDREN
            or tokens & _BOOL_SUFFIX_TOKENS                       # ACTIVE_FLAG, DELETED_YN
        )
        if is_bool_name and base in _VARCHAR_BASES:
            rule = build_candidate(
                rule_name=f"{column_name} should not be stored as {base} (boolean column)",
                rule_type="GOVERNANCE",
                database_name=database_name,
                schema_name=schema_name,
                table_name=table_name,
                column_name=column_name,
                description=(
                    f"{column_name} is named like a boolean flag but is stored as {data_type}. "
                    "VARCHAR boolean columns often accumulate values like 'Yes', 'yes', 'YES', "
                    "'y', '1', 'true', 'TRUE' — inconsistent representations that break "
                    "WHERE IS_ACTIVE = 'Y' filters silently."
                ),
                reason=(
                    f"Column name suggests a boolean (prefix/suffix tokens: "
                    f"{', '.join(sorted((tokens & _BOOL_PREFIX_TOKENS) | (tokens & _BOOL_SUFFIX_TOKENS)))}) "
                    f"but data type is {data_type}."
                ),
                evidence=[
                    f"Column name: {column_name}",
                    f"Stored type: {data_type}",
                    "Expected type: BOOLEAN",
                    "Rows with values outside (Y, N, YES, NO, TRUE, FALSE, 1, 0): checked by rule SQL",
                ],
                severity="WARNING",
                confidence=0.8,
                threshold_config={"governance_check": "boolean_as_varchar"},
                generated_sql=boolean_as_varchar_sql(database_name, schema_name, table_name, column_name),
            )
            rule["rule_fingerprint"] = "source:governance"
            candidates.append(rule)

        # ── 3. ID_WRONG_TYPE ─────────────────────────────────────────────────
        # Column name ends in _ID/_KEY/_FK/_PK AND the type is NOT numeric.
        # Skip if it's also a date or varchar-date column (already covered
        # above or irrelevant) or if the type is already numeric (correct).
        if tokens & _ID_SUFFIX_TOKENS and base not in _NUMERIC_BASES and base not in _VARCHAR_BASES:
            # Only flag non-numeric, non-varchar types (e.g. BOOLEAN, DATE accidentally
            # named CREATED_ID) -- VARCHAR ID columns are a separate common pattern
            # (string surrogate keys) that is debatable, so skip VARCHAR to avoid noise.
            pass  # intentionally excluded -- VARCHAR IDs are common and defensible

        if tokens & _ID_SUFFIX_TOKENS and base not in _NUMERIC_BASES and base in _VARCHAR_BASES:
            # VARCHAR _ID columns: check if the values look non-numeric
            # (flag with lower confidence since string surrogate keys are common).
            rule = build_candidate(
                rule_name=f"{column_name} stored as {base} — verify if numeric key expected",
                rule_type="GOVERNANCE",
                database_name=database_name,
                schema_name=schema_name,
                table_name=table_name,
                column_name=column_name,
                description=(
                    f"{column_name} is named like a key/identifier column but is stored as "
                    f"{data_type}. If this column holds numeric IDs, storing it as VARCHAR "
                    "makes joins between tables with numeric counterparts silently type-cast "
                    "and can prevent index use. If it's intentionally a string key (UUID, "
                    "composite ID), this rule can be safely rejected."
                ),
                reason=(
                    f"Column name ends in an ID/key token ({', '.join(sorted(tokens & _ID_SUFFIX_TOKENS))}) "
                    f"but stored type is {data_type}."
                ),
                evidence=[
                    f"Column name: {column_name}",
                    f"Stored type: {data_type}",
                    "Check: counts rows where value cannot be cast to a number",
                ],
                severity="WARNING",
                confidence=0.65,
                threshold_config={"governance_check": "column_id_wrong_type"},
                generated_sql=column_id_wrong_type_sql(database_name, schema_name, table_name, column_name),
            )
            rule["rule_fingerprint"] = "source:governance"
            candidates.append(rule)

    return candidates
