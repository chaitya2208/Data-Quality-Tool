"""App storage tools -- insert/update scan, profile, rule, alert, and log
records into the app-owned Snowflake DB (PLAYGROUND_DB.{CORE,PROFILING,RULES,
ALERTS,LOGS}, per infra/snowflake/02-06_*.sql).

Built before the agents themselves: agents need somewhere to write their
outputs, so this layer exists first. All functions go through run_app_query()
(tools/snowflake_connection.py) -- no new connection logic here.

IDs are generated in Python (uuid4) rather than relying on each table's
DEFAULT UUID_STRING(), and returned immediately -- so a caller (e.g. an agent)
has the new row's ID in hand for a follow-up call (store a rule, then later
store its execution result) without a round-trip SELECT to look it up.

VARIANT columns (EVIDENCE, THRESHOLD_CONFIG, TEST_RESULT, TOP_VALUES,
SAMPLE_ROWS, DETAILS, ...) can't bind a raw Python dict/list -- Snowflake
needs PARSE_JSON(<json string>). Every function that touches a VARIANT column
handles the json.dumps(...) + PARSE_JSON(%(...)s) internally so callers just
pass normal dicts/lists. Snowflake also rejects PARSE_JSON(NULL) specifically
inside an INSERT ... VALUES (...) clause ("Invalid expression [PARSE_JSON(null)]
in VALUES clause") when the bound value is None -- confirmed against real
Snowflake. The fix is INSERT ... SELECT ... instead of INSERT ... VALUES (...);
the same PARSE_JSON(NULL) is valid in a SELECT list. Every function with a
VARIANT column uses that form.

NOTE on RULE_FINGERPRINT: this module does not compute it. Hashing
(table + column + rule_type + normalized SQL) for duplicate-rule detection is
rule-engine/dedup logic, not storage logic, and is not yet implemented --
store_recommended_rule() only accepts a fingerprint string and stores it as
given. Build the actual deduplicator (e.g. rule_engine/rule_deduplicator.py)
before relying on this for real dedup.
"""

from __future__ import annotations

import datetime
import decimal
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

from tools.snowflake_connection import run_app_query


def _new_id() -> str:
    return str(uuid.uuid4())


def _json_default(value: Any) -> Any:
    """Fallback encoder for values json.dumps can't natively serialize but
    Snowflake results commonly contain -- e.g. profile_top_values() on a
    TIMESTAMP/DATE column returns raw datetime.datetime values inside its
    result dicts, which are then passed straight into a VARIANT column here.
    """
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _json_or_null(value: Any) -> str | None:
    return json.dumps(value, default=_json_default) if value is not None else None


# ---------------------------------------------------------------------------
# CORE.SCAN_RUNS
# ---------------------------------------------------------------------------


def create_scan_run(
    scan_name: str,
    target_database: str,
    target_schema: str | None = None,
    target_table: str | None = None,
    created_by: str | None = None,
) -> str:
    """Create a scan run in PENDING status. Returns the new scan_id."""
    scan_id = _new_id()
    run_app_query(
        """
        INSERT INTO CORE.SCAN_RUNS
            (SCAN_ID, SCAN_NAME, STATUS, TARGET_DATABASE, TARGET_SCHEMA,
             TARGET_TABLE, CREATED_BY)
        VALUES
            (%(scan_id)s, %(scan_name)s, 'PENDING', %(target_database)s,
             %(target_schema)s, %(target_table)s, %(created_by)s)
        """,
        {
            "scan_id": scan_id,
            "scan_name": scan_name,
            "target_database": target_database,
            "target_schema": target_schema,
            "target_table": target_table,
            "created_by": created_by,
        },
    )
    return scan_id


def update_scan_status(
    scan_id: str,
    status: str | None = None,
    current_step: str | None = None,
    progress_percentage: float | None = None,
    error_message: str | None = None,
    mark_ended: bool = False,
) -> None:
    """Partially update a scan run. Only fields passed (non-None, or
    mark_ended=True) are updated -- a scan is touched many times over its
    life (status -> RUNNING, then current_step/progress_percentage as agents
    progress, then status -> COMPLETED/FAILED), so this must not require
    resending every field on each call.
    """
    sets: list[str] = []
    params: dict[str, Any] = {"scan_id": scan_id}

    if status is not None:
        sets.append("STATUS = %(status)s")
        params["status"] = status
    if current_step is not None:
        sets.append("CURRENT_STEP = %(current_step)s")
        params["current_step"] = current_step
    if progress_percentage is not None:
        sets.append("PROGRESS_PERCENTAGE = %(progress_percentage)s")
        params["progress_percentage"] = progress_percentage
    if error_message is not None:
        sets.append("ERROR_MESSAGE = %(error_message)s")
        params["error_message"] = error_message
    if mark_ended:
        sets.append("ENDED_AT = CURRENT_TIMESTAMP()")

    if not sets:
        return

    run_app_query(
        f"UPDATE CORE.SCAN_RUNS SET {', '.join(sets)} WHERE SCAN_ID = %(scan_id)s",
        params,
    )


def list_scan_runs(
    limit: int = 100,
    database_name: str | None = None,
    schema_name: str | None = None,
) -> list[dict]:
    """Return recent scan runs, newest first."""
    where: list[str] = []
    params: dict[str, Any] = {"limit": limit}
    if database_name is not None:
        where.append("TARGET_DATABASE = %(database_name)s")
        params["database_name"] = database_name
    if schema_name is not None:
        where.append("TARGET_SCHEMA = %(schema_name)s")
        params["schema_name"] = schema_name
    where_clause = f"WHERE {' AND '.join(where)}" if where else ""
    rows = run_app_query(
        f"""
        SELECT SCAN_ID, SCAN_NAME, STATUS, CURRENT_STEP, PROGRESS_PERCENTAGE,
               TARGET_DATABASE, TARGET_SCHEMA, TARGET_TABLE, ERROR_MESSAGE,
               CREATED_BY, STARTED_AT, ENDED_AT
        FROM CORE.SCAN_RUNS
        {where_clause}
        ORDER BY STARTED_AT DESC
        LIMIT %(limit)s
        """,
        params,
    )
    return [
        {
            "scan_id": r["SCAN_ID"],
            "scan_name": r["SCAN_NAME"],
            "status": r["STATUS"],
            "current_step": r["CURRENT_STEP"],
            "progress_percentage": r["PROGRESS_PERCENTAGE"],
            "target_database": r["TARGET_DATABASE"],
            "target_schema": r["TARGET_SCHEMA"],
            "target_table": r["TARGET_TABLE"],
            "error_message": r["ERROR_MESSAGE"],
            "created_by": r["CREATED_BY"],
            "started_at": str(r["STARTED_AT"]) if r["STARTED_AT"] else None,
            "ended_at": str(r["ENDED_AT"]) if r["ENDED_AT"] else None,
        }
        for r in rows
    ]


def get_latest_scan_id(
    target_database: str, target_schema: str, target_table: str | None = None
) -> str | None:
    """Most recently started SCAN_RUN matching this target, or None.

    For the live progress feed: the frontend kicks off a scan (a long,
    synchronous POST -- no job queue exists yet, see
    docs/deferred-and-future-work.md) and needs the new scan's SCAN_ID to
    poll its logs *before* that POST resolves. create_scan_run() already
    returns the SCAN_ID to the route that's mid-flight, but the frontend's
    poller is a separate concurrent request with no other way to learn it,
    so it looks the row up by target instead.

    target_table=None means "any table in this schema" (no TARGET_TABLE
    filter at all), not "only the schema-level umbrella scan" -- a schema
    scan's umbrella SCAN_RUN (TARGET_TABLE IS NULL) never gets per-step
    AGENT_RUN_LOGS entries (only each table's own scan does, via
    graphs/dq_workflow_graph.py's nodes), so filtering to NULL here would
    make schema-scope polling always find a scan with no logs to show.
    Leaving TARGET_TABLE unfiltered instead surfaces whichever per-table
    scan is currently running -- exactly the progress a schema scan's
    sequential per-table loop should show.
    """
    where = ["TARGET_DATABASE = %(target_database)s", "TARGET_SCHEMA = %(target_schema)s"]
    params: dict[str, Any] = {"target_database": target_database, "target_schema": target_schema}
    if target_table is not None:
        where.append("TARGET_TABLE = %(target_table)s")
        params["target_table"] = target_table
    else:
        # Exclude umbrella scans (TARGET_TABLE IS NULL) — they never get
        # AGENT_RUN_LOGS entries, so returning one here would make the
        # progress feed show "waiting to start" until the first per-table
        # scan row appears.
        where.append("TARGET_TABLE IS NOT NULL")

    rows = run_app_query(
        f"""
        SELECT SCAN_ID, TARGET_TABLE
        FROM CORE.SCAN_RUNS
        WHERE {' AND '.join(where)}
        ORDER BY STARTED_AT DESC
        LIMIT 1
        """,
        params,
    )
    if not rows:
        return None, None
    return rows[0]["SCAN_ID"], rows[0]["TARGET_TABLE"]


# ---------------------------------------------------------------------------
# PROFILING.TABLE_PROFILES / COLUMN_PROFILES
# ---------------------------------------------------------------------------


def store_table_profile(
    scan_id: str,
    database_name: str,
    schema_name: str,
    table_name: str,
    row_count: int | None,
    column_count: int | None,
    is_sampled: bool = False,
    sample_size: int | None = None,
) -> str:
    """Insert a table-level profile row. Returns the new profile_id.

    is_sampled/sample_size record whether this profile came from a fixed-
    size SAMPLE (tools/snowflake_profiling_tools.py's sample-first
    profiling for tables at/above _SAMPLE_ROW_THRESHOLD rows) rather than a
    full-table scan -- so a reviewer of TABLE_PROFILES can tell "based on a
    50,000-row sample" apart from an exact full-table result, rather than
    both looking identically precise.
    """
    profile_id = _new_id()
    run_app_query(
        """
        INSERT INTO PROFILING.TABLE_PROFILES
            (PROFILE_ID, SCAN_ID, DATABASE_NAME, SCHEMA_NAME, TABLE_NAME,
             ROW_COUNT, COLUMN_COUNT, IS_SAMPLED, SAMPLE_SIZE)
        VALUES
            (%(profile_id)s, %(scan_id)s, %(database_name)s, %(schema_name)s,
             %(table_name)s, %(row_count)s, %(column_count)s, %(is_sampled)s,
             %(sample_size)s)
        """,
        {
            "profile_id": profile_id,
            "scan_id": scan_id,
            "database_name": database_name,
            "schema_name": schema_name,
            "table_name": table_name,
            "row_count": row_count,
            "column_count": column_count,
            "is_sampled": is_sampled,
            "sample_size": sample_size,
        },
    )
    return profile_id


def store_column_profile(
    scan_id: str,
    database_name: str,
    schema_name: str,
    table_name: str,
    column_name: str,
    data_type: str,
    null_count: int | None = None,
    null_percentage: float | None = None,
    distinct_count: int | None = None,
    min_value: str | None = None,
    max_value: str | None = None,
    top_values: list | dict | None = None,
    is_pii: bool = False,
    pii_type: str | None = None,
    sensitivity_level: str | None = None,
    llm_sharing_policy: str | None = None,
) -> str:
    """Insert a column-level profile row, including PII/sensitivity
    classification -- the persisted floor the masking middleware enforces on
    every LLM call for this column. Returns the new profile_id.
    """
    profile_id = _new_id()
    run_app_query(
        """
        INSERT INTO PROFILING.COLUMN_PROFILES
            (PROFILE_ID, SCAN_ID, DATABASE_NAME, SCHEMA_NAME, TABLE_NAME,
             COLUMN_NAME, DATA_TYPE, NULL_COUNT, NULL_PERCENTAGE,
             DISTINCT_COUNT, MIN_VALUE, MAX_VALUE, TOP_VALUES,
             IS_PII, PII_TYPE, SENSITIVITY_LEVEL, LLM_SHARING_POLICY)
        SELECT
            %(profile_id)s, %(scan_id)s, %(database_name)s, %(schema_name)s,
            %(table_name)s, %(column_name)s, %(data_type)s, %(null_count)s,
            %(null_percentage)s, %(distinct_count)s, %(min_value)s,
            %(max_value)s, PARSE_JSON(%(top_values)s),
            %(is_pii)s, %(pii_type)s, %(sensitivity_level)s,
            %(llm_sharing_policy)s
        """,
        {
            "profile_id": profile_id,
            "scan_id": scan_id,
            "database_name": database_name,
            "schema_name": schema_name,
            "table_name": table_name,
            "column_name": column_name,
            "data_type": data_type,
            "null_count": null_count,
            "null_percentage": null_percentage,
            "distinct_count": distinct_count,
            "min_value": min_value,
            "max_value": max_value,
            "top_values": _json_or_null(top_values),
            "is_pii": is_pii,
            "pii_type": pii_type,
            "sensitivity_level": sensitivity_level,
            "llm_sharing_policy": llm_sharing_policy,
        },
    )
    return profile_id


