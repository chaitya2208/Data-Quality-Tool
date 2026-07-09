"""Rule Template Tools -- deterministic, SELECT-only SQL templates.

One function per rule shape (completeness, uniqueness, accepted-values,
positive-amount, freshness), plus a dispatcher, render_sql_for_rule(), that
picks the right template given a rule object (the same dict shape
skills/_shared.py's build_candidate() produces: rule_type, database_name,
schema_name, table_name, column_name, threshold_config).

These are the single source of truth for rule SQL. The 5 skills in skills/
(completeness_skill.py, uniqueness_skill.py, validity_skill.py,
freshness_skill.py, volume_skill.py) call into this module instead of
building SQL inline -- they used to build it themselves; refactored here to
avoid two places generating (and potentially drifting on) the same SQL.

Every template is a fixed, hand-written SELECT string with only identifiers
and literal values substituted in (via _safe_identifier() and quote-escaping
for string literals) -- there is no free-form or LLM-generated SQL path here,
so every template is SELECT-only by construction. This satisfies the
definition of done for TEMPLATE-based rules specifically. It does not replace
the still-missing general SQL Validator (see docs/deferred-and-future-work.md
#1) -- that gate is still required for any future LLM-generated SQL that
doesn't come from a template.

Additions beyond the 5 requested templates: email_format_sql() and
volume_sql(). The 5 skills already produce EMAIL-format and VOLUME rule
candidates (validity_skill.py, volume_skill.py) that need SQL too -- without
these, render_sql_for_rule() couldn't handle every rule_type the skills
actually emit.

render_sql_for_instance() (bottom of this file) is the Layer-1/Layer-2
dispatcher from docs/rules-architecture.md 5.6/4.4 for the new
RULE_DEFINITIONS + RULE_INSTANCES model -- it renders a definition's
sql_template against a scope + target_config instead of a flat rule dict.
It is additive: render_sql_for_rule() and render_sample_sql_for_rule() are
unchanged and still used by the existing 6 skills.
"""

from __future__ import annotations

import re
from typing import Any

from tools.snowflake_metadata_tools import _safe_identifier

# Same default as freshness_skill.py -- kept in one place so both agree.
_DEFAULT_MAX_AGE_HOURS = 24

# Sample failed rows: how many offending rows to pull back for a human to
# look at. No LIMIT enforcement exists in sql_validation_tools.py (confirmed
# -- a row-returning SELECT passes validate_select_only() exactly like the
# COUNT(*) aggregate templates do), so every sample_sql function below must
# hardcode its own LIMIT.
_SAMPLE_ROW_LIMIT = 10

# render_sql_for_instance()'s {params.<key>} placeholder -- matches a single
# dotted key so a template can reference threshold_config["max_age_hours"] as
# literal text "{params.max_age_hours}".
_PARAM_PLACEHOLDER_RE = re.compile(r"\{params\.([A-Za-z0-9_]+)\}")

# Shared message for every scope where a definition has no sql_template --
# per docs/rules-architecture.md 5.5's SQL trust chain, this function only
# ever renders TRUSTED template SQL. A definition with no sql_template is a
# CUSTOM/new definition whose SQL comes from Claude's draft_sql instead, and
# draft_sql must go through SQL Generation + Validation (sql_validation_tools.py),
# never straight through this function.
_NO_SQL_TEMPLATE_MSG = (
    "no sql_template for this definition -- CUSTOM definitions must be "
    "rendered from Claude's draft_sql, not this function"
)


def _fqn(database_name: str, schema_name: str, table_name: str) -> str:
    return (
        f"{_safe_identifier(database_name)}."
        f"{_safe_identifier(schema_name)}."
        f"{_safe_identifier(table_name)}"
    )


def _quoted_list(values: list[Any]) -> str:
    """Comma-separated SQL string literals, with embedded quotes escaped
    (Snowflake string-literal escape is doubling the quote: '' ). Values
    here come from real observed data (e.g. top_values), not a fixed list,
    so this must not be skipped even though it looks like "just formatting".
    """
    return ", ".join("'{}'".format(str(v).replace("'", "''")) for v in values)


def completeness_sql(
    database_name: str, schema_name: str, table_name: str, column_name: str
) -> str:
    table = _fqn(database_name, schema_name, table_name)
    col = _safe_identifier(column_name)
    return (
        "SELECT\n"
        "    COUNT(*) AS FAILED_COUNT,\n"
        f"    (SELECT COUNT(*) FROM {table}) AS TOTAL_COUNT\n"
        f"FROM {table}\n"
        f"WHERE {col} IS NULL"
    )


