"""Rule Execution Agent -- runs one *approved* rule's SQL now, on manual
trigger (architecture.md §4b's "Execute each rule's validated SQL", MVP1
scope: manual execution; scheduled execution is MVP2).

This is a distinct step from agents/rule_test_execution_agent.py, which
test-runs *recommended* rules once, before approval, so a human doesn't
approve blind. This agent runs *approved* rules, on demand, after approval.
Until this agent existed, nothing in this codebase had ever executed an
approved rule -- RULE_EXECUTION_HISTORY was schema-only.

Flow (per the ask):
    get approved rule -> validate SQL again -> run SQL -> store execution
    history -> if FAILED, create an alert; if PASSED, storage only.

Deviation flagged: every other agent in this codebase (metadata_agent.py,
profiling_agent.py, rule_recommendation_agent.py, ...) is pure compute --
storage/persistence calls live in the route (main.py), not the agent, per
this codebase's established convention (see storage-wiring work: main.py
loops and calls store_recommended_rule(), the graph/pipeline never do).
This agent breaks that convention on purpose, because the ask's flow
explicitly lists "store execution history" and "call Alert Agent" as steps
*inside* this agent, not the caller's. Kept as asked rather than silently
restructured back to the old convention -- flagging the deviation instead
of hiding it.

"Call Alert Agent" -- agents/alert_agent.run_alert_agent(), a real module
now (previously this called storage_tools.store_alert() directly, flagged
at the time as a stand-in until the Alert Agent itself was asked for --
see alert_agent.py's docstring for what it does and doesn't cover).

Execution statuses (per the ask -- four, not the three
RULE_EXECUTION_HISTORY.STATUS's own schema comment lists):
    PASSED  -- ran, failed_count == 0
    FAILED  -- ran, failed_count > 0 -> alert created
    ERROR   -- SQL raised at execution time (query itself broke)
    SKIPPED -- never ran: re-validation failed, or the rule is inactive.
               Distinct from ERROR (which means "we tried and it broke") --
               SKIPPED means "we deliberately did not attempt it."
"""

from __future__ import annotations

import json
from typing import Any

from agents.alert_agent import run_alert_agent
from tools.sample_query_tools import build_sample_failed_rows
from tools.snowflake_connection import run_query
from tools.sql_validation_tools import validate_sql
from tools.storage_tools import (
    get_approved_rule,
    list_latest_column_profiles,
    store_execution_result,
)

# Same rationale as rule_test_execution_agent.py's timeout: these are simple
# COUNT(*)-shaped aggregate queries (rule_template_tools.py), no sampling
# yet (deferred-and-future-work.md #4), so 30s is generous.
_RUN_QUERY_TIMEOUT_SECONDS = 30


def _execute(rule_sql: str) -> dict[str, Any]:
    """Run one rule's SQL and classify PASSED/FAILED/ERROR (not SKIPPED --
    that's decided before this is called, see run_rule_execution_agent()).
    """
    try:
        rows = run_query(rule_sql, timeout=_RUN_QUERY_TIMEOUT_SECONDS)
        if not rows:
            return {
                "status": "ERROR",
                "failed_count": None,
                "total_count": None,
                "failure_percentage": None,
                "error_message": "SQL executed but returned no rows",
            }

        row = rows[0]
        failed_count = row.get("FAILED_COUNT")
        total_count = row.get("TOTAL_COUNT")
        failure_percentage = (
            round(failed_count / total_count * 100, 4)
            if failed_count is not None and total_count
            else None
        )
        return {
            "status": "PASSED" if failed_count == 0 else "FAILED",
            "failed_count": failed_count,
            "total_count": total_count,
            "failure_percentage": failure_percentage,
            "error_message": None,
        }
    except Exception as exc:  # noqa: BLE001 -- surfaced as an ERROR row, not a crash
        return {
            "status": "ERROR",
            "failed_count": None,
            "total_count": None,
            "failure_percentage": None,
            "error_message": str(exc),
        }