def list_latest_column_profiles(
    database_name: str, schema_name: str, table_name: str
) -> list[dict[str, Any]]:
    """Most recent profile row per column (QUALIFY ROW_NUMBER, same latest-
    per-key pattern as list_approved_rules()'s execution join) -- for
    PII-policy lookups when masking sample failed rows outside the normal
    profiling flow. agents/rule_execution_agent.py has no column_profiles in
    memory the way the test-execution path does (it starts from
    get_approved_rule(), not a fresh scan), so it needs this instead.

    Returns only what sample-row masking needs: [{column_name,
    llm_sharing_policy}, ...].
    """
    rows = run_app_query(
        """
        SELECT COLUMN_NAME, LLM_SHARING_POLICY
        FROM PROFILING.COLUMN_PROFILES
        WHERE DATABASE_NAME = %(database_name)s AND SCHEMA_NAME = %(schema_name)s
          AND TABLE_NAME = %(table_name)s
        QUALIFY ROW_NUMBER() OVER (PARTITION BY COLUMN_NAME ORDER BY PROFILED_AT DESC) = 1
        """,
        {
            "database_name": database_name,
            "schema_name": schema_name,
            "table_name": table_name,
        },
    )
    return [
        {"column_name": row["COLUMN_NAME"], "llm_sharing_policy": row["LLM_SHARING_POLICY"]}
        for row in rows
    ]


def list_table_profile_history(
    database_name: str, schema_name: str, table_name: str, limit: int = 10
) -> list[dict[str, Any]]:
    """Most recent `limit` TABLE_PROFILES rows for one table, newest first,
    each {row_count, profiled_at}. Feeds skills/volume_skill.py's
    historical-average comparison once enough scans exist. Called before the
    current scan's own profile is stored (store_profile_result() runs after
    run_dq_workflow() in scan_operations.py), so this never includes today's
    own row count.
    """
    rows = run_app_query(
        """
        SELECT ROW_COUNT, PROFILED_AT
        FROM PROFILING.TABLE_PROFILES
        WHERE DATABASE_NAME = %(database_name)s AND SCHEMA_NAME = %(schema_name)s
          AND TABLE_NAME = %(table_name)s
        ORDER BY PROFILED_AT DESC
        LIMIT %(limit)s
        """,
        {
            "database_name": database_name,
            "schema_name": schema_name,
            "table_name": table_name,
            "limit": limit,
        },
    )
    return [{"row_count": row["ROW_COUNT"], "profiled_at": row["PROFILED_AT"]} for row in rows]


# ---------------------------------------------------------------------------
# RULES.RECOMMENDED_INSTANCES / RULE_INSTANCES / REJECTED_INSTANCES / RULE_EXECUTION_HISTORY
# ---------------------------------------------------------------------------


def store_recommended_rule(
    scan_id: str,
    rule_name: str,
    rule_type: str,
    database_name: str,
    schema_name: str,
    table_name: str,
    column_name: str | None,
    description: str,
    reason: str,
    evidence: list | dict | None,
    severity: str,
    confidence: float,
    priority: float,
    threshold_config: dict | None,
    generated_sql: str,
    validation_status: str = "PENDING",
    test_status: str = "PENDING",
    test_result: dict | None = None,
    rule_fingerprint: str | None = None,
    business_explanation: str | None = None,
    business_impact: str | None = None,
    false_positive_risk: str | None = None,
    scope: str | None = None,
    target_config: dict | None = None,
    definition_id: str | None = None,
    is_new_definition: bool | None = None,
    proposed_definition: dict | None = None,
    suggested_group_id: str | None = None,
) -> str:
    """Insert a recommended rule. Returns the new rule_id.

    rule_fingerprint is stored as given -- computing it (hash of
    table+column+rule_type+normalized SQL, for re-scan dedup) is rule-engine
    logic that doesn't exist yet; see module docstring.

    business_explanation/business_impact/false_positive_risk are Claude's
    text-only output from tools/claude_tools.explain_rule_with_claude() (see
    "Add Better Claude Explanations" -- Claude explains, code validates and
    executes, human approves). All three default to None: a rule stored
    before this feature existed, or one whose explanation call failed/was
    skipped, simply has no explanation yet rather than a fabricated one.

    scope/target_config/definition_id/is_new_definition/proposed_definition/
    suggested_group_id are the docs/rules-architecture.md §4.7 instance
    fields -- all optional/None so this function's existing flat-shape
    callers (the old run_rule_recommendation_agent() pipeline) are
    unaffected. TARGET_CONFIG/PROPOSED_DEFINITION are VARIANT columns, same
    PARSE_JSON(%(...)s) treatment as EVIDENCE/THRESHOLD_CONFIG above.
    """
    rule_id = _new_id()
    run_app_query(
        """
        INSERT INTO RULES.RECOMMENDED_INSTANCES
            (RULE_ID, SCAN_ID, RULE_NAME, RULE_TYPE, DATABASE_NAME,
             SCHEMA_NAME, TABLE_NAME, COLUMN_NAME, DESCRIPTION, REASON,
             EVIDENCE, SEVERITY, CONFIDENCE, PRIORITY, THRESHOLD_CONFIG,
             GENERATED_SQL, VALIDATION_STATUS, TEST_STATUS, TEST_RESULT,
             RULE_FINGERPRINT, BUSINESS_EXPLANATION, BUSINESS_IMPACT,
             FALSE_POSITIVE_RISK, SCOPE, TARGET_CONFIG, DEFINITION_ID,
             IS_NEW_DEFINITION, PROPOSED_DEFINITION, SUGGESTED_GROUP_ID)
        SELECT
            %(rule_id)s, %(scan_id)s, %(rule_name)s, %(rule_type)s,
            %(database_name)s, %(schema_name)s, %(table_name)s,
            %(column_name)s, %(description)s, %(reason)s,
            PARSE_JSON(%(evidence)s), %(severity)s, %(confidence)s,
            %(priority)s, PARSE_JSON(%(threshold_config)s), %(generated_sql)s,
            %(validation_status)s, %(test_status)s, PARSE_JSON(%(test_result)s),
            %(rule_fingerprint)s, %(business_explanation)s, %(business_impact)s,
            %(false_positive_risk)s, %(scope)s, PARSE_JSON(%(target_config)s),
            %(definition_id)s, %(is_new_definition)s,
            PARSE_JSON(%(proposed_definition)s), %(suggested_group_id)s
        """,
        {
            "rule_id": rule_id,
            "scan_id": scan_id,
            "rule_name": rule_name,
            "rule_type": rule_type,
            "database_name": database_name,
            "schema_name": schema_name,
            "table_name": table_name,
            "column_name": column_name,
            "description": description,
            "reason": reason,
            "evidence": _json_or_null(evidence),
            "severity": severity,
            "confidence": confidence,
            "priority": priority,
            "threshold_config": _json_or_null(threshold_config),
            "generated_sql": generated_sql,
            "validation_status": validation_status,
            "test_status": test_status,
            "test_result": _json_or_null(test_result),
            "rule_fingerprint": rule_fingerprint,
            "business_explanation": business_explanation,
            "business_impact": business_impact,
            "false_positive_risk": false_positive_risk,
            "scope": scope,
            "target_config": _json_or_null(target_config),
            "definition_id": definition_id,
            "is_new_definition": is_new_definition,
            "proposed_definition": _json_or_null(proposed_definition),
            "suggested_group_id": suggested_group_id,
        },
    )
    return rule_id


def _parse_recommended_rule_row(row: dict[str, Any]) -> dict[str, Any]:
    """Reshape one raw RECOMMENDED_INSTANCES row (Snowflake's UPPERCASE column
    names, VARIANT columns returned as JSON strings) into the lowercase
    snake_case dict shape every agent in this codebase already uses
    (rule_name, rule_type, database_name, ...) -- so a caller reading rules
    back out of storage gets the same shape it stored, not a second,
    Snowflake-flavored shape to translate. approval_status is computed here
    (not a RECOMMENDED_INSTANCES column) from the caller-supplied RULE_INSTANCES/
    REJECTED_INSTANCES membership check in list_recommended_rules()/
    get_recommended_rule() -- see their docstrings.
    """
    test_result = json.loads(row["TEST_RESULT"]) if row["TEST_RESULT"] else None
    return {
        "rule_id": row["RULE_ID"],
        "scan_id": row["SCAN_ID"],
        "rule_name": row["RULE_NAME"],
        "rule_type": row["RULE_TYPE"],
        "database_name": row["DATABASE_NAME"],
        "schema_name": row["SCHEMA_NAME"],
        "table_name": row["TABLE_NAME"],
        "column_name": row["COLUMN_NAME"],
        "description": row["DESCRIPTION"],
        "reason": row["REASON"],
        "evidence": json.loads(row["EVIDENCE"]) if row["EVIDENCE"] else None,
        "severity": row["SEVERITY"],
        "confidence": row["CONFIDENCE"],
        "priority": row["PRIORITY"],
        "threshold_config": (
            json.loads(row["THRESHOLD_CONFIG"]) if row["THRESHOLD_CONFIG"] else None
        ),
        "generated_sql": row["GENERATED_SQL"],
        "validation_status": row["VALIDATION_STATUS"],
        "test_status": row["TEST_STATUS"],
        "test_result": test_result,
        # Convenience field for the approval-screen table (the ask's
        # "Failed count" column) -- pulled out of TEST_RESULT rather than a
        # separate stored column, since TEST_RESULT is already the single
        # source of truth for test-run numbers (see
        # agents/rule_test_execution_agent.py).
        "failed_count": test_result.get("failed_count") if test_result else None,
        "rule_fingerprint": row["RULE_FINGERPRINT"],
        "business_explanation": row["BUSINESS_EXPLANATION"],
        "business_impact": row["BUSINESS_IMPACT"],
        "false_positive_risk": row["FALSE_POSITIVE_RISK"],
        "created_at": row["CREATED_AT"],
        "scope": row.get("SCOPE"),
        "target_config": (
            json.loads(row["TARGET_CONFIG"]) if row.get("TARGET_CONFIG") else None
        ),
        "definition_id": row.get("DEFINITION_ID"),
        "is_new_definition": row.get("IS_NEW_DEFINITION"),
        "proposed_definition": (
            json.loads(row["PROPOSED_DEFINITION"]) if row.get("PROPOSED_DEFINITION") else None
        ),
        "suggested_group_id": row.get("SUGGESTED_GROUP_ID"),
    }


def count_pending_rules() -> int:
    """Return the count of PENDING recommended rules (rules not yet approved
    or rejected). Single COUNT(*) — never returns a large result set."""
    rows = run_app_query(
        """
        SELECT COUNT(*) AS CNT
        FROM RULES.RECOMMENDED_INSTANCES r
        WHERE r.RULE_ID NOT IN (SELECT ORIGINAL_RECOMMENDED_RULE_ID FROM RULES.RULE_INSTANCES)
          AND r.RULE_ID NOT IN (SELECT ORIGINAL_RECOMMENDED_RULE_ID FROM RULES.REJECTED_INSTANCES)
        """
    )
    return rows[0]["CNT"] if rows else 0


def list_recommended_rules(
    scan_id: str | None = None, pending_only: bool = False
) -> list[dict[str, Any]]:
    """List recommended rules, newest first, optionally filtered to one scan
    or to only PENDING rules.

    pending_only=True filters to rules not yet approved/rejected -- used by
    the unfiltered (no scan_id) path to avoid returning the full history which
    is large enough to trigger Snowflake S3 result streaming.
    """
    conditions = []
    params: dict[str, Any] = {}
    if scan_id is not None:
        conditions.append("r.SCAN_ID = %(scan_id)s")
        params["scan_id"] = scan_id
    if pending_only:
        conditions.append("a.RULE_ID IS NULL AND j.RULE_ID IS NULL")
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    rows = run_app_query(
        f"""
        SELECT
            r.*,
            CASE
                WHEN a.RULE_ID IS NOT NULL THEN 'APPROVED'
                WHEN j.RULE_ID IS NOT NULL THEN 'REJECTED'
                ELSE 'PENDING'
            END AS APPROVAL_STATUS
        FROM RULES.RECOMMENDED_INSTANCES r
        LEFT JOIN RULES.RULE_INSTANCES a ON a.ORIGINAL_RECOMMENDED_RULE_ID = r.RULE_ID
        LEFT JOIN RULES.REJECTED_INSTANCES j ON j.ORIGINAL_RECOMMENDED_RULE_ID = r.RULE_ID
        {where}
        ORDER BY r.CREATED_AT DESC
        """,
        params,
    )
    parsed = []
    for row in rows:
        rule = _parse_recommended_rule_row(row)
        rule["approval_status"] = row["APPROVAL_STATUS"]
        parsed.append(rule)
    return parsed