def completeness_sample_sql(
    database_name: str, schema_name: str, table_name: str, column_name: str
) -> str:
    """Row-returning counterpart to completeness_sql() -- same WHERE
    predicate, SELECT * ... LIMIT N instead of COUNT(*), for the sample
    failed rows feature (see agents/rule_test_execution_agent.py /
    agents/rule_execution_agent.py)."""
    table = _fqn(database_name, schema_name, table_name)
    col = _safe_identifier(column_name)
    return f"SELECT *\nFROM {table}\nWHERE {col} IS NULL\nLIMIT {_SAMPLE_ROW_LIMIT}"


def uniqueness_sql(
    database_name: str, schema_name: str, table_name: str, column_name: str
) -> str:
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


def uniqueness_sample_sql(
    database_name: str, schema_name: str, table_name: str, column_name: str
) -> str:
    """Row-returning counterpart to uniqueness_sql() -- rows whose value is
    one of the duplicated keys, not an aggregate count of them."""
    table = _fqn(database_name, schema_name, table_name)
    col = _safe_identifier(column_name)
    return (
        f"SELECT *\nFROM {table}\n"
        f"WHERE {col} IN (\n"
        f"    SELECT {col} FROM {table}\n"
        f"    WHERE {col} IS NOT NULL\n"
        f"    GROUP BY {col}\n"
        "    HAVING COUNT(*) > 1\n"
        ")\n"
        f"LIMIT {_SAMPLE_ROW_LIMIT}"
    )


def accepted_values_sql(
    database_name: str,
    schema_name: str,
    table_name: str,
    column_name: str,
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
        f"    (SELECT COUNT(*) FROM {table}) AS TOTAL_COUNT\n"
        f"FROM {table}\n"
        f"WHERE {col} NOT IN ({values_sql})"
    )


def accepted_values_sample_sql(
    database_name: str,
    schema_name: str,
    table_name: str,
    column_name: str,
    accepted_values: list[Any],
) -> str:
    """Row-returning counterpart to accepted_values_sql()."""
    if not accepted_values:
        raise ValueError("accepted_values must not be empty")
    table = _fqn(database_name, schema_name, table_name)
    col = _safe_identifier(column_name)
    values_sql = _quoted_list(accepted_values)
    return (
        f"SELECT *\nFROM {table}\n"
        f"WHERE {col} NOT IN ({values_sql})\n"
        f"LIMIT {_SAMPLE_ROW_LIMIT}"
    )


def positive_amount_sql(
    database_name: str, schema_name: str, table_name: str, column_name: str
) -> str:
    table = _fqn(database_name, schema_name, table_name)
    col = _safe_identifier(column_name)
    return (
        "SELECT\n"
        "    COUNT(*) AS FAILED_COUNT,\n"
        f"    (SELECT COUNT(*) FROM {table}) AS TOTAL_COUNT\n"
        f"FROM {table}\n"
        f"WHERE {col} <= 0"
    )


def positive_amount_sample_sql(
    database_name: str, schema_name: str, table_name: str, column_name: str
) -> str:
    """Row-returning counterpart to positive_amount_sql()."""
    table = _fqn(database_name, schema_name, table_name)
    col = _safe_identifier(column_name)
    return f"SELECT *\nFROM {table}\nWHERE {col} <= 0\nLIMIT {_SAMPLE_ROW_LIMIT}"


def freshness_sql(
    database_name: str,
    schema_name: str,
    table_name: str,
    timestamp_column: str,
    max_age_hours: int = _DEFAULT_MAX_AGE_HOURS,
) -> str:
    table = _fqn(database_name, schema_name, table_name)
    col = _safe_identifier(timestamp_column)
    hours = int(max_age_hours)
    return (
        "SELECT\n"
        "    CASE\n"
        f"        WHEN MAX({col}) < DATEADD('hour', -{hours}, CURRENT_TIMESTAMP())\n"
        "        THEN 1\n"
        "        ELSE 0\n"
        "    END AS FAILED_COUNT,\n"
        "    1 AS TOTAL_COUNT\n"
        f"FROM {table}"
    )


def freshness_evidence_sql(
    database_name: str, schema_name: str, table_name: str, timestamp_column: str
) -> str:
    """Not row-level -- FRESHNESS has no per-row predicate to sample (the
    check is about the table's single MAX(timestamp), not individual rows).
    Used only to build the evidence fact inside sample_failed_rows (see
    tools/sample_query_tools.py) -- returns the one most-recent timestamp
    value as a stand-in for "here's the evidence," not a list of failures.
    """
    table = _fqn(database_name, schema_name, table_name)
    col = _safe_identifier(timestamp_column)
    return f"SELECT MAX({col}) AS MOST_RECENT_VALUE\nFROM {table}"