def run_rule_execution_agent(rule_id: str) -> dict[str, Any]:
    """Run one approved rule end to end: fetch -> revalidate -> execute ->
    store history -> alert if failed.

    Output: {execution_id, alert_id, status, failed_count, total_count,
    failure_percentage, error_message} -- alert_id is None unless status
    was FAILED (see agents/alert_agent.run_alert_agent()). Raises
    ValueError if rule_id isn't an approved rule -- the caller (the /run
    route) turns that into a 404,
    same pattern as get_recommended_rule()'s None-return elsewhere in this
    codebase's routes, just surfaced as an exception here since this
    function's job is "run the whole thing," not "look it up."
    """
    rule = get_approved_rule(rule_id)
    if rule is None:
        raise ValueError(f"Approved rule {rule_id!r} not found")

    # Re-validate before running -- defense in depth (architecture.md §6:
    # "the validator is defense in depth, the role is the real wall, both
    # must hold"). The rule's SQL was already validated at recommendation
    # time and again if edited before approval, but a rule sitting in
    # RULE_INSTANCES could in principle be old, edited outside this flow,
    # or the allowed-table scope could matter differently at run time --
    # re-checking costs nothing and closes that gap rather than trusting a
    # status set once, earlier.
    #
    # For scope=CROSS_TABLE rules (docs/rules-architecture.md §5.7), the
    # allowed-table list is expanded to also include the ref table named in
    # target_config -- same expansion agents/sql_validation_agent.py already
    # applies at recommendation time. Without this, every CROSS_TABLE rule's
    # legitimate anti-join to a second table gets rejected as "not in scope"
    # on every single run, since re-validation is otherwise single-table-only
    # (confirmed directly: a real CROSS_TABLE rule SKIPPED at run time with
    # "SQL references table(s) not in the allowed scope" before this fix).
    allowed_tables = [f"{rule['database_name']}.{rule['schema_name']}.{rule['table_name']}"]
    if rule.get("scope") == "CROSS_TABLE":
        target_config = rule.get("target_config")
        if isinstance(target_config, str):
            try:
                target_config = json.loads(target_config)
            except (ValueError, TypeError):
                target_config = None
        if isinstance(target_config, dict):
            ref_database = target_config.get("ref_database")
            ref_schema = target_config.get("ref_schema")
            ref_table = target_config.get("ref_table")
            if ref_database and ref_schema and ref_table:
                allowed_tables.append(f"{ref_database}.{ref_schema}.{ref_table}")
    validation = validate_sql(rule["rule_sql"], allowed_tables=allowed_tables)

    if not rule["is_active"]:
        result = {
            "status": "SKIPPED",
            "failed_count": None,
            "total_count": None,
            "failure_percentage": None,
            "error_message": "Rule is inactive (IS_ACTIVE = false)",
        }
    elif not validation.is_valid:
        result = {
            "status": "SKIPPED",
            "failed_count": None,
            "total_count": None,
            "failure_percentage": None,
            "error_message": f"Re-validation failed: {'; '.join(validation.errors)}",
        }
    else:
        result = _execute(rule["rule_sql"])

    execution_id = store_execution_result(
        rule_id=rule_id,
        status=result["status"],
        failed_count=result["failed_count"],
        total_count=result["total_count"],
        failure_percentage=result["failure_percentage"],
        error_message=result["error_message"],
        instance_id=rule_id,
    )

    # Sample failed rows (only worth the extra query on a real FAILED run --
    # PASSED/ERROR/SKIPPED have nothing to sample or nothing that ran at
    # all). Column policies come from a fresh storage lookup, not an
    # in-memory column_profiles list -- this agent starts from
    # get_approved_rule(), not a fresh scan, so it has no profiling context
    # in scope the way the test-execution path does.
    sample_failed_rows = None
    if result["status"] == "FAILED":
        try:
            column_profiles = list_latest_column_profiles(
                rule["database_name"], rule["schema_name"], rule["table_name"]
            )
            column_policies = {
                c["column_name"]: c.get("llm_sharing_policy") for c in column_profiles
            }
            sample_failed_rows = build_sample_failed_rows(
                rule, result["failed_count"], result["total_count"], column_policies
            )
        except Exception:  # noqa: BLE001 -- sampling must not fail the real execution result
            sample_failed_rows = None

    # PASSED / ERROR / SKIPPED: execution history only, no alert -- per the
    # ask ("if passed, only store execution history"). ERROR/SKIPPED aren't
    # mentioned explicitly in the ask's two branches, but neither is "the
    # rule failed its check" (FAILED is the only branch that means the data
    # actually violated the rule) -- an ERROR/SKIPPED run is a run that
    # couldn't tell you that, so alerting on it would be a false signal.
    # run_alert_agent() itself no-ops (returns None) for anything but
    # FAILED, so this call is unconditional rather than gated here too.
    alert_id = run_alert_agent(rule, execution_id, result, sample_failed_rows)

    return {"execution_id": execution_id, "alert_id": alert_id, **result}