def list_recommended_rules_summary(
    pending_only: bool = False,
    scan_id: str | None = None,
) -> list[dict[str, Any]]:
    """Lightweight list of recommended rules. scan_id filters to one scan;
    pending_only filters to unapproved/unrejected rules."""
    conditions: list[str] = []
    params: dict[str, Any] = {}
    if pending_only:
        conditions.append("a.RULE_ID IS NULL AND j.RULE_ID IS NULL")
    if scan_id is not None:
        conditions.append("r.SCAN_ID = %(scan_id)s")
        params["scan_id"] = scan_id
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    rows = run_app_query(
        f"""
        SELECT
            r.RULE_ID,
            r.SCAN_ID,
            r.RULE_NAME,
            r.RULE_TYPE,
            r.DATABASE_NAME,
            r.SCHEMA_NAME,
            r.TABLE_NAME,
            r.COLUMN_NAME,
            r.SEVERITY,
            r.CONFIDENCE,
            r.PRIORITY,
            r.VALIDATION_STATUS,
            r.TEST_STATUS,
            r.RULE_FINGERPRINT,
            r.TEST_RESULT:failed_count::INT AS FAILED_COUNT,
            r.CREATED_AT,
            CASE
                WHEN a.RULE_ID IS NOT NULL THEN 'APPROVED'
                WHEN j.RULE_ID IS NOT NULL THEN 'REJECTED'
                ELSE 'PENDING'
            END AS APPROVAL_STATUS
        FROM RULES.RECOMMENDED_INSTANCES r
        LEFT JOIN RULES.RULE_INSTANCES a ON a.ORIGINAL_RECOMMENDED_RULE_ID = r.RULE_ID
        LEFT JOIN RULES.REJECTED_INSTANCES j ON j.ORIGINAL_RECOMMENDED_RULE_ID = r.RULE_ID
        {where}
        ORDER BY r.CREATED_AT DESC
        """,
        params,
    )
    return [
        {
            "rule_id": row["RULE_ID"],
            "scan_id": row["SCAN_ID"],
            "rule_name": row["RULE_NAME"],
            "rule_type": row["RULE_TYPE"],
            "database_name": row["DATABASE_NAME"],
            "schema_name": row["SCHEMA_NAME"],
            "table_name": row["TABLE_NAME"],
            "column_name": row["COLUMN_NAME"],
            "severity": row["SEVERITY"],
            "confidence": row["CONFIDENCE"],
            "priority": row["PRIORITY"],
            "validation_status": row["VALIDATION_STATUS"],
            "test_status": row["TEST_STATUS"],
            "rule_fingerprint": row["RULE_FINGERPRINT"],
            "failed_count": row["FAILED_COUNT"],
            "created_at": row["CREATED_AT"],
            "approval_status": row["APPROVAL_STATUS"],
        }
        for row in rows
    ]


def get_recommended_rule(rule_id: str) -> dict[str, Any] | None:
    """Fetch one recommended rule by id, with the same computed
    approval_status as list_recommended_rules(). Returns None if not found.
    """
    rows = run_app_query(
        """
        SELECT
            r.*,
            CASE
                WHEN a.RULE_ID IS NOT NULL THEN 'APPROVED'
                WHEN j.RULE_ID IS NOT NULL THEN 'REJECTED'
                ELSE 'PENDING'
            END AS APPROVAL_STATUS
        FROM RULES.RECOMMENDED_INSTANCES r
        LEFT JOIN RULES.RULE_INSTANCES a ON a.ORIGINAL_RECOMMENDED_RULE_ID = r.RULE_ID
        LEFT JOIN RULES.REJECTED_INSTANCES j ON j.ORIGINAL_RECOMMENDED_RULE_ID = r.RULE_ID
        WHERE r.RULE_ID = %(rule_id)s
        """,
        {"rule_id": rule_id},
    )
    if not rows:
        return None
    rule = _parse_recommended_rule_row(rows[0])
    rule["approval_status"] = rows[0]["APPROVAL_STATUS"]
    return rule


def delete_pending_rules_for_table(
    database_name: str, schema_name: str, table_name: str
) -> int:
    """Delete all PENDING recommended rules for a table before a re-scan.

    Only deletes rules that have never been approved or rejected -- rules
    that a human already acted on stay untouched. Returns the count deleted.
    """
    rows = run_app_query(
        """
        DELETE FROM RULES.RECOMMENDED_INSTANCES
        WHERE DATABASE_NAME = %(database_name)s
          AND SCHEMA_NAME   = %(schema_name)s
          AND TABLE_NAME    = %(table_name)s
          AND RULE_ID NOT IN (
              SELECT ORIGINAL_RECOMMENDED_RULE_ID FROM RULES.RULE_INSTANCES
          )
          AND RULE_ID NOT IN (
              SELECT ORIGINAL_RECOMMENDED_RULE_ID FROM RULES.REJECTED_INSTANCES
          )
        """,
        {
            "database_name": database_name,
            "schema_name": schema_name,
            "table_name": table_name,
        },
    )
    return len(rows)


def get_pending_rule_fingerprints(
    database_name: str, schema_name: str, table_name: str
) -> dict[tuple[str, str | None, str], str]:
    """Return a mapping of (table_name, column_name, rule_type) → rule_id for
    all PENDING recommended rules on this table. Used by scan_operations to
    re-associate (refresh) an existing pending rule with the latest scan rather
    than inserting a duplicate."""
    rows = run_app_query(
        """
        SELECT r.RULE_ID, r.TABLE_NAME, r.COLUMN_NAME, r.RULE_TYPE
        FROM RULES.RECOMMENDED_INSTANCES r
        WHERE r.DATABASE_NAME = %(database_name)s
          AND r.SCHEMA_NAME   = %(schema_name)s
          AND r.TABLE_NAME    = %(table_name)s
          AND r.RULE_ID NOT IN (SELECT ORIGINAL_RECOMMENDED_RULE_ID FROM RULES.RULE_INSTANCES)
          AND r.RULE_ID NOT IN (SELECT ORIGINAL_RECOMMENDED_RULE_ID FROM RULES.REJECTED_INSTANCES)
        """,
        {"database_name": database_name, "schema_name": schema_name, "table_name": table_name},
    )
    return {(row["TABLE_NAME"], row["COLUMN_NAME"], row["RULE_TYPE"]): row["RULE_ID"] for row in rows}


def refresh_pending_rule(
    rule_id: str,
    scan_id: str,
    description: str,
    reason: str,
    evidence: list | dict | None,
    severity: str,
    confidence: float,
    priority: float,
    threshold_config: dict | None,
    generated_sql: str,
    validation_status: str,
    test_status: str,
    test_result: dict | None,
    rule_fingerprint: str | None,
    business_explanation: str | None,
    business_impact: str | None,
    false_positive_risk: str | None,
    scope: str | None = None,
    target_config: dict | None = None,
    definition_id: str | None = None,
    is_new_definition: bool | None = None,
    proposed_definition: dict | None = None,
    suggested_group_id: str | None = None,
) -> None:
    """Update an existing PENDING recommended rule with fresh results from a
    new scan. Re-associates it with the new scan_id and overwrites all
    analysis fields so the approval queue always reflects the latest run.
    The rule_id (and therefore any UI state, e.g. expanded row) is preserved.

    scope/target_config/definition_id/is_new_definition/proposed_definition/
    suggested_group_id are always overwritten on refresh, same "this scan's
    latest analysis wins" treatment every other field in this function
    already gets -- unlike store_recommended_rule()'s optional/None
    treatment (an insert with nothing yet to say), a refresh call always
    comes from run_instance_recommendation_agent() with real values for
    these fields.
    """
    run_app_query(
        """
        UPDATE RULES.RECOMMENDED_INSTANCES SET
            SCAN_ID              = %(scan_id)s,
            DESCRIPTION          = %(description)s,
            REASON               = %(reason)s,
            EVIDENCE             = PARSE_JSON(%(evidence)s),
            SEVERITY             = %(severity)s,
            CONFIDENCE           = %(confidence)s,
            PRIORITY             = %(priority)s,
            THRESHOLD_CONFIG     = PARSE_JSON(%(threshold_config)s),
            GENERATED_SQL        = %(generated_sql)s,
            VALIDATION_STATUS    = %(validation_status)s,
            TEST_STATUS          = %(test_status)s,
            TEST_RESULT          = PARSE_JSON(%(test_result)s),
            RULE_FINGERPRINT     = %(rule_fingerprint)s,
            BUSINESS_EXPLANATION = %(business_explanation)s,
            BUSINESS_IMPACT      = %(business_impact)s,
            FALSE_POSITIVE_RISK  = %(false_positive_risk)s,
            SCOPE                = %(scope)s,
            TARGET_CONFIG        = PARSE_JSON(%(target_config)s),
            DEFINITION_ID        = %(definition_id)s,
            IS_NEW_DEFINITION    = %(is_new_definition)s,
            PROPOSED_DEFINITION  = PARSE_JSON(%(proposed_definition)s),
            SUGGESTED_GROUP_ID   = %(suggested_group_id)s,
            CREATED_AT           = CURRENT_TIMESTAMP()
        WHERE RULE_ID = %(rule_id)s
        """,
        {
            "rule_id": rule_id,
            "scan_id": scan_id,
            "description": description,
            "reason": reason,
            "evidence": _json_or_null(evidence),
            "severity": severity,
            "confidence": confidence,
            "priority": priority,
            "threshold_config": _json_or_null(threshold_config),
            "generated_sql": generated_sql,
            "validation_status": validation_status,
            "test_status": test_status,
            "test_result": _json_or_null(test_result),
            "rule_fingerprint": rule_fingerprint,
            "business_explanation": business_explanation,
            "business_impact": business_impact,
            "false_positive_risk": false_positive_risk,
            "scope": scope,
            "target_config": _json_or_null(target_config),
            "definition_id": definition_id,
            "is_new_definition": is_new_definition,
            "proposed_definition": _json_or_null(proposed_definition),
            "suggested_group_id": suggested_group_id,
        },
    )


def update_recommended_rule(
    rule_id: str,
    severity: str | None = None,
    threshold_config: dict | None = None,
    generated_sql: str | None = None,
    validation_status: str | None = None,
    validation_errors: list[str] | None = None,
) -> None:
    """Partially update a recommended rule -- the Edit action on the
    approval screen. Only fields passed (non-None) are updated, same
    partial-update pattern as update_scan_status() above.

    validation_status/validation_errors are set here because the ask's Edit
    behavior is "update threshold/severity/SQL, then revalidate" -- the
    caller (the /edit route) re-runs tools/sql_validation_tools.validate_sql()
    on the new generated_sql and passes the fresh result straight through
    to this same call, rather than a second round-trip.

    validation_errors has no column on RECOMMENDED_INSTANCES (see
    04_create_rule_tables.sql) -- accepted as a parameter but not persisted;
    the /edit route returns it directly in its response instead. Kept as a
    parameter (not just dropped) so this function's signature documents that
    the caller has it available, even though this layer has nowhere to put
    it long-term (same kind of intentionally-not-yet-modeled gap as
    RULE_FINGERPRINT, see module docstring).
    """
    sets: list[str] = []
    params: dict[str, Any] = {"rule_id": rule_id}

    if severity is not None:
        sets.append("SEVERITY = %(severity)s")
        params["severity"] = severity
    if threshold_config is not None:
        sets.append("THRESHOLD_CONFIG = PARSE_JSON(%(threshold_config)s)")
        params["threshold_config"] = _json_or_null(threshold_config)
    if generated_sql is not None:
        sets.append("GENERATED_SQL = %(generated_sql)s")
        params["generated_sql"] = generated_sql
    if validation_status is not None:
        sets.append("VALIDATION_STATUS = %(validation_status)s")
        params["validation_status"] = validation_status

    if not sets:
        return

    run_app_query(
        f"UPDATE RULES.RECOMMENDED_INSTANCES SET {', '.join(sets)} WHERE RULE_ID = %(rule_id)s",
        params,
    )


def store_approved_rule(
    original_recommended_rule_id: str,
    rule_name: str,
    rule_type: str,
    database_name: str,
    schema_name: str,
    table_name: str,
    column_name: str | None,
    severity: str,
    threshold_config: dict | None,
    rule_sql: str,
    approved_by: str | None = None,
    is_active: bool = True,
    schedule_config: dict | None = None,
    scope: str | None = None,
    target_config: dict | None = None,
    definition_id: str | None = None,
    group_id: str | None = None,
) -> str:
    """Insert an approved (active) rule. Returns the new rule_id.

    scope/target_config/definition_id/group_id are the §4.8 instance fields
    -- optional/None so existing (pre-redesign) callers are unaffected.
    TARGET_CONFIG is VARIANT, same PARSE_JSON(%(...)s) treatment as
    THRESHOLD_CONFIG/SCHEDULE_CONFIG above.
    """
    rule_id = _new_id()
    run_app_query(
        """
        INSERT INTO RULES.RULE_INSTANCES
            (RULE_ID, ORIGINAL_RECOMMENDED_RULE_ID, RULE_NAME, RULE_TYPE,
             DATABASE_NAME, SCHEMA_NAME, TABLE_NAME, COLUMN_NAME, SEVERITY,
             THRESHOLD_CONFIG, RULE_SQL, IS_ACTIVE, SCHEDULE_CONFIG, APPROVED_BY,
             SCOPE, TARGET_CONFIG, DEFINITION_ID, GROUP_ID)
        SELECT
            %(rule_id)s, %(original_recommended_rule_id)s, %(rule_name)s,
            %(rule_type)s, %(database_name)s, %(schema_name)s, %(table_name)s,
            %(column_name)s, %(severity)s, PARSE_JSON(%(threshold_config)s),
            %(rule_sql)s, %(is_active)s, PARSE_JSON(%(schedule_config)s),
            %(approved_by)s, %(scope)s, PARSE_JSON(%(target_config)s),
            %(definition_id)s, %(group_id)s
        """,
        {
            "rule_id": rule_id,
            "original_recommended_rule_id": original_recommended_rule_id,
            "rule_name": rule_name,
            "rule_type": rule_type,
            "database_name": database_name,
            "schema_name": schema_name,
            "table_name": table_name,
            "column_name": column_name,
            "severity": severity,
            "threshold_config": _json_or_null(threshold_config),
            "rule_sql": rule_sql,
            "is_active": is_active,
            "schedule_config": _json_or_null(schedule_config),
            "approved_by": approved_by,
            "scope": scope,
            "target_config": _json_or_null(target_config),
            "definition_id": definition_id,
            "group_id": group_id,
        },
    )
    return rule_id