def email_format_sql(
    database_name: str, schema_name: str, table_name: str, column_name: str
) -> str:
    """Not one of the 5 requested templates -- added because validity_skill.py
    already proposes EMAIL-format rule candidates and needs SQL for them.
    """
    table = _fqn(database_name, schema_name, table_name)
    col = _safe_identifier(column_name)
    # Fixed pattern, not user input -- escaped defensively anyway.
    pattern = r"^[^@\s]+@[^@\s]+\.[^@\s]+$".replace("\\", "\\\\").replace("'", "''")
    return (
        "SELECT\n"
        "    COUNT(*) AS FAILED_COUNT,\n"
        f"    (SELECT COUNT(*) FROM {table}) AS TOTAL_COUNT\n"
        f"FROM {table}\n"
        f"WHERE {col} IS NOT NULL\n"
        f"  AND NOT REGEXP_LIKE({col}, '{pattern}')"
    )


def email_format_sample_sql(
    database_name: str, schema_name: str, table_name: str, column_name: str
) -> str:
    """Row-returning counterpart to email_format_sql()."""
    table = _fqn(database_name, schema_name, table_name)
    col = _safe_identifier(column_name)
    pattern = r"^[^@\s]+@[^@\s]+\.[^@\s]+$".replace("\\", "\\\\").replace("'", "''")
    return (
        f"SELECT *\nFROM {table}\n"
        f"WHERE {col} IS NOT NULL\n"
        f"  AND NOT REGEXP_LIKE({col}, '{pattern}')\n"
        f"LIMIT {_SAMPLE_ROW_LIMIT}"
    )


def volume_sql(database_name: str, schema_name: str, table_name: str) -> str:
    """Not one of the 5 requested templates -- added because volume_skill.py
    already proposes a row-count-> 0 rule candidate and needs SQL for it.

    Kept unchanged (including its TOTAL_COUNT=1 quirk) for backward
    compatibility with any already-approved rule using this shape --
    render_sql_for_rule() only dispatches here when threshold_config has no
    historical_avg_row_count key (i.e. rules created before volume_historical_sql()
    existed, or a table with <3 profiles' worth of history). See
    volume_historical_sql() for the real-row-count-reporting replacement.
    """
    table = _fqn(database_name, schema_name, table_name)
    return (
        "SELECT\n"
        "    CASE WHEN COUNT(*) > 0 THEN 0 ELSE 1 END AS FAILED_COUNT,\n"
        "    1 AS TOTAL_COUNT\n"
        f"FROM {table}"
    )


def volume_historical_sql(
    database_name: str,
    schema_name: str,
    table_name: str,
    historical_avg: float,
    warning_band_pct: float = 30,
) -> str:
    """Historical-average volume check -- fails when the current row count
    deviates from historical_avg by more than warning_band_pct percent.
    Reports the real row count as TOTAL_COUNT (unlike the legacy volume_sql(),
    which hardcodes TOTAL_COUNT=1) -- needed so a VOLUME alert's sample
    evidence (tools/sample_query_tools.py) has a real total to show.

    When historical_avg == 0, the formula degrades to COUNT(*) > 0 with no
    special-casing needed: ABS(COUNT(*) - 0) > 0 * pct/100.0 simplifies to
    COUNT(*) > 0 in SQL terms.
    """
    table = _fqn(database_name, schema_name, table_name)
    return (
        "SELECT\n"
        f"    CASE WHEN ABS(COUNT(*) - {historical_avg}) > {historical_avg} * {warning_band_pct}/100.0\n"
        "         THEN 1 ELSE 0 END AS FAILED_COUNT,\n"
        "    COUNT(*) AS TOTAL_COUNT\n"
        f"FROM {table}"
    )


def date_as_varchar_sql(
    database_name: str, schema_name: str, table_name: str, column_name: str
) -> str:
    """Governance: rows where a date-named VARCHAR column contains values that
    cannot be parsed as a date (TRY_TO_DATE returns NULL on failure).

    Counts non-NULL rows that fail TRY_TO_DATE -- a NULL original value is
    excluded (that's a COMPLETENESS concern, not a type-mismatch concern).
    FAILED_COUNT = rows with a non-parseable date string.
    """
    table = _fqn(database_name, schema_name, table_name)
    col = _safe_identifier(column_name)
    return (
        "SELECT\n"
        "    COUNT(*) AS FAILED_COUNT,\n"
        f"    (SELECT COUNT(*) FROM {table} WHERE {col} IS NOT NULL) AS TOTAL_COUNT\n"
        f"FROM {table}\n"
        f"WHERE {col} IS NOT NULL\n"
        f"  AND TRY_TO_DATE({col}) IS NULL"
    )