def _parse_approved_rule_row(row: dict[str, Any]) -> dict[str, Any]:
    """Reshape one raw RULE_INSTANCES row into the same lowercase
    snake_case dict shape _parse_recommended_rule_row() produces for
    RECOMMENDED_INSTANCES -- so callers get a consistent shape across both
    tables. Shared by get_approved_rule() and list_approved_rules().
    """
    return {
        "rule_id": row["RULE_ID"],
        "original_recommended_rule_id": row["ORIGINAL_RECOMMENDED_RULE_ID"],
        "rule_name": row["RULE_NAME"],
        "rule_type": row["RULE_TYPE"],
        "database_name": row["DATABASE_NAME"],
        "schema_name": row["SCHEMA_NAME"],
        "table_name": row["TABLE_NAME"],
        "column_name": row["COLUMN_NAME"],
        "severity": row["SEVERITY"],
        "threshold_config": (
            json.loads(row["THRESHOLD_CONFIG"]) if row["THRESHOLD_CONFIG"] else None
        ),
        "rule_sql": row["RULE_SQL"],
        "is_active": row["IS_ACTIVE"],
        "schedule_config": (
            json.loads(row["SCHEDULE_CONFIG"]) if row["SCHEDULE_CONFIG"] else None
        ),
        "approved_at": row["APPROVED_AT"],
        "approved_by": row["APPROVED_BY"],
        "scope": row.get("SCOPE"),
        "target_config": (
            json.loads(row["TARGET_CONFIG"]) if row.get("TARGET_CONFIG") else None
        ),
        "definition_id": row.get("DEFINITION_ID"),
        "group_id": row.get("GROUP_ID"),
    }


def get_approved_rule(rule_id: str) -> dict[str, Any] | None:
    """Fetch one approved rule by id. Returns None if not found."""
    rows = run_app_query(
        "SELECT * FROM RULES.RULE_INSTANCES WHERE RULE_ID = %(rule_id)s",
        {"rule_id": rule_id},
    )
    if not rows:
        return None
    return _parse_approved_rule_row(rows[0])


def list_approved_rules() -> list[dict[str, Any]]:
    """List every approved rule, newest first, each annotated with its most
    recent execution (last_run_status/last_run_failed_count/
    last_run_total_count/last_run_failure_percentage/last_run_at) --
    exactly what the Active Rules Page's "Last run status" column needs,
    without a second round-trip per rule.

    The most-recent-execution-per-rule join uses QUALIFY ROW_NUMBER() (a
    Snowflake extension) rather than a correlated subquery or GROUP BY +
    MAX(STARTED_AT) -- picks exactly one row per approved RULE_ID, ties
    broken arbitrarily (STARTED_AT collisions are not expected in practice:
    manual runs, one at a time). Partitioned by a.RULE_ID (always
    non-null), not h.RULE_ID (NULL for every rule with no execution
    history, which would otherwise bucket all never-run rules into one
    partition) -- a rule with no executions yet gets exactly one row, with
    NULLs for every last_run_* field via the LEFT JOIN, surfaced by the UI
    as "Never run" rather than a missing key or a dropped row.
    """
    rows = run_app_query(
        """
        SELECT
            a.*,
            h.STATUS AS LAST_RUN_STATUS,
            h.FAILED_COUNT AS LAST_RUN_FAILED_COUNT,
            h.TOTAL_COUNT AS LAST_RUN_TOTAL_COUNT,
            h.FAILURE_PERCENTAGE AS LAST_RUN_FAILURE_PERCENTAGE,
            h.STARTED_AT AS LAST_RUN_AT
        FROM RULES.RULE_INSTANCES a
        LEFT JOIN RULES.RULE_EXECUTION_HISTORY h ON h.RULE_ID = a.RULE_ID
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY a.RULE_ID ORDER BY h.STARTED_AT DESC
        ) = 1
        ORDER BY a.APPROVED_AT DESC
        """
    )
    parsed = []
    for row in rows:
        rule = _parse_approved_rule_row(row)
        rule["last_run_status"] = row["LAST_RUN_STATUS"]
        rule["last_run_failed_count"] = row["LAST_RUN_FAILED_COUNT"]
        rule["last_run_total_count"] = row["LAST_RUN_TOTAL_COUNT"]
        rule["last_run_failure_percentage"] = row["LAST_RUN_FAILURE_PERCENTAGE"]
        rule["last_run_at"] = row["LAST_RUN_AT"]
        parsed.append(rule)
    return parsed


def store_rejected_rule(
    original_recommended_rule_id: str,
    rejection_reason: str | None = None,
    rejected_by: str | None = None,
) -> str:
    """Insert a rejected-rule record. Returns the new rule_id.

    rejection_reason may be blank -- the user can leave it empty per the
    approval workflow (still useful signal: rejected without explanation).
    """
    rule_id = _new_id()
    run_app_query(
        """
        INSERT INTO RULES.REJECTED_INSTANCES
            (RULE_ID, ORIGINAL_RECOMMENDED_RULE_ID, REJECTION_REASON, REJECTED_BY)
        VALUES
            (%(rule_id)s, %(original_recommended_rule_id)s, %(rejection_reason)s,
             %(rejected_by)s)
        """,
        {
            "rule_id": rule_id,
            "original_recommended_rule_id": original_recommended_rule_id,
            "rejection_reason": rejection_reason,
            "rejected_by": rejected_by,
        },
    )
    return rule_id


def store_user_feedback(
    feedback_type: str,
    rule_type: str,
    database_name: str,
    schema_name: str,
    table_name: str,
    column_name: str | None,
    rule_id: str | None = None,
    alert_id: str | None = None,
    comment: str | None = None,
    threshold_config: dict | None = None,
    created_by: str | None = None,
) -> str:
    """Insert one feedback record. Returns the new feedback_id.

    Feedback Loop (per the ask): a rejection, a false-positive mark, or a
    threshold edit are all signals a future recommendation should check
    before proposing the "same" rule again. This table
    (`RULES.USER_FEEDBACK`) existed since early in this project but sat
    completely unused (see docs/deferred-and-future-work.md #8) -- this is
    the first thing that ever writes to it.

    feedback_type is one of REJECT / EDIT / FALSE_POSITIVE (matching
    04_create_rule_tables.sql's own comment, which also lists APPROVE --
    not written here since nothing in this ask needs to look up "was this
    approved before," only the three negative/corrective signals).
    rule_type/database_name/schema_name/table_name/column_name are the
    lookup key get_feedback_for_table() matches against -- denormalized
    onto this row (not just rule_id/alert_id) specifically so a *future*
    recommendation for the same table/column can find this feedback
    without needing the original (possibly long-gone, since
    RECOMMENDED_INSTANCES rows are never deleted but the rule itself may not
    exist in a later scan's candidate set) rule/alert row.
    """
    feedback_id = _new_id()
    run_app_query(
        """
        INSERT INTO RULES.USER_FEEDBACK
            (FEEDBACK_ID, RULE_ID, ALERT_ID, FEEDBACK_TYPE, COMMENT,
             RULE_TYPE, DATABASE_NAME, SCHEMA_NAME, TABLE_NAME, COLUMN_NAME,
             THRESHOLD_CONFIG, CREATED_BY)
        SELECT
            %(feedback_id)s, %(rule_id)s, %(alert_id)s, %(feedback_type)s,
            %(comment)s, %(rule_type)s, %(database_name)s, %(schema_name)s,
            %(table_name)s, %(column_name)s, PARSE_JSON(%(threshold_config)s),
            %(created_by)s
        """,
        {
            "feedback_id": feedback_id,
            "rule_id": rule_id,
            "alert_id": alert_id,
            "feedback_type": feedback_type,
            "comment": comment,
            "rule_type": rule_type,
            "database_name": database_name,
            "schema_name": schema_name,
            "table_name": table_name,
            "column_name": column_name,
            "threshold_config": _json_or_null(threshold_config),
            "created_by": created_by,
        },
    )
    return feedback_id


def get_feedback_for_table(
    database_name: str, schema_name: str, table_name: str
) -> list[dict[str, Any]]:
    """All feedback recorded for one table, newest first -- what
    agents/rule_recommendation_agent.py checks before proposing rules for
    that table again (see that module's _apply_feedback()).

    Not filtered by column_name here (the caller matches per-candidate,
    since one call covers every column's feedback for the table in one
    round-trip rather than one query per candidate rule).
    """
    rows = run_app_query(
        """
        SELECT RULE_TYPE, COLUMN_NAME, FEEDBACK_TYPE, COMMENT,
               THRESHOLD_CONFIG, CREATED_AT
        FROM RULES.USER_FEEDBACK
        WHERE DATABASE_NAME = %(database_name)s
          AND SCHEMA_NAME = %(schema_name)s
          AND TABLE_NAME = %(table_name)s
        ORDER BY CREATED_AT DESC
        """,
        {
            "database_name": database_name,
            "schema_name": schema_name,
            "table_name": table_name,
        },
    )
    return [
        {
            "rule_type": row["RULE_TYPE"],
            "column_name": row["COLUMN_NAME"],
            "feedback_type": row["FEEDBACK_TYPE"],
            "comment": row["COMMENT"],
            "threshold_config": (
                json.loads(row["THRESHOLD_CONFIG"]) if row["THRESHOLD_CONFIG"] else None
            ),
            "created_at": row["CREATED_AT"],
        }
        for row in rows
    ]


def store_execution_result(
    rule_id: str,
    status: str,
    failed_count: int | None = None,
    total_count: int | None = None,
    failure_percentage: float | None = None,
    error_message: str | None = None,
    mark_ended: bool = True,
    instance_id: str | None = None,
) -> str:
    """Insert a rule execution result. Returns the new execution_id.

    instance_id is optional and additive (§7: RULE_EXECUTION_HISTORY gains
    INSTANCE_ID alongside the retained RULE_ID) -- omitted, it stays NULL and
    every existing caller's behavior is unchanged.
    """
    execution_id = _new_id()
    run_app_query(
        f"""
        INSERT INTO RULES.RULE_EXECUTION_HISTORY
            (EXECUTION_ID, RULE_ID, STATUS, FAILED_COUNT, TOTAL_COUNT,
             FAILURE_PERCENTAGE, ERROR_MESSAGE, INSTANCE_ID
             {", ENDED_AT" if mark_ended else ""})
        VALUES
            (%(execution_id)s, %(rule_id)s, %(status)s, %(failed_count)s,
             %(total_count)s, %(failure_percentage)s, %(error_message)s,
             %(instance_id)s
             {", CURRENT_TIMESTAMP()" if mark_ended else ""})
        """,
        {
            "execution_id": execution_id,
            "rule_id": rule_id,
            "status": status,
            "failed_count": failed_count,
            "total_count": total_count,
            "failure_percentage": failure_percentage,
            "error_message": error_message,
            "instance_id": instance_id,
        },
    )
    return execution_id


def _parse_execution_history_row(row: dict[str, Any]) -> dict[str, Any]:
    """Reshape one raw RULE_EXECUTION_HISTORY row (joined with
    RULE_INSTANCES for rule_name/database/schema/table/column --
    RULE_EXECUTION_HISTORY itself only stores RULE_ID, same join pattern as
    _parse_alert_row()) into a lowercase snake_case dict.

    duration_seconds is computed here (DATEDIFF in the query, see
    list_execution_history()) rather than left for the frontend to
    subtract two timestamp strings itself; None for a row with no
    ENDED_AT (SKIPPED runs still set ENDED_AT via store_execution_result()'s
    default, so in practice this is only None if a future caller ever
    passes mark_ended=False).
    """
    return {
        "execution_id": row["EXECUTION_ID"],
        "rule_id": row["RULE_ID"],
        "rule_name": row["RULE_NAME"],
        "database_name": row["DATABASE_NAME"],
        "schema_name": row["SCHEMA_NAME"],
        "table_name": row["TABLE_NAME"],
        "column_name": row["COLUMN_NAME"],
        "status": row["STATUS"],
        "failed_count": row["FAILED_COUNT"],
        "total_count": row["TOTAL_COUNT"],
        "failure_percentage": row["FAILURE_PERCENTAGE"],
        "started_at": row["STARTED_AT"],
        "ended_at": row["ENDED_AT"],
        "duration_seconds": row["DURATION_SECONDS"],
        "error_message": row["ERROR_MESSAGE"],
    }


def list_execution_history(
    status: str | None = None,
    rule_id: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """List rule runs, newest first. limit caps the result set to avoid
    Snowflake S3 streaming when the caller only needs a dashboard feed."""
    where: list[str] = []
    params: dict[str, Any] = {}
    if status is not None:
        where.append("h.STATUS = %(status)s")
        params["status"] = status
    if rule_id is not None:
        where.append("h.RULE_ID = %(rule_id)s")
        params["rule_id"] = rule_id
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    limit_sql = "LIMIT %(limit)s" if limit is not None else ""
    if limit is not None:
        params["limit"] = limit

    rows = run_app_query(
        f"""
        SELECT
            h.EXECUTION_ID, h.RULE_ID, h.STATUS, h.FAILED_COUNT, h.TOTAL_COUNT,
            h.FAILURE_PERCENTAGE, h.STARTED_AT, h.ENDED_AT, h.ERROR_MESSAGE,
            ar.RULE_NAME, ar.DATABASE_NAME, ar.SCHEMA_NAME, ar.TABLE_NAME,
            ar.COLUMN_NAME,
            DATEDIFF('second', h.STARTED_AT, h.ENDED_AT) AS DURATION_SECONDS
        FROM RULES.RULE_EXECUTION_HISTORY h
        LEFT JOIN RULES.RULE_INSTANCES ar ON ar.RULE_ID = h.RULE_ID
        {where_sql}
        ORDER BY h.STARTED_AT DESC
        {limit_sql}
        """,
        params,
    )
    return [_parse_execution_history_row(row) for row in rows]


def list_table_health() -> list[dict[str, Any]]:
    """Per-table DQ health summary for the Table Health Page: one row per
    table that has at least one approved rule (a table with no approved
    rules isn't being monitored yet, so it has no health to show -- same
    "recommended, not yet executable" exclusion this codebase applies
    elsewhere, not a query bug).

    For each table:
        total_active_rules -- COUNT of its RULE_INSTANCES with IS_ACTIVE.
        passed_rules / failed_rules -- COUNT of active rules whose most
            recent execution (same QUALIFY ROW_NUMBER() "latest run per
            rule" pattern as list_approved_rules()) is PASSED / FAILED.
            A rule that has never run, or whose latest run was ERROR/
            SKIPPED, counts toward total_active_rules but not passed or
            failed -- it hasn't told us pass/fail yet, so it can't count
            as either (same reasoning alert_agent.py uses to not alert on
            ERROR/SKIPPED).
        open_alerts -- COUNT of this table's OPEN alerts (join through
            RULE_INSTANCES, since ALERTS only stores RULE_ID, same pattern
            as list_alerts()/get_alerts_summary()).
        last_scan_at -- MAX(PROFILED_AT) across this table's
            TABLE_PROFILES rows, i.e. the most recent time this table was
            profiled (a scan), not the most recent rule *execution*
            ("scan" and "rule run" are different actions in this
            codebase -- scanning produces profiles/recommendations,
            running is per-rule and post-approval).

    dq_score is computed in Python, not SQL, as
    round(passed_rules / total_active_rules * 100, 1) -- per the ask's own
    formula (passed / total, not passed / (passed + failed)), so a table
    with never-run rules scores lower until those rules actually run once,
    same "unrun means not proven good" spirit as the rest of this
    pipeline. None (not 0) when total_active_rules is 0 -- shouldn't
    happen given the base table already requires >=1 active rule via
    COUNT_IF, but guards the division regardless.
    """
    rows = run_app_query(
        """
        WITH latest_execution AS (
            SELECT
                a.RULE_ID, a.DATABASE_NAME, a.SCHEMA_NAME, a.TABLE_NAME,
                a.IS_ACTIVE, h.STATUS AS LAST_STATUS
            FROM RULES.RULE_INSTANCES a
            LEFT JOIN RULES.RULE_EXECUTION_HISTORY h ON h.RULE_ID = a.RULE_ID
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY a.RULE_ID ORDER BY h.STARTED_AT DESC
            ) = 1
        ),
        rule_agg AS (
            SELECT
                DATABASE_NAME, SCHEMA_NAME, TABLE_NAME,
                COUNT_IF(IS_ACTIVE) AS TOTAL_ACTIVE_RULES,
                COUNT_IF(IS_ACTIVE AND LAST_STATUS = 'PASSED') AS PASSED_RULES,
                COUNT_IF(IS_ACTIVE AND LAST_STATUS = 'FAILED') AS FAILED_RULES
            FROM latest_execution
            GROUP BY DATABASE_NAME, SCHEMA_NAME, TABLE_NAME
        ),
        alert_agg AS (
            SELECT
                ar.DATABASE_NAME, ar.SCHEMA_NAME, ar.TABLE_NAME,
                COUNT_IF(al.STATUS = 'OPEN') AS OPEN_ALERTS
            FROM ALERTS.ALERTS al
            JOIN RULES.RULE_INSTANCES ar ON ar.RULE_ID = al.RULE_ID
            GROUP BY ar.DATABASE_NAME, ar.SCHEMA_NAME, ar.TABLE_NAME
        ),
        scan_agg AS (
            SELECT DATABASE_NAME, SCHEMA_NAME, TABLE_NAME,
                   MAX(PROFILED_AT) AS LAST_SCAN_AT
            FROM PROFILING.TABLE_PROFILES
            GROUP BY DATABASE_NAME, SCHEMA_NAME, TABLE_NAME
        )
        SELECT
            r.DATABASE_NAME, r.SCHEMA_NAME, r.TABLE_NAME,
            r.TOTAL_ACTIVE_RULES, r.PASSED_RULES, r.FAILED_RULES,
            COALESCE(al.OPEN_ALERTS, 0) AS OPEN_ALERTS,
            s.LAST_SCAN_AT
        FROM rule_agg r
        LEFT JOIN alert_agg al
            ON al.DATABASE_NAME = r.DATABASE_NAME
           AND al.SCHEMA_NAME = r.SCHEMA_NAME
           AND al.TABLE_NAME = r.TABLE_NAME
        LEFT JOIN scan_agg s
            ON s.DATABASE_NAME = r.DATABASE_NAME
           AND s.SCHEMA_NAME = r.SCHEMA_NAME
           AND s.TABLE_NAME = r.TABLE_NAME
        ORDER BY r.DATABASE_NAME, r.SCHEMA_NAME, r.TABLE_NAME
        """
    )
    result = []
    for row in rows:
        total = row["TOTAL_ACTIVE_RULES"]
        passed = row["PASSED_RULES"]
        dq_score = round(passed / total * 100, 1) if total else None
        result.append(
            {
                "database_name": row["DATABASE_NAME"],
                "schema_name": row["SCHEMA_NAME"],
                "table_name": row["TABLE_NAME"],
                "total_active_rules": total,
                "passed_rules": passed,
                "failed_rules": row["FAILED_RULES"],
                "open_alerts": row["OPEN_ALERTS"],
                "dq_score": dq_score,
                "last_scan_at": row["LAST_SCAN_AT"],
            }
        )
    return result


# ---------------------------------------------------------------------------
# ALERTS.ALERTS / ALERT_VIOLATION_SAMPLES
# ---------------------------------------------------------------------------


def store_alert(
    rule_id: str,
    execution_id: str,
    title: str,
    description: str,
    severity: str,
    failed_count: int | None = None,
    failure_percentage: float | None = None,
    business_explanation: str | None = None,
    business_impact: str | None = None,
    false_positive_risk: str | None = None,
    instance_id: str | None = None,
) -> str:
    """Insert a new alert with STATUS = OPEN. Returns the new alert_id.

    business_explanation/business_impact/false_positive_risk are Claude's
    text-only output from tools/claude_tools.explain_alert_with_claude()
    (see "Add Better Claude Explanations"). All default to None: an alert
    created before this feature existed, or whose explanation call
    failed/was skipped, has no explanation yet -- the alert itself (title/
    description/severity/status) is unaffected either way, since those are
    all set deterministically by agents/alert_agent.py, not by Claude.

    instance_id is optional and additive (§7: ALERTS gains INSTANCE_ID
    alongside the retained RULE_ID) -- omitted, it stays NULL and every
    existing caller's behavior is unchanged.
    """
    alert_id = _new_id()
    run_app_query(
        """
        INSERT INTO ALERTS.ALERTS
            (ALERT_ID, RULE_ID, EXECUTION_ID, TITLE, DESCRIPTION, SEVERITY,
             STATUS, FAILED_COUNT, FAILURE_PERCENTAGE, BUSINESS_EXPLANATION,
             BUSINESS_IMPACT, FALSE_POSITIVE_RISK, INSTANCE_ID)
        VALUES
            (%(alert_id)s, %(rule_id)s, %(execution_id)s, %(title)s,
             %(description)s, %(severity)s, 'OPEN', %(failed_count)s,
             %(failure_percentage)s, %(business_explanation)s,
             %(business_impact)s, %(false_positive_risk)s, %(instance_id)s)
        """,
        {
            "alert_id": alert_id,
            "rule_id": rule_id,
            "execution_id": execution_id,
            "title": title,
            "description": description,
            "severity": severity,
            "failed_count": failed_count,
            "failure_percentage": failure_percentage,
            "business_explanation": business_explanation,
            "business_impact": business_impact,
            "false_positive_risk": false_positive_risk,
            "instance_id": instance_id,
        },
    )
    return alert_id


def _parse_alert_row(row: dict[str, Any]) -> dict[str, Any]:
    """Reshape one raw ALERTS row (joined with RULE_INSTANCES for
    database/schema/table/rule_name -- ALERTS itself carries only RULE_ID,
    no denormalized location fields) into a lowercase snake_case dict, same
    convention as _parse_recommended_rule_row()/_parse_approved_rule_row().
    """
    return {
        "alert_id": row["ALERT_ID"],
        "rule_id": row["RULE_ID"],
        "execution_id": row["EXECUTION_ID"],
        "title": row["TITLE"],
        "description": row["DESCRIPTION"],
        "severity": row["SEVERITY"],
        "status": row["STATUS"],
        "failed_count": row["FAILED_COUNT"],
        "failure_percentage": row["FAILURE_PERCENTAGE"],
        "created_at": row["CREATED_AT"],
        "updated_at": row["UPDATED_AT"],
        "rule_name": row["RULE_NAME"],
        "rule_type": row["RULE_TYPE"],
        "database_name": row["DATABASE_NAME"],
        "schema_name": row["SCHEMA_NAME"],
        "table_name": row["TABLE_NAME"],
        "column_name": row["COLUMN_NAME"],
        "business_explanation": row["BUSINESS_EXPLANATION"],
        "business_impact": row["BUSINESS_IMPACT"],
        "false_positive_risk": row["FALSE_POSITIVE_RISK"],
    }


def list_alerts(
    database_name: str | None = None,
    schema_name: str | None = None,
    table_name: str | None = None,
    severity: str | None = None,
    status: str | None = None,
    date: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """List alerts, newest first. limit caps the result set to avoid
    Snowflake S3 streaming when the caller only needs a dashboard feed."""
    where: list[str] = []
    params: dict[str, Any] = {}

    if database_name is not None:
        where.append("ar.DATABASE_NAME = %(database_name)s")
        params["database_name"] = database_name
    if schema_name is not None:
        where.append("ar.SCHEMA_NAME = %(schema_name)s")
        params["schema_name"] = schema_name
    if table_name is not None:
        where.append("ar.TABLE_NAME = %(table_name)s")
        params["table_name"] = table_name
    if severity is not None:
        where.append("al.SEVERITY = %(severity)s")
        params["severity"] = severity
    if status is not None:
        where.append("al.STATUS = %(status)s")
        params["status"] = status
    if date is not None:
        where.append("TO_DATE(al.CREATED_AT) = %(date)s")
        params["date"] = date

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    limit_sql = "LIMIT %(limit)s" if limit is not None else ""
    if limit is not None:
        params["limit"] = limit
    rows = run_app_query(
        f"""
        SELECT
            al.ALERT_ID, al.RULE_ID, al.EXECUTION_ID, al.TITLE, al.DESCRIPTION,
            al.SEVERITY, al.STATUS, al.FAILED_COUNT, al.FAILURE_PERCENTAGE,
            al.CREATED_AT, al.UPDATED_AT,
            al.BUSINESS_EXPLANATION, al.BUSINESS_IMPACT, al.FALSE_POSITIVE_RISK,
            ar.RULE_NAME, ar.RULE_TYPE, ar.DATABASE_NAME, ar.SCHEMA_NAME,
            ar.TABLE_NAME, ar.COLUMN_NAME
        FROM ALERTS.ALERTS al
        LEFT JOIN RULES.RULE_INSTANCES ar ON ar.RULE_ID = al.RULE_ID
        {where_sql}
        ORDER BY al.CREATED_AT DESC
        {limit_sql}
        """,
        params,
    )
    return [_parse_alert_row(row) for row in rows]


def get_open_alert_for_rule(rule_id: str) -> dict[str, Any] | None:
    """Most recent OPEN alert for one rule, or None if it has no open
    alert. For the scheduler's auto-resolve step (see scheduler.py):
    architecture.md §8's "a passing rule auto-clears its alert on a later
    run" needs to find *which* alert to resolve when a scheduled
    re-execution comes back PASSED -- update_alert_status()'s own
    docstring has anticipated this caller since it was written, but never
    had one until the scheduler existed to trigger it.

    Newest-first + LIMIT 1 rather than assuming at most one OPEN alert per
    rule exists -- alert_agent.py creates one new alert per FAILED run with
    no dedup (deferred-and-future-work.md #15), so a rule that failed
    repeatedly before a scheduler ever ran could have several OPEN alerts;
    this resolves the most recent one, same "newest wins" convention as
    every other list_*() in this module.
    """
    rows = run_app_query(
        """
        SELECT ALERT_ID
        FROM ALERTS.ALERTS
        WHERE RULE_ID = %(rule_id)s AND STATUS = 'OPEN'
        ORDER BY CREATED_AT DESC
        LIMIT 1
        """,
        {"rule_id": rule_id},
    )
    return {"alert_id": rows[0]["ALERT_ID"]} if rows else None


def get_alert(alert_id: str) -> dict[str, Any] | None:
    """Fetch one alert by id, joined the same way as list_alerts(). Returns
    None if not found -- used by the accept/false-positive routes to 404
    before calling update_alert_status().

    Also fetches violation_samples (get_violation_samples()) -- a second
    query, done only here and not in list_alerts(), to avoid an N+1 on the
    Alerts Dashboard's list view; matches this codebase's existing
    "detail views lazy-fetch more" convention (e.g. list_approved_rules()
    vs. get_approved_rule() already differ the same way).
    """
    rows = run_app_query(
        """
        SELECT
            al.*,
            ar.RULE_NAME, ar.RULE_TYPE, ar.DATABASE_NAME, ar.SCHEMA_NAME,
            ar.TABLE_NAME, ar.COLUMN_NAME
        FROM ALERTS.ALERTS al
        LEFT JOIN RULES.RULE_INSTANCES ar ON ar.RULE_ID = al.RULE_ID
        WHERE al.ALERT_ID = %(alert_id)s
        """,
        {"alert_id": alert_id},
    )
    if not rows:
        return None
    alert = _parse_alert_row(rows[0])
    alert["violation_samples"] = get_violation_samples(alert_id)
    return alert


def get_alerts_summary() -> dict[str, Any]:
    """Aggregate counts for the Alerts Dashboard's summary tiles:
    total_open_alerts, critical_alerts (OPEN + CRITICAL), warning_alerts
    (OPEN + WARNING), failed_rules_today (distinct rules with an alert
    created today), tables_affected (distinct database.schema.table among
    OPEN alerts).

    One query, not five -- COUNT_IF/COUNT(DISTINCT ...) all read the same
    joined row set in a single pass rather than five round-trips to
    Snowflake for one dashboard load.
    """
    rows = run_app_query(
        """
        SELECT
            COUNT_IF(al.STATUS = 'OPEN') AS TOTAL_OPEN_ALERTS,
            COUNT_IF(al.STATUS = 'OPEN' AND al.SEVERITY = 'CRITICAL') AS CRITICAL_ALERTS,
            COUNT_IF(al.STATUS = 'OPEN' AND al.SEVERITY = 'WARNING') AS WARNING_ALERTS,
            COUNT(DISTINCT CASE
                WHEN TO_DATE(al.CREATED_AT) = TO_DATE(CURRENT_TIMESTAMP())
                THEN al.RULE_ID
            END) AS FAILED_RULES_TODAY,
            COUNT(DISTINCT CASE
                WHEN al.STATUS = 'OPEN'
                THEN ar.DATABASE_NAME || '.' || ar.SCHEMA_NAME || '.' || ar.TABLE_NAME
            END) AS TABLES_AFFECTED
        FROM ALERTS.ALERTS al
        LEFT JOIN RULES.RULE_INSTANCES ar ON ar.RULE_ID = al.RULE_ID
        """
    )
    row = rows[0]
    return {
        "total_open_alerts": row["TOTAL_OPEN_ALERTS"],
        "critical_alerts": row["CRITICAL_ALERTS"],
        "warning_alerts": row["WARNING_ALERTS"],
        "failed_rules_today": row["FAILED_RULES_TODAY"],
        "tables_affected": row["TABLES_AFFECTED"],
    }


def update_alert_status(alert_id: str, status: str) -> None:
    """Transition an alert: OPEN -> ACCEPTED / REJECTED / FALSE_POSITIVE, or
    auto-resolve when a rule passes on a later scan.

    Not in the original function list -- added because store_alert() only
    creates alerts, and the README requires these status transitions (user
    marks false positive; a passing re-run clears the alert). Without this,
    ALERTS.STATUS could never change after creation.
    """
    run_app_query(
        """
        UPDATE ALERTS.ALERTS
        SET STATUS = %(status)s, UPDATED_AT = CURRENT_TIMESTAMP()
        WHERE ALERT_ID = %(alert_id)s
        """,
        {"alert_id": alert_id, "status": status},
    )


def store_violation_samples(alert_id: str, sample_rows: list | dict) -> str:
    """Insert masked/raw sample violating rows for an alert. Returns the new
    sample_id. Caller is responsible for masking per each column's
    LLM_SHARING_POLICY before calling this -- this function stores whatever
    it's given.
    """
    sample_id = _new_id()
    run_app_query(
        """
        INSERT INTO ALERTS.ALERT_VIOLATION_SAMPLES (SAMPLE_ID, ALERT_ID, SAMPLE_ROWS)
        SELECT %(sample_id)s, %(alert_id)s, PARSE_JSON(%(sample_rows)s)
        """,
        {
            "sample_id": sample_id,
            "alert_id": alert_id,
            "sample_rows": _json_or_null(sample_rows),
        },
    )
    return sample_id


def get_violation_samples(alert_id: str) -> dict[str, Any] | None:
    """Most recent ALERT_VIOLATION_SAMPLES row for one alert, parsed back
    out of its VARIANT column -- the {rows, note, evidence} dict
    tools/sample_query_tools.build_sample_failed_rows() built at alert-
    creation time. Returns None if nothing was ever stored for this alert
    (e.g. an alert created before this feature existed, or one whose sample
    build failed and was swallowed by alert_agent.py's own try/except).
    Newest-first + LIMIT 1, same "one row per alert in practice, but don't
    assume it" convention as get_open_alert_for_rule().
    """
    rows = run_app_query(
        """
        SELECT SAMPLE_ROWS
        FROM ALERTS.ALERT_VIOLATION_SAMPLES
        WHERE ALERT_ID = %(alert_id)s
        ORDER BY CREATED_AT DESC
        LIMIT 1
        """,
        {"alert_id": alert_id},
    )
    if not rows or rows[0]["SAMPLE_ROWS"] is None:
        return None
    return json.loads(rows[0]["SAMPLE_ROWS"])


# ---------------------------------------------------------------------------
# LOGS.AGENT_RUN_LOGS
# ---------------------------------------------------------------------------


def log_agent_run(
    scan_id: str,
    agent_name: str,
    step_name: str,
    status: str,
    message: str | None = None,
    details: dict | None = None,
) -> str:
    """Insert one agent-activity log entry, for the UI's live progress feed.

    Not in the original function list -- added because LOGS.AGENT_RUN_LOGS
    (built specifically for the README's "UI can see logs overview of what
    agent is performing" requirement) had no function writing to it.
    Returns the new log_id.
    """
    log_id = _new_id()
    run_app_query(
        """
        INSERT INTO LOGS.AGENT_RUN_LOGS
            (LOG_ID, SCAN_ID, AGENT_NAME, STEP_NAME, STATUS, MESSAGE, DETAILS)
        SELECT
            %(log_id)s, %(scan_id)s, %(agent_name)s, %(step_name)s,
            %(status)s, %(message)s, PARSE_JSON(%(details)s)
        """,
        {
            "log_id": log_id,
            "scan_id": scan_id,
            "agent_name": agent_name,
            "step_name": step_name,
            "status": status,
            "message": message,
            "details": _json_or_null(details),
        },
    )
    return log_id


def _parse_agent_run_log_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "log_id": row["LOG_ID"],
        "scan_id": row["SCAN_ID"],
        "agent_name": row["AGENT_NAME"],
        "step_name": row["STEP_NAME"],
        "status": row["STATUS"],
        "message": row["MESSAGE"],
        "details": json.loads(row["DETAILS"]) if row["DETAILS"] else None,
        "logged_at": row["LOGGED_AT"],
    }


def list_agent_run_logs(scan_id: str) -> list[dict[str, Any]]:
    """Every log entry for one scan, oldest first -- the UI's live progress
    feed (README requirement: "user can see what the agent is doing").
    Chronological (not newest-first, unlike every other list_*() in this
    module) because a progress feed reads top-to-bottom as a timeline, not
    as a most-recent-first table.
    """
    rows = run_app_query(
        """
        SELECT LOG_ID, SCAN_ID, AGENT_NAME, STEP_NAME, STATUS, MESSAGE, DETAILS, LOGGED_AT
        FROM LOGS.AGENT_RUN_LOGS
        WHERE SCAN_ID = %(scan_id)s
        ORDER BY LOGGED_AT ASC
        """,
        {"scan_id": scan_id},
    )
    return [_parse_agent_run_log_row(row) for row in rows]


# ---------------------------------------------------------------------------
# CORE.SCAN_SCHEDULES -- scheduler (see apps/backend/agent_service/scheduler.py)
# ---------------------------------------------------------------------------


def _parse_scan_schedule_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "schedule_id": row["SCHEDULE_ID"],
        "schedule_type": row["SCHEDULE_TYPE"],
        "target_database": row["TARGET_DATABASE"],
        "target_schema": row["TARGET_SCHEMA"],
        "target_table": row["TARGET_TABLE"],
        "interval_minutes": row["INTERVAL_MINUTES"],
        "is_active": row["IS_ACTIVE"],
        "last_run_at": row["LAST_RUN_AT"],
        "created_at": row["CREATED_AT"],
    }