def boolean_as_varchar_sql(
    database_name: str, schema_name: str, table_name: str, column_name: str
) -> str:
    """Governance: rows where a boolean-named VARCHAR column contains values
    outside the common boolean representations (Y/N/YES/NO/TRUE/FALSE/1/0).

    FAILED_COUNT = rows with non-NULL values that are not a recognised boolean.
    TOTAL_COUNT  = rows with non-NULL values (blanks/NULLs excluded -- those
    are a COMPLETENESS concern).
    """
    table = _fqn(database_name, schema_name, table_name)
    col = _safe_identifier(column_name)
    return (
        "SELECT\n"
        "    COUNT(*) AS FAILED_COUNT,\n"
        f"    (SELECT COUNT(*) FROM {table} WHERE {col} IS NOT NULL) AS TOTAL_COUNT\n"
        f"FROM {table}\n"
        f"WHERE {col} IS NOT NULL\n"
        f"  AND UPPER(TRIM({col})) NOT IN ('Y', 'N', 'YES', 'NO', 'TRUE', 'FALSE', '1', '0')"
    )


def column_id_wrong_type_sql(
    database_name: str, schema_name: str, table_name: str, column_name: str
) -> str:
    """Governance: rows where a key-named VARCHAR column contains values that
    cannot be cast to a number (i.e. are genuinely non-numeric strings).

    FAILED_COUNT = rows whose value is non-NULL and non-castable to NUMBER.
    If the column holds UUIDs or composite string keys, every row will
    "fail" -- that's by design; the human reviewer decides whether to reject
    the rule.
    """
    table = _fqn(database_name, schema_name, table_name)
    col = _safe_identifier(column_name)
    return (
        "SELECT\n"
        "    COUNT(*) AS FAILED_COUNT,\n"
        f"    (SELECT COUNT(*) FROM {table} WHERE {col} IS NOT NULL) AS TOTAL_COUNT\n"
        f"FROM {table}\n"
        f"WHERE {col} IS NOT NULL\n"
        f"  AND TRY_TO_NUMBER({col}) IS NULL"
    )


def render_sql_for_rule(rule: dict[str, Any]) -> str:
    """Given a rule object (the dict shape skills/_shared.py's
    build_candidate() produces), return the SELECT-only SQL for it.

    VALIDITY is one rule_type covering 3 different templates (email format,
    positive amount, accepted values) -- disambiguated by which key is
    present in threshold_config, since rule_type alone can't tell them apart.
    """
    rule_type = rule["rule_type"]
    database_name = rule["database_name"]
    schema_name = rule["schema_name"]
    table_name = rule["table_name"]
    column_name = rule.get("column_name")
    threshold_config = rule.get("threshold_config") or {}

    if rule_type == "COMPLETENESS":
        return completeness_sql(database_name, schema_name, table_name, column_name)

    if rule_type == "UNIQUENESS":
        return uniqueness_sql(database_name, schema_name, table_name, column_name)

    if rule_type == "VALIDITY":
        if "accepted_values" in threshold_config:
            return accepted_values_sql(
                database_name,
                schema_name,
                table_name,
                column_name,
                threshold_config["accepted_values"],
            )
        if "pattern" in threshold_config:
            return email_format_sql(database_name, schema_name, table_name, column_name)
        if "min_value" in threshold_config:
            return positive_amount_sql(database_name, schema_name, table_name, column_name)
        raise ValueError(
            f"Cannot determine VALIDITY sub-type for rule {rule.get('rule_name')!r}: "
            f"threshold_config has none of accepted_values/pattern/min_value"
        )

    if rule_type == "FRESHNESS":
        max_age_hours = threshold_config.get("max_age_hours", _DEFAULT_MAX_AGE_HOURS)
        return freshness_sql(
            database_name, schema_name, table_name, column_name, max_age_hours
        )

    if rule_type == "VOLUME":
        if "historical_avg_row_count" in threshold_config:
            return volume_historical_sql(
                database_name,
                schema_name,
                table_name,
                threshold_config["historical_avg_row_count"],
                threshold_config.get("warning_band_pct", 30),
            )
        return volume_sql(database_name, schema_name, table_name)

    if rule_type == "GOVERNANCE":
        check = threshold_config.get("governance_check")
        if check == "date_as_varchar":
            return date_as_varchar_sql(database_name, schema_name, table_name, column_name)
        if check == "boolean_as_varchar":
            return boolean_as_varchar_sql(database_name, schema_name, table_name, column_name)
        if check == "column_id_wrong_type":
            return column_id_wrong_type_sql(database_name, schema_name, table_name, column_name)
        raise ValueError(
            f"Unknown GOVERNANCE check: {check!r}; "
            "expected date_as_varchar / boolean_as_varchar / column_id_wrong_type"
        )

    raise ValueError(f"Unknown rule_type: {rule_type!r}")