def create_scan_schedule(
    schedule_type: str,
    target_database: str,
    target_schema: str | None,
    target_table: str | None,
    interval_minutes: int,
) -> str:
    """Create one schedule row. schedule_type is 'RULE_EXECUTION' (re-run
    approved rules) or 'RESCAN' (re-run recommend-rules). Returns the new
    schedule_id.
    """
    schedule_id = _new_id()
    run_app_query(
        """
        INSERT INTO CORE.SCAN_SCHEDULES
            (SCHEDULE_ID, SCHEDULE_TYPE, TARGET_DATABASE, TARGET_SCHEMA,
             TARGET_TABLE, INTERVAL_MINUTES)
        VALUES
            (%(schedule_id)s, %(schedule_type)s, %(target_database)s,
             %(target_schema)s, %(target_table)s, %(interval_minutes)s)
        """,
        {
            "schedule_id": schedule_id,
            "schedule_type": schedule_type,
            "target_database": target_database,
            "target_schema": target_schema,
            "target_table": target_table,
            "interval_minutes": interval_minutes,
        },
    )
    return schedule_id


def list_scan_schedules(is_active: bool | None = None) -> list[dict[str, Any]]:
    """List all schedules, optionally filtered to active-only -- the
    scheduler's own poll loop calls this with is_active=True on every tick
    to find what's due; the (future) settings-page route calls it with no
    filter to show everything, active or not.
    """
    where_sql = "WHERE IS_ACTIVE = %(is_active)s" if is_active is not None else ""
    rows = run_app_query(
        f"""
        SELECT SCHEDULE_ID, SCHEDULE_TYPE, TARGET_DATABASE, TARGET_SCHEMA,
               TARGET_TABLE, INTERVAL_MINUTES, IS_ACTIVE, LAST_RUN_AT, CREATED_AT
        FROM CORE.SCAN_SCHEDULES
        {where_sql}
        ORDER BY CREATED_AT ASC
        """,
        {"is_active": is_active} if is_active is not None else {},
    )
    return [_parse_scan_schedule_row(row) for row in rows]


def update_scan_schedule_last_run(schedule_id: str) -> None:
    """Stamp LAST_RUN_AT = now for one schedule -- called by the scheduler
    right after it fires a due schedule, so the next poll tick's "is this
    due" check (LAST_RUN_AT + INTERVAL_MINUTES <= now) is based on a real
    completed run, not a stale/never-run timestamp.
    """
    run_app_query(
        """
        UPDATE CORE.SCAN_SCHEDULES
        SET LAST_RUN_AT = CURRENT_TIMESTAMP()
        WHERE SCHEDULE_ID = %(schedule_id)s
        """,
        {"schedule_id": schedule_id},
    )


def get_scan_schedule(schedule_id: str) -> dict[str, Any] | None:
    """Fetch one schedule by id. Returns None if not found -- used by the
    deactivate route to 404 before calling set_scan_schedule_active(), same
    fetch-then-404 convention as get_alert()/get_approved_rule().
    """
    rows = run_app_query(
        """
        SELECT SCHEDULE_ID, SCHEDULE_TYPE, TARGET_DATABASE, TARGET_SCHEMA,
               TARGET_TABLE, INTERVAL_MINUTES, IS_ACTIVE, LAST_RUN_AT, CREATED_AT
        FROM CORE.SCAN_SCHEDULES
        WHERE SCHEDULE_ID = %(schedule_id)s
        """,
        {"schedule_id": schedule_id},
    )
    return _parse_scan_schedule_row(rows[0]) if rows else None


def set_scan_schedule_active(schedule_id: str, is_active: bool) -> None:
    """Flip IS_ACTIVE on one schedule -- the Settings page's Deactivate
    button. scheduler.py's poll loop only ever fires is_active=True rows, so
    this is the only lever needed to stop a schedule from running again (no
    edit/delete route exists, matching the explicit create+list+deactivate
    scope for this feature).
    """
    run_app_query(
        """
        UPDATE CORE.SCAN_SCHEDULES
        SET IS_ACTIVE = %(is_active)s
        WHERE SCHEDULE_ID = %(schedule_id)s
        """,
        {"schedule_id": schedule_id, "is_active": is_active},
    )


# ---------------------------------------------------------------------------
# RULES.RULE_DEFINITIONS -- the rule library (docs/rules-architecture.md §4.3)
#
# Phase 1 scope: storage CRUD only. Nothing in this codebase writes to this
# table yet -- 14_seed_rule_definitions.sql inserts the 11 SYSTEM rows
# directly via SQL, and the recommendation-agent rework that reads/writes
# this table on the CLAUDE/USER path (§5.4, §6.1) is a later phase.
# ---------------------------------------------------------------------------


def _parse_rule_definition_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "definition_id": row["DEFINITION_ID"],
        "name": row["NAME"],
        "category": row["CATEGORY"],
        "description": row["DESCRIPTION"],
        "check_logic": row["CHECK_LOGIC"],
        "parameters_schema": (
            json.loads(row["PARAMETERS_SCHEMA"]) if row["PARAMETERS_SCHEMA"] else None
        ),
        "default_threshold_config": (
            json.loads(row["DEFAULT_THRESHOLD_CONFIG"]) if row["DEFAULT_THRESHOLD_CONFIG"] else None
        ),
        "default_severity": row["DEFAULT_SEVERITY"],
        "allowed_scopes": json.loads(row["ALLOWED_SCOPES"]) if row["ALLOWED_SCOPES"] else None,
        "sql_template": row["SQL_TEMPLATE"],
        "source": row["SOURCE"],
        "status": row["STATUS"],
        "instance_count": row["INSTANCE_COUNT"],
        "approval_count": row["APPROVAL_COUNT"],
        "created_at": row["CREATED_AT"],
        "created_by": row["CREATED_BY"],
    }


def create_rule_definition(
    name: str,
    category: str,
    description: str,
    check_logic: str,
    allowed_scopes: list[str],
    parameters_schema: dict | None = None,
    default_threshold_config: dict | None = None,
    default_severity: str | None = None,
    sql_template: str | None = None,
    source: str = "USER",
    status: str = "ACTIVE",
    created_by: str | None = None,
) -> str:
    """Insert a new rule definition. Returns the new definition_id.

    Only called for CLAUDE/USER-sourced definitions -- SYSTEM definitions are
    seeded directly by 14_seed_rule_definitions.sql, not through this
    function. status defaults to ACTIVE: a definition only reaches this
    table at the moment it graduates from PROPOSED (§4.3.1), which per §6.1
    happens exactly when a human approves an instance using it -- there is
    no code path that inserts a still-PROPOSED definition row here.
    """
    definition_id = _new_id()
    run_app_query(
        """
        INSERT INTO RULES.RULE_DEFINITIONS
            (DEFINITION_ID, NAME, CATEGORY, DESCRIPTION, CHECK_LOGIC,
             PARAMETERS_SCHEMA, DEFAULT_THRESHOLD_CONFIG, DEFAULT_SEVERITY,
             ALLOWED_SCOPES, SQL_TEMPLATE, SOURCE, STATUS, CREATED_BY)
        SELECT
            %(definition_id)s, %(name)s, %(category)s, %(description)s,
            %(check_logic)s, PARSE_JSON(%(parameters_schema)s),
            PARSE_JSON(%(default_threshold_config)s), %(default_severity)s,
            PARSE_JSON(%(allowed_scopes)s), %(sql_template)s, %(source)s,
            %(status)s, %(created_by)s
        """,
        {
            "definition_id": definition_id,
            "name": name,
            "category": category,
            "description": description,
            "check_logic": check_logic,
            "parameters_schema": _json_or_null(parameters_schema),
            "default_threshold_config": _json_or_null(default_threshold_config),
            "default_severity": default_severity,
            "allowed_scopes": _json_or_null(allowed_scopes),
            "sql_template": sql_template,
            "source": source,
            "status": status,
            "created_by": created_by,
        },
    )
    return definition_id


def get_rule_definition(definition_id: str) -> dict[str, Any] | None:
    """Fetch one rule definition by id. Returns None if not found."""
    rows = run_app_query(
        "SELECT * FROM RULES.RULE_DEFINITIONS WHERE DEFINITION_ID = %(definition_id)s",
        {"definition_id": definition_id},
    )
    return _parse_rule_definition_row(rows[0]) if rows else None


def ensure_system_rule_definitions_seeded() -> int:
    """Self-heal the SYSTEM rule library: if RULE_DEFINITIONS has zero SYSTEM
    rows (e.g. after a fresh-start TRUNCATE), re-run
    infra/snowflake/14_seed_rule_definitions.sql so the app doesn't need a
    manual migration re-run before the next scan can recommend anything.
    Returns the number of SYSTEM rows present after this call (0 only if the
    seed file itself is missing/unreadable, which is logged, not raised --
    matching this module's "a missing nice-to-have must not break the
    caller" convention elsewhere).

    Reads the migration file directly rather than duplicating its 11 INSERT
    statements here -- the file stays the single source of truth for what
    "the SYSTEM library" contains; this function only decides *whether* to
    replay it. Every statement in that file is already individually
    idempotent (INSERT...WHERE NOT EXISTS per definition name), so calling
    this on a normal boot with the library already seeded is a cheap no-op
    (11 SELECT-and-skip statements), not a duplicate-insert risk.
    """
    existing = run_app_query(
        "SELECT COUNT(*) AS CNT FROM RULES.RULE_DEFINITIONS WHERE SOURCE = 'SYSTEM'"
    )
    if existing[0]["CNT"] > 0:
        return existing[0]["CNT"]

    seed_path = (
        Path(__file__).resolve().parents[4] / "infra" / "snowflake" / "14_seed_rule_definitions.sql"
    )
    try:
        sql_text = seed_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"[storage_tools] Could not read {seed_path} to re-seed SYSTEM rule definitions: {exc}")
        return 0

    cleaned = "\n".join(line.split("--", 1)[0] for line in sql_text.splitlines())
    statements = [s.strip() for s in cleaned.split(";") if s.strip()]
    for stmt in statements:
        run_app_query(stmt)

    reseeded = run_app_query(
        "SELECT COUNT(*) AS CNT FROM RULES.RULE_DEFINITIONS WHERE SOURCE = 'SYSTEM'"
    )
    print(f"[storage_tools] Re-seeded {reseeded[0]['CNT']} SYSTEM rule definitions.")
    return reseeded[0]["CNT"]


def list_rule_definitions(
    status: str | None = None, source: str | None = None
) -> list[dict[str, Any]]:
    """List rule definitions, ordered by APPROVAL_COUNT DESC then NAME --
    the ranking §5.4 specifies for Claude's library context ("ordered by
    approval_count DESC. This is what Claude works from"), so the most
    frequently approved definitions surface first for both that future
    caller and any definitions-library UI.
    """
    where: list[str] = []
    params: dict[str, Any] = {}
    if status is not None:
        where.append("STATUS = %(status)s")
        params["status"] = status
    if source is not None:
        where.append("SOURCE = %(source)s")
        params["source"] = source
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    rows = run_app_query(
        f"""
        SELECT * FROM RULES.RULE_DEFINITIONS
        {where_sql}
        ORDER BY APPROVAL_COUNT DESC, NAME ASC
        """,
        params,
    )
    return [_parse_rule_definition_row(row) for row in rows]


def set_rule_definition_status(definition_id: str, status: str) -> None:
    """Flip a definition between ACTIVE and DISABLED (§6.3's Disable/
    Re-enable definition actions). Existing RULE_INSTANCES rows using this
    definition are unaffected either way -- only future suggestion behavior
    (a later phase) reads this status.
    """
    run_app_query(
        """
        UPDATE RULES.RULE_DEFINITIONS
        SET STATUS = %(status)s
        WHERE DEFINITION_ID = %(definition_id)s
        """,
        {"definition_id": definition_id, "status": status},
    )


def increment_rule_definition_approval_count(definition_id: str) -> None:
    """APPROVAL_COUNT += 1 -- called once per instance approval (§6.1:
    "Increment definition.approval_count by 1"), regardless of how many
    instances of this definition already exist.
    """
    run_app_query(
        """
        UPDATE RULES.RULE_DEFINITIONS
        SET APPROVAL_COUNT = APPROVAL_COUNT + 1
        WHERE DEFINITION_ID = %(definition_id)s
        """,
        {"definition_id": definition_id},
    )


def increment_rule_definition_instance_count(definition_id: str, delta: int = 1) -> None:
    """INSTANCE_COUNT += delta -- §4.3 notes this is "maintained by code, not
    a join": incremented when a new RULE_INSTANCES row is created against
    this definition. delta accepts negative values for the deactivate path
    a later phase may add; no caller passes a negative delta yet.
    """
    run_app_query(
        """
        UPDATE RULES.RULE_DEFINITIONS
        SET INSTANCE_COUNT = INSTANCE_COUNT + %(delta)s
        WHERE DEFINITION_ID = %(definition_id)s
        """,
        {"definition_id": definition_id, "delta": delta},
    )


# ---------------------------------------------------------------------------
# RULES.RULE_GROUPS -- display/approval grouping only (§4.6). No effect on
# execution; purely how the approval screen clusters related instances.
# ---------------------------------------------------------------------------


def _parse_rule_group_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "group_id": row["GROUP_ID"],
        "name": row["NAME"],
        "description": row["DESCRIPTION"],
        "definition_id": row["DEFINITION_ID"],
        "scope_level": row["SCOPE_LEVEL"],
        "database_name": row["DATABASE_NAME"],
        "schema_name": row["SCHEMA_NAME"],
        "table_name": row["TABLE_NAME"],
        "created_at": row["CREATED_AT"],
    }


def create_rule_group(
    name: str,
    definition_id: str,
    scope_level: str,
    database_name: str,
    schema_name: str,
    table_name: str | None = None,
    description: str | None = None,
) -> str:
    """Insert a new rule group. Returns the new group_id."""
    group_id = _new_id()
    run_app_query(
        """
        INSERT INTO RULES.RULE_GROUPS
            (GROUP_ID, NAME, DESCRIPTION, DEFINITION_ID, SCOPE_LEVEL,
             DATABASE_NAME, SCHEMA_NAME, TABLE_NAME)
        VALUES
            (%(group_id)s, %(name)s, %(description)s, %(definition_id)s,
             %(scope_level)s, %(database_name)s, %(schema_name)s, %(table_name)s)
        """,
        {
            "group_id": group_id,
            "name": name,
            "description": description,
            "definition_id": definition_id,
            "scope_level": scope_level,
            "database_name": database_name,
            "schema_name": schema_name,
            "table_name": table_name,
        },
    )
    return group_id


def get_rule_group(group_id: str) -> dict[str, Any] | None:
    """Fetch one rule group by id. Returns None if not found."""
    rows = run_app_query(
        "SELECT * FROM RULES.RULE_GROUPS WHERE GROUP_ID = %(group_id)s",
        {"group_id": group_id},
    )
    return _parse_rule_group_row(rows[0]) if rows else None


def find_rule_group(
    name: str, definition_id: str, scope_level: str, schema_name: str
) -> dict[str, Any] | None:
    """Look up an existing group by the exact key §4.6 specifies for re-scan
    matching ("The group_id on RECOMMENDED_INSTANCES is matched by name +
    definition_id + scope_level + schema"). Returns the newest match if
    somehow more than one exists, or None if no group matches -- the caller
    (a later phase's recommendation agent) creates a new group on None.
    """
    rows = run_app_query(
        """
        SELECT * FROM RULES.RULE_GROUPS
        WHERE NAME = %(name)s AND DEFINITION_ID = %(definition_id)s
          AND SCOPE_LEVEL = %(scope_level)s AND SCHEMA_NAME = %(schema_name)s
        ORDER BY CREATED_AT DESC
        LIMIT 1
        """,
        {
            "name": name,
            "definition_id": definition_id,
            "scope_level": scope_level,
            "schema_name": schema_name,
        },
    )
    return _parse_rule_group_row(rows[0]) if rows else None


def list_rule_groups(
    database_name: str | None = None,
    schema_name: str | None = None,
    table_name: str | None = None,
) -> list[dict[str, Any]]:
    """List rule groups, newest first, optionally filtered by location --
    the approval screen's grouped view (§6.4)."""
    where: list[str] = []
    params: dict[str, Any] = {}
    if database_name is not None:
        where.append("DATABASE_NAME = %(database_name)s")
        params["database_name"] = database_name
    if schema_name is not None:
        where.append("SCHEMA_NAME = %(schema_name)s")
        params["schema_name"] = schema_name
    if table_name is not None:
        where.append("TABLE_NAME = %(table_name)s")
        params["table_name"] = table_name
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    rows = run_app_query(
        f"""
        SELECT * FROM RULES.RULE_GROUPS
        {where_sql}
        ORDER BY CREATED_AT DESC
        """,
        params,
    )
    return [_parse_rule_group_row(row) for row in rows]