def render_sample_sql_for_rule(rule: dict[str, Any], limit: int = _SAMPLE_ROW_LIMIT) -> str | None:
    """Row-returning SELECT for one rule's sample failed rows, or None when
    the rule_type/scope has no per-row predicate to sample (FRESHNESS/VOLUME/
    TABLE-scope are table-level aggregates; an unknown/Claude-only rule_type
    with no matching SQL shape has no template at all). Callers
    (agents/rule_test_execution_agent.py, agents/rule_execution_agent.py, via
    tools/sample_query_tools.py) build a fallback evidence/note dict instead
    of a row list when this returns None.

    Same VALIDITY sub-type disambiguation as render_sql_for_rule().

    `rule` here is either a RECOMMENDED_INSTANCES row (key is
    ``generated_sql``, used by rule_test_execution_agent.py's pre-approval
    path) or a RULE_INSTANCES row (key is ``rule_sql``, used by
    rule_execution_agent.py's post-approval path) -- checking both is
    required: this function previously read only ``generated_sql``, which
    made every post-approval sample-row fallback silently return None for
    any rule whose row-level shape wasn't one of the COMPLETENESS/UNIQUENESS/
    VALIDITY templates above (confirmed directly against a real approved
    CONDITIONAL rule -- the alert's violation_samples came back empty with
    "no SQL available to sample" even though rule_sql held a real,
    sampleable query).
    """
    rule_type = rule.get("rule_type")
    scope = rule.get("scope")
    database_name = rule["database_name"]
    schema_name = rule["schema_name"]
    table_name = rule["table_name"]
    column_name = rule.get("column_name")
    threshold_config = rule.get("threshold_config") or {}

    if rule_type == "COMPLETENESS":
        return completeness_sample_sql(database_name, schema_name, table_name, column_name)

    if rule_type == "UNIQUENESS":
        return uniqueness_sample_sql(database_name, schema_name, table_name, column_name)

    if rule_type == "VALIDITY":
        if "accepted_values" in threshold_config:
            return accepted_values_sample_sql(
                database_name,
                schema_name,
                table_name,
                column_name,
                threshold_config["accepted_values"],
            )
        if "pattern" in threshold_config:
            return email_format_sample_sql(database_name, schema_name, table_name, column_name)
        if "min_value" in threshold_config:
            return positive_amount_sample_sql(database_name, schema_name, table_name, column_name)
        # Fall through to COUNT_IF/WHERE extraction for Claude-written VALIDITY SQL.

    # FRESHNESS/VOLUME rule_type, or TABLE scope regardless of rule_type:
    # table-level aggregates, no per-row predicate to sample.
    if rule_type in ("FRESHNESS", "VOLUME") or scope == "TABLE":
        return None

    generated_sql = rule.get("generated_sql") or rule.get("rule_sql") or ""

    # CONDITIONAL's default shape (rule_template_tools._render_conditional_default)
    # and any legacy flat Claude-written rule both use
    # "COUNT_IF(<condition>) AS FAILED_COUNT" -- the WHERE clause of a sample
    # query is just that same condition. Checked first since it is the more
    # specific/reliable shape when present.
    match = re.search(r"COUNT_IF\((.+?)\)\s+AS\s+FAILED_COUNT", generated_sql, re.IGNORECASE | re.DOTALL)
    if match:
        condition = match.group(1).strip()
        fqn = _fqn(database_name, schema_name, table_name)
        return f"SELECT *\nFROM {fqn}\nWHERE {condition}\nLIMIT {limit}"

    # COLUMN/MULTI_COLUMN/CROSS_TABLE's default rendering
    # (_render_column_scope/_render_multi_column_scope/_render_cross_table_default)
    # all end in "...FROM <fqn>[ t]<whitespace>WHERE <predicate>" (COUNT(*),
    # not COUNT_IF) -- extract everything after that marker as the same
    # predicate a sample query needs. CROSS_TABLE's predicate references the
    # primary table via a "t" alias, so the marker (and therefore the sample
    # query) must preserve it; COLUMN/MULTI_COLUMN have no alias.
    if scope in ("COLUMN", "MULTI_COLUMN", "CROSS_TABLE"):
        fqn = _fqn(database_name, schema_name, table_name)
        alias = " t" if scope == "CROSS_TABLE" else ""
        marker_pattern = re.escape(f"FROM {fqn}{alias}") + r"\s+WHERE\s+(.+)$"
        match = re.search(marker_pattern, generated_sql, re.IGNORECASE | re.DOTALL)
        if match:
            predicate = match.group(1).strip()
            return f"SELECT *\nFROM {fqn}{alias}\nWHERE {predicate}\nLIMIT {limit}"

    return None


# ---------------------------------------------------------------------------
# render_sql_for_instance() -- the RULE_DEFINITIONS/RULE_INSTANCES dispatcher
# from docs/rules-architecture.md 5.6, 4.4 and 4.5. See the module docstring
# for how this relates to render_sql_for_rule() above (unchanged, still used
# by the 6 skills).
# ---------------------------------------------------------------------------

# Operators CONDITIONAL scope's target_config:when_operator is allowed to be.
# Per 5.6/9's "if STATUS = SHIPPED then SHIPPED_DATE required" example --
# an arbitrary operator string must never be interpolated into SQL unchecked.
_VALID_CONDITIONAL_OPERATORS = {"=", "!=", ">", "<", ">=", "<="}


def _quote_scalar(value: Any) -> str:
    """Render a single target_config scalar (CONDITIONAL's when_value) as a
    SQL literal -- unquoted for int/float, single-quoted with the embedded
    quote doubled (same escaping convention as _quoted_list()) otherwise.
    """
    if isinstance(value, (int, float)):
        return str(value)
    return "'{}'".format(str(value).replace("'", "''"))


def _substitute_params(template: str, threshold_config: dict[str, Any] | None) -> str:
    """Replace every {params.<key>} placeholder in a sql_template with the
    matching threshold_config[key] -- numbers rendered as literal numbers,
    lists rendered as a parenthesized quoted list (accepted_values_sql()'s
    pattern), everything else rendered as a quoted, quote-escaped string
    literal (_quoted_list()'s pattern for a single value).
    """
    threshold_config = threshold_config or {}

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in threshold_config:
            raise ValueError(
                f"Missing threshold_config key {key!r} for template "
                f"placeholder {{params.{key}}}"
            )
        value = threshold_config[key]
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, list):
            return f"({_quoted_list(value)})"
        return "'{}'".format(str(value).replace("'", "''"))

    return _PARAM_PLACEHOLDER_RE.sub(_replace, template)


def _render_template(
    template: str,
    database_name: str,
    schema_name: str,
    table_name: str,
    target_replacements: dict[str, str] | None,
    threshold_config: dict[str, Any] | None,
) -> str:
    """Substitute {database}/{schema}/{table}, then any scope-specific
    {target.*} placeholders (already safely-quoted identifier text supplied
    by the caller), then {params.<key>} placeholders (via
    _substitute_params()), into one system sql_template. Plain str.replace
    is used for the fixed {database}/{schema}/{table}/{target.*} keys since
    they are exact literal tokens, not a pattern; {params.*} needs the regex
    pass because the key portion is data-driven.
    """
    rendered = template
    rendered = rendered.replace("{database}", _safe_identifier(database_name))
    rendered = rendered.replace("{schema}", _safe_identifier(schema_name))
    rendered = rendered.replace("{table}", _safe_identifier(table_name))
    for placeholder, value in (target_replacements or {}).items():
        rendered = rendered.replace(placeholder, value)
    return _substitute_params(rendered, threshold_config)


def _render_column_scope(
    definition: dict[str, Any],
    target_config: dict[str, Any],
    database_name: str,
    schema_name: str,
    table_name: str,
    threshold_config: dict[str, Any] | None,
) -> str:
    """COLUMN scope (5.6 table row 1): sql_template is a complete SELECT
    statement; only {target.column} plus the shared {database}/{schema}/
    {table}/{params.*} placeholders are substituted.
    """
    sql_template = definition.get("sql_template")
    if not sql_template:
        raise ValueError(_NO_SQL_TEMPLATE_MSG)
    target_replacements = {"{target.column}": _safe_identifier(target_config["column"])}
    return _render_template(
        sql_template, database_name, schema_name, table_name, target_replacements, threshold_config
    )