def get_recommended_instances_pending_by_group(group_id: str) -> list[dict[str, Any]]:
    """Every PENDING RECOMMENDED_INSTANCES row sharing this group_id -- the
    approval screen's "expand a group to see its instances" view (§6.4).
    PENDING = not present in RULE_INSTANCES.ORIGINAL_RECOMMENDED_RULE_ID and
    not present in REJECTED_INSTANCES.ORIGINAL_RECOMMENDED_RULE_ID, same
    definition every other PENDING lookup in this module uses. Reuses
    _parse_recommended_rule_row() so the shape matches list_recommended_rules().
    """
    rows = run_app_query(
        """
        SELECT * FROM RULES.RECOMMENDED_INSTANCES
        WHERE SUGGESTED_GROUP_ID = %(group_id)s
          AND RULE_ID NOT IN (SELECT ORIGINAL_RECOMMENDED_RULE_ID FROM RULES.RULE_INSTANCES)
          AND RULE_ID NOT IN (SELECT ORIGINAL_RECOMMENDED_RULE_ID FROM RULES.REJECTED_INSTANCES)
        ORDER BY CREATED_AT DESC
        """,
        {"group_id": group_id},
    )
    return [_parse_recommended_rule_row(row) for row in rows]


# ---------------------------------------------------------------------------
# Fingerprinting, dedup lookups, and feedback suppression (§4.7/§4.9) --
# Phase 2 of the rules redesign. RULE_FINGERPRINT on RECOMMENDED_INSTANCES
# already exists as a column (Phase 1 populated it with placeholder
# "source:template"/"source:claude" strings) -- these functions make it a
# real identity hash going forward and give the recommendation agent the
# three dedup lookups §4.7's priority order needs plus §4.9's feedback
# suppression query. Old rows keep their placeholder string values; nothing
# here rewrites them.
# ---------------------------------------------------------------------------


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=_json_default) if value is not None else ""


def compute_rule_fingerprint(
    definition_id: str | None,
    scope: str | None,
    database_name: str,
    schema_name: str,
    table_name: str,
    target_config: dict | None,
    threshold_config: dict | None,
) -> str:
    """Real identity hash for one instance (§4.7): sha256 of
    definition_id + scope + database_name + schema_name + table_name +
    canonical_json(target_config) + canonical_json(threshold_config),
    '|'-joined so no part can collide with another (a bare concatenation
    could make ("AB", "C") hash the same as ("A", "BC")).

    canonical_json sorts dict keys so the same target_config/threshold_config
    dict always hashes identically regardless of construction order.
    """
    parts = [
        definition_id or "",
        scope or "",
        database_name,
        schema_name,
        table_name,
        _canonical_json(target_config),
        _canonical_json(threshold_config),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def compute_target_key(
    definition_id: str | None,
    scope: str | None,
    target_config: dict | None,
) -> str:
    """Narrower hash used only for feedback suppression matching (§4.9): the
    key is deliberately (definition_id + scope + target_config) with no
    database/schema/table (the caller already scopes its query to one table)
    and no threshold_config (an EDIT/FALSE_POSITIVE/REJECT signal about "this
    check on this target" must still match a candidate proposing a different
    threshold for the same target -- including threshold_config here would
    make the feedback never match anything but an identical threshold).
    """
    parts = [definition_id or "", scope or "", _canonical_json(target_config)]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def get_active_instance_fingerprints(
    database_name: str, schema_name: str, table_name: str
) -> set[str]:
    """Fingerprints of every active RULE_INSTANCES row on this table (§4.7
    priority 1: "Fingerprint matches an active RULE_INSTANCES row -> skip
    entirely. Already running."). Joins through ORIGINAL_RECOMMENDED_RULE_ID
    since the fingerprint itself lives on RECOMMENDED_INSTANCES, not on
    RULE_INSTANCES.
    """
    rows = run_app_query(
        """
        SELECT DISTINCT r.RULE_FINGERPRINT
        FROM RULES.RULE_INSTANCES i
        JOIN RULES.RECOMMENDED_INSTANCES r ON r.RULE_ID = i.ORIGINAL_RECOMMENDED_RULE_ID
        WHERE i.DATABASE_NAME = %(database_name)s
          AND i.SCHEMA_NAME = %(schema_name)s
          AND i.TABLE_NAME = %(table_name)s
          AND i.IS_ACTIVE = TRUE
          AND r.RULE_FINGERPRINT IS NOT NULL
        """,
        {"database_name": database_name, "schema_name": schema_name, "table_name": table_name},
    )
    return {row["RULE_FINGERPRINT"] for row in rows}


def get_rejected_instance_fingerprints(
    database_name: str, schema_name: str, table_name: str
) -> dict[str, str]:
    """Map of fingerprint -> most recent rejection_reason for this table
    (§4.7 priority 3: "Fingerprint matches a REJECTED_INSTANCES row -> skip.
    Respect the rejection."). The re-propose-on-new-evidence exception in
    §4.7 is the caller's job -- this just returns the lookup.

    Rows are fetched newest-first (ORDER BY j.REJECTED_AT DESC) and only
    inserted into the dict on first-seen fingerprint, same "latest wins"
    pattern as agents/rule_recommendation_agent.py's _apply_feedback().
    """
    rows = run_app_query(
        """
        SELECT r.RULE_FINGERPRINT, j.REJECTION_REASON
        FROM RULES.REJECTED_INSTANCES j
        JOIN RULES.RECOMMENDED_INSTANCES r ON r.RULE_ID = j.ORIGINAL_RECOMMENDED_RULE_ID
        WHERE r.DATABASE_NAME = %(database_name)s
          AND r.SCHEMA_NAME = %(schema_name)s
          AND r.TABLE_NAME = %(table_name)s
          AND r.RULE_FINGERPRINT IS NOT NULL
        ORDER BY j.REJECTED_AT DESC
        """,
        {"database_name": database_name, "schema_name": schema_name, "table_name": table_name},
    )
    result: dict[str, str] = {}
    for row in rows:
        fingerprint = row["RULE_FINGERPRINT"]
        if fingerprint not in result:
            result[fingerprint] = row["REJECTION_REASON"]
    return result


def get_pending_instance_by_fingerprint(
    database_name: str, schema_name: str, table_name: str
) -> dict[str, str]:
    """Map of RULE_FINGERPRINT -> RULE_ID for every PENDING RECOMMENDED_INSTANCES
    row on this table (§4.7 priority 2: "Fingerprint matches a PENDING
    RECOMMENDED_INSTANCES row -> refresh."). Same PENDING definition (not in
    RULE_INSTANCES, not in REJECTED_INSTANCES) as get_pending_rule_fingerprints(),
    keyed on RULE_FINGERPRINT instead of (table, column, rule_type) -- that
    function stays as-is for its existing callers.
    """
    rows = run_app_query(
        """
        SELECT r.RULE_ID, r.RULE_FINGERPRINT
        FROM RULES.RECOMMENDED_INSTANCES r
        WHERE r.DATABASE_NAME = %(database_name)s
          AND r.SCHEMA_NAME   = %(schema_name)s
          AND r.TABLE_NAME    = %(table_name)s
          AND r.RULE_FINGERPRINT IS NOT NULL
          AND r.RULE_ID NOT IN (SELECT ORIGINAL_RECOMMENDED_RULE_ID FROM RULES.RULE_INSTANCES)
          AND r.RULE_ID NOT IN (SELECT ORIGINAL_RECOMMENDED_RULE_ID FROM RULES.REJECTED_INSTANCES)
        """,
        {"database_name": database_name, "schema_name": schema_name, "table_name": table_name},
    )
    return {row["RULE_FINGERPRINT"]: row["RULE_ID"] for row in rows}


def get_feedback_suppression_data(
    database_name: str, schema_name: str, table_name: str
) -> dict[str, Any]:
    """The exact §4.9 feedback suppression query plus its three lookup sets:

    - suppressed: target_key -> rejection comment, from FEEDBACK_TYPE=REJECT rows
    - priority_halved: set of target_keys, from FEEDBACK_TYPE=FALSE_POSITIVE rows
    - threshold_seeds: target_key -> threshold_config, from FEEDBACK_TYPE=EDIT
      rows (first-seen wins, rows already newest-first)

    Rows without both DEFINITION_ID and SCOPE are old feedback recorded
    before this feature existed (or via the still-active store_user_feedback())
    and can't be matched by the (definition_id + scope + target_config) key --
    skipped, not an error.
    """
    rows = run_app_query(
        """
        SELECT FEEDBACK_TYPE, DEFINITION_ID, SCOPE, TARGET_CONFIG, THRESHOLD_CONFIG, COMMENT
        FROM RULES.USER_FEEDBACK
        WHERE DATABASE_NAME = %(database_name)s
          AND SCHEMA_NAME = %(schema_name)s
          AND TABLE_NAME = %(table_name)s
        ORDER BY CREATED_AT DESC
        """,
        {"database_name": database_name, "schema_name": schema_name, "table_name": table_name},
    )

    suppressed: dict[str, str] = {}
    priority_halved: set[str] = set()
    threshold_seeds: dict[str, dict] = {}

    for row in rows:
        definition_id = row["DEFINITION_ID"]
        scope = row["SCOPE"]
        if definition_id is None or scope is None:
            continue

        target_config = json.loads(row["TARGET_CONFIG"]) if row["TARGET_CONFIG"] else None
        key = compute_target_key(definition_id, scope, target_config)
        feedback_type = row["FEEDBACK_TYPE"]

        if feedback_type == "REJECT":
            if key not in suppressed:
                suppressed[key] = row["COMMENT"]
        elif feedback_type == "FALSE_POSITIVE":
            priority_halved.add(key)
        elif feedback_type == "EDIT" and row["THRESHOLD_CONFIG"] is not None:
            if key not in threshold_seeds:
                threshold_seeds[key] = json.loads(row["THRESHOLD_CONFIG"])

    return {
        "suppressed": suppressed,
        "priority_halved": priority_halved,
        "threshold_seeds": threshold_seeds,
    }


def store_user_feedback_v2(
    feedback_type: str,
    database_name: str,
    schema_name: str,
    table_name: str,
    source_type: str,
    source_id: str,
    definition_id: str | None = None,
    scope: str | None = None,
    target_config: dict | None = None,
    column_name: str | None = None,
    rule_type: str | None = None,
    comment: str | None = None,
    threshold_config: dict | None = None,
    created_by: str | None = None,
) -> str:
    """Insert one feedback record carrying the §4.9 fields (source_type/
    source_id/definition_id/scope/target_config) alongside the existing
    RULE_TYPE/DATABASE_NAME/SCHEMA_NAME/TABLE_NAME/COLUMN_NAME/
    THRESHOLD_CONFIG/COMMENT/CREATED_BY fields store_user_feedback() already
    writes. Returns the new feedback_id.

    A new function rather than a rework of store_user_feedback() -- that one
    is still used by the current approve/reject/edit routes and takes
    rule_id/alert_id params this ask replaces with source_type+source_id;
    changing its signature would break those callers today. source_type is
    RECOMMENDED_INSTANCE / RULE_INSTANCE / ALERT / DEFINITION; source_id is
    whichever id source_type points at (e.g. source_type="RECOMMENDED_INSTANCE",
    source_id=the recommended rule_id).
    """
    feedback_id = _new_id()
    run_app_query(
        """
        INSERT INTO RULES.USER_FEEDBACK
            (FEEDBACK_ID, FEEDBACK_TYPE, COMMENT, RULE_TYPE, DATABASE_NAME,
             SCHEMA_NAME, TABLE_NAME, COLUMN_NAME, THRESHOLD_CONFIG,
             CREATED_BY, SOURCE_TYPE, SOURCE_ID, DEFINITION_ID, SCOPE,
             TARGET_CONFIG)
        SELECT
            %(feedback_id)s, %(feedback_type)s, %(comment)s, %(rule_type)s,
            %(database_name)s, %(schema_name)s, %(table_name)s,
            %(column_name)s, PARSE_JSON(%(threshold_config)s),
            %(created_by)s, %(source_type)s, %(source_id)s,
            %(definition_id)s, %(scope)s, PARSE_JSON(%(target_config)s)
        """,
        {
            "feedback_id": feedback_id,
            "feedback_type": feedback_type,
            "comment": comment,
            "rule_type": rule_type,
            "database_name": database_name,
            "schema_name": schema_name,
            "table_name": table_name,
            "column_name": column_name,
            "threshold_config": _json_or_null(threshold_config),
            "created_by": created_by,
            "source_type": source_type,
            "source_id": source_id,
            "definition_id": definition_id,
            "scope": scope,
            "target_config": _json_or_null(target_config),
        },
    )
    return feedback_id


def set_rule_instance_active(instance_id: str, is_active: bool) -> None:
    """Flip IS_ACTIVE on one RULE_INSTANCES row (§6.3's Deactivate/Reactivate
    -- one function, both directions via the bool). No deletion; the
    instance is simply skipped at execution time. RULE_ID is kept as
    RULE_INSTANCES' primary key column name from Phase 1 for backward
    compat -- "instance_id" here names the concept, not a literal column.
    """
    run_app_query(
        """
        UPDATE RULES.RULE_INSTANCES
        SET IS_ACTIVE = %(is_active)s
        WHERE RULE_ID = %(instance_id)s
        """,
        {"instance_id": instance_id, "is_active": is_active},
    )