def _render_multi_column_scope(
    definition: dict[str, Any],
    target_config: dict[str, Any],
    database_name: str,
    schema_name: str,
    table_name: str,
    threshold_config: dict[str, Any] | None,
) -> str:
    """MULTI_COLUMN scope (5.6 table row 2): unlike COLUMN, sql_template here
    is only the boolean predicate (e.g. "{target.columns.0} > {target.columns.1}"
    for a START_DATE/END_DATE ordering check) -- this function supplies the
    surrounding COUNT(*)/TOTAL_COUNT boilerplate, matching the shape of
    completeness_sql() etc. above.
    """
    sql_template = definition.get("sql_template")
    if not sql_template:
        raise ValueError(_NO_SQL_TEMPLATE_MSG)
    columns = target_config["columns"]
    target_replacements = {
        f"{{target.columns.{i}}}": _safe_identifier(col) for i, col in enumerate(columns)
    }
    predicate = _render_template(
        sql_template, database_name, schema_name, table_name, target_replacements, threshold_config
    )
    table = _fqn(database_name, schema_name, table_name)
    return (
        "SELECT\n"
        "    COUNT(*) AS FAILED_COUNT,\n"
        f"    (SELECT COUNT(*) FROM {table}) AS TOTAL_COUNT\n"
        f"FROM {table}\n"
        f"WHERE {predicate}"
    )


def _render_table_scope(
    definition: dict[str, Any],
    target_config: dict[str, Any],
    database_name: str,
    schema_name: str,
    table_name: str,
    threshold_config: dict[str, Any] | None,
) -> str:
    """TABLE scope (5.6 table row 3): same complete-statement template
    substitution as COLUMN, minus any {target.*} substitution -- target_config
    is {} for this scope since there is no column reference (true table-level
    aggregate, e.g. freshness/volume-shaped checks).
    """
    sql_template = definition.get("sql_template")
    if not sql_template:
        raise ValueError(_NO_SQL_TEMPLATE_MSG)
    return _render_template(
        sql_template, database_name, schema_name, table_name, None, threshold_config
    )


def _render_cross_table_default(
    target_config: dict[str, Any],
    database_name: str,
    schema_name: str,
    table_name: str,
) -> str:
    """Default CROSS_TABLE shape (5.6 table row 4, used when the definition
    has no sql_template): a referential-integrity anti-join -- rows in the
    primary table whose target column has no matching row in the referenced
    table's ref_column.
    """
    primary_fqn = _fqn(database_name, schema_name, table_name)
    ref_fqn = _fqn(
        target_config["ref_database"], target_config["ref_schema"], target_config["ref_table"]
    )
    col = _safe_identifier(target_config["column"])
    ref_col = _safe_identifier(target_config["ref_column"])
    return (
        "SELECT\n"
        "    COUNT(*) AS FAILED_COUNT,\n"
        f"    (SELECT COUNT(*) FROM {primary_fqn}) AS TOTAL_COUNT\n"
        f"FROM {primary_fqn} t\n"
        f"WHERE t.{col} IS NOT NULL\n"
        "  AND NOT EXISTS (\n"
        f"    SELECT 1 FROM {ref_fqn} r WHERE r.{ref_col} = t.{col}\n"
        "  )"
    )


def _render_cross_table_scope(
    definition: dict[str, Any],
    target_config: dict[str, Any],
    database_name: str,
    schema_name: str,
    table_name: str,
    threshold_config: dict[str, Any] | None,
) -> str:
    """CROSS_TABLE scope (5.6 table row 4): if the definition carries a
    sql_template, treat it as a complete statement and substitute
    {target.column}/{target.ref_database}/{target.ref_schema}/
    {target.ref_table}/{target.ref_column} (plus {database}/{schema}/{table}/
    {params.*}) as an override. Otherwise fall back to the anti-join default
    -- this is the one scope where target_config's own structure is enough
    to generate a sensible default without a template.
    """
    sql_template = definition.get("sql_template")
    if not sql_template:
        return _render_cross_table_default(target_config, database_name, schema_name, table_name)
    target_replacements = {
        "{target.column}": _safe_identifier(target_config["column"]),
        "{target.ref_database}": _safe_identifier(target_config["ref_database"]),
        "{target.ref_schema}": _safe_identifier(target_config["ref_schema"]),
        "{target.ref_table}": _safe_identifier(target_config["ref_table"]),
        "{target.ref_column}": _safe_identifier(target_config["ref_column"]),
    }
    return _render_template(
        sql_template, database_name, schema_name, table_name, target_replacements, threshold_config
    )


def _render_conditional_default(
    target_config: dict[str, Any],
    when_operator: str,
    database_name: str,
    schema_name: str,
    table_name: str,
) -> str:
    """Default CONDITIONAL shape (5.6 table row 5, used when the definition
    has no sql_template): the "if STATUS = SHIPPED then SHIPPED_DATE required"
    example from section 9 -- fails when the WHEN condition holds and the
    target column is null.
    """
    table = _fqn(database_name, schema_name, table_name)
    col = _safe_identifier(target_config["column"])
    when_col = _safe_identifier(target_config["when_column"])
    when_value_sql = _quote_scalar(target_config["when_value"])
    condition = f"{when_col} {when_operator} {when_value_sql}"
    return (
        "SELECT\n"
        f"    COUNT_IF({condition} AND {col} IS NULL) AS FAILED_COUNT,\n"
        f"    COUNT_IF({condition}) AS TOTAL_COUNT\n"
        f"FROM {table}"
    )


def _render_conditional_scope(
    definition: dict[str, Any],
    target_config: dict[str, Any],
    database_name: str,
    schema_name: str,
    table_name: str,
    threshold_config: dict[str, Any] | None,
) -> str:
    """CONDITIONAL scope (5.6 table row 5): same sql_template-override-if-
    present-else-default approach as CROSS_TABLE. when_operator is validated
    against a fixed whitelist before it is used anywhere (default shape or
    interpolated into a template) -- it is never a free-form operator string.
    """
    when_operator = target_config["when_operator"]
    if when_operator not in _VALID_CONDITIONAL_OPERATORS:
        raise ValueError(
            f"Invalid when_operator {when_operator!r}; expected one of "
            f"{sorted(_VALID_CONDITIONAL_OPERATORS)}"
        )
    sql_template = definition.get("sql_template")
    if not sql_template:
        return _render_conditional_default(
            target_config, when_operator, database_name, schema_name, table_name
        )
    target_replacements = {
        "{target.column}": _safe_identifier(target_config["column"]),
        "{target.when_column}": _safe_identifier(target_config["when_column"]),
        "{target.when_operator}": when_operator,
        "{target.when_value}": _quote_scalar(target_config["when_value"]),
    }
    return _render_template(
        sql_template, database_name, schema_name, table_name, target_replacements, threshold_config
    )


def render_sql_for_instance(
    definition: dict[str, Any],
    scope: str,
    target_config: dict[str, Any],
    database_name: str,
    schema_name: str,
    table_name: str,
    threshold_config: dict[str, Any] | None = None,
) -> str:
    """Dispatcher implementing docs/rules-architecture.md 5.6 (the
    render_sql_for_rule() -> render_sql_for_instance() table), together with
    the target_config shapes from 4.4/4.5 and the SQL trust chain from 5.5.

    Renders one RULE_DEFINITIONS.sql_template against an instance's scope +
    target_config + threshold_config. Per 5.5, this function only ever
    produces TRUSTED SQL from a system sql_template -- a definition with no
    sql_template (a CUSTOM/new definition) is deliberately not handled here;
    its SQL comes from Claude's draft_sql instead, which must go through SQL
    Generation + Validation (sql_validation_tools.py) before it can become
    generated_sql, never straight from this function.

    Args:
        definition: RULE_DEFINITIONS row (dict) -- only ``sql_template`` is
            read here; category/name/etc. are not used for dispatch (5.6:
            "SQL dispatch is entirely driven by sql_template + scope +
            target_config -- never by category alone").
        scope: One of COLUMN / MULTI_COLUMN / TABLE / CROSS_TABLE /
            CONDITIONAL (4.4). SCHEMA/DATABASE are never valid here.
        target_config: Scope-specific target shape (4.5).
        threshold_config: Optional {params.<key>} substitution source.

    Raises:
        ValueError: no sql_template for a definition on a scope that
            requires one (COLUMN/MULTI_COLUMN/TABLE), or an unknown scope.
    """
    if scope == "COLUMN":
        return _render_column_scope(
            definition, target_config, database_name, schema_name, table_name, threshold_config
        )
    if scope == "MULTI_COLUMN":
        return _render_multi_column_scope(
            definition, target_config, database_name, schema_name, table_name, threshold_config
        )
    if scope == "TABLE":
        return _render_table_scope(
            definition, target_config, database_name, schema_name, table_name, threshold_config
        )
    if scope == "CROSS_TABLE":
        return _render_cross_table_scope(
            definition, target_config, database_name, schema_name, table_name, threshold_config
        )
    if scope == "CONDITIONAL":
        return _render_conditional_scope(
            definition, target_config, database_name, schema_name, table_name, threshold_config
        )
    raise ValueError(f"Unknown scope: {scope!r}")
