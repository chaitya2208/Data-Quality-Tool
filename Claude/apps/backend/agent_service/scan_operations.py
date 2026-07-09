"""Shared scan/execution operations -- extracted out of src/main.py so
scheduler.py (a sibling module at the package root, not inside src/) can
call the exact same logic the manual HTTP routes use, without importing
main.py itself.

Why this had to be its own module rather than main.py exposing these
functions directly: main.py is loaded by uvicorn as
apps.backend.agent_service.src.main, not as a bare top-level "main" module
-- `from main import ...` inside scheduler.py raised
ModuleNotFoundError: No module named 'main' the first time this was
tried and verified directly against a running scheduler tick, since
`src/` was never added to sys.path as an importable package (no
src/__init__.py, and main.py's own sys.path.insert only adds its parent's
parent, i.e. this package root, not src/ itself). Extracting the shared
logic to a plain module at the package root (same level as tools/,
agents/, graphs/, scheduler.py) sidesteps the whole problem: both main.py
and scheduler.py import scan_operations normally, and scan_operations
itself never imports main.py or scheduler.py, so there's no cycle.

Every function here is unchanged in behavior from what previously lived
in main.py -- this is a pure move, not a rewrite.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from agents.metadata_agent import run_metadata_agent
from agents.pii_agent import run_pii_agent
from agents.profiling_agent import run_profiling_agent
from agents.rule_execution_agent import run_rule_execution_agent
from agents.rule_explanation_agent import run_rule_explanation_agent
from agents.rule_recommendation_agent import run_instance_recommendation_agent
from graphs.dq_workflow_graph import run_dq_workflow
from tools.snowflake_profiling_tools import store_profile_result
from tools.storage_tools import (
    create_scan_run,
    get_open_alert_for_rule,
    get_pending_instance_by_fingerprint,
    get_pending_rule_fingerprints,
    list_approved_rules,
    list_recommended_rules,
    log_agent_run,
    refresh_pending_rule,
    store_recommended_rule,
    update_alert_status,
    update_scan_status,
)

# Storage's VALIDATION_STATUS/TEST_STATUS columns are plain STRING with no
# CHECK constraint (04_create_rule_tables.sql's PENDING/PASSED/FAILED comment
# is convention only, not enforced) -- narrower than this codebase's own
# agent-local vocabularies (sql_validation_agent.py's VALID/INVALID;
# rule_test_execution_agent.py's PENDING/PASSED/FAILED/ERROR).
# Same mapping main.py has always used -- moved here since
# _recommend_rules_for_table() is the only place that reads it.
#
# ERROR is stored as ERROR, not mapped to FAILED (architecture.md 5.8): ERROR
# means the SQL itself failed to execute (broken query, timeout, missing
# table); FAILED means the query ran and found violations. Different
# problems, must stay visually distinct on the approval screen.
_VALIDATION_STATUS_MAP = {"VALID": "PASSED", "INVALID": "FAILED"}
_TEST_STATUS_MAP = {"PENDING": "PENDING", "PASSED": "PASSED", "FAILED": "FAILED", "ERROR": "ERROR"}


def recommend_rules_for_table(database_name: str, schema_name: str, table_name: str) -> dict[str, Any]:
    """Run the full LangGraph workflow (metadata -> profiling -> PII
    classification -> rule recommendation -> SQL generation -> SQL
    validation -> rule test execution) for one table, persist the profile
    and every tested rule to the app DB, and return them -- each rule now
    carries a real rule_id (from store_recommended_rule()) plus
    test_status/test_result alongside validation_status, so the
    Recommended Rules Page can act on it (approve/reject/edit) immediately.

    Creates its own scan run, tying every persisted row to a real SCAN_ID.
    Every rule is stored regardless of status -- including INVALID/ERROR
    ones -- so nothing Claude recommends is silently hidden from the
    approval screen; validation_errors (not a RECOMMENDED_INSTANCES column)
    rides along on the response only, not in storage.

    Raises on failure (marking the scan FAILED first) rather than catching
    -- callers decide how to surface that (the single-table HTTP route:
    500; the schema-scan loop below: record per-table and continue with
    the next table; the scheduler's RESCAN job: logged and skipped, same
    per-table isolation).
    """
    scan_id = create_scan_run(
        scan_name=f"Recommend rules: {database_name}.{schema_name}.{table_name}",
        target_database=database_name,
        target_schema=schema_name,
        target_table=table_name,
    )
    try:
        update_scan_status(scan_id, status="RUNNING", current_step="RECOMMENDING_RULES")
        result = run_dq_workflow(scan_id, database_name, schema_name, table_name)

        store_profile_result(
            scan_id,
            database_name,
            schema_name,
            table_name,
            {"table": result["table_profile"], "columns": result["column_profiles"]},
        )

        # Rules already pending for this table -- re-associate (update) any
        # matching pending rule with the new scan's results rather than
        # inserting a duplicate. This preserves the rule_id so the approval
        # queue stays clean across re-scans while always reflecting the latest
        # analysis. A match is on (table, column, rule_type) -- same as before.
        existing_pending = get_pending_rule_fingerprints(database_name, schema_name, table_name)

        stored_rules = []
        for rule in result["tested_rules"]:
            fingerprint = (rule.get("table_name"), rule.get("column_name"), rule.get("rule_type"))
            existing_rule_id = existing_pending.get(fingerprint)

            explanation = run_rule_explanation_agent(rule)

            v_status = _VALIDATION_STATUS_MAP.get(rule.get("validation_status"), "PENDING")
            t_status = _TEST_STATUS_MAP.get(rule.get("test_status"), "PENDING")

            if existing_rule_id is not None:
                refresh_pending_rule(
                    rule_id=existing_rule_id,
                    scan_id=scan_id,
                    description=rule.get("description"),
                    reason=rule.get("reason"),
                    evidence=rule.get("evidence"),
                    severity=rule["severity"],
                    confidence=rule["confidence"],
                    priority=rule["priority"],
                    threshold_config=rule.get("threshold_config"),
                    generated_sql=rule.get("generated_sql"),
                    validation_status=v_status,
                    test_status=t_status,
                    test_result=rule.get("test_result"),
                    rule_fingerprint=rule.get("rule_fingerprint"),
                    business_explanation=explanation.get("business_explanation"),
                    business_impact=explanation.get("business_impact"),
                    false_positive_risk=explanation.get("false_positive_risk"),
                )
                rule_id = existing_rule_id
            else:
                rule_id = store_recommended_rule(
                    scan_id=scan_id,
                    rule_name=rule["rule_name"],
                    rule_type=rule["rule_type"],
                    database_name=rule["database_name"],
                    schema_name=rule["schema_name"],
                    table_name=rule["table_name"],
                    column_name=rule.get("column_name"),
                    description=rule.get("description"),
                    reason=rule.get("reason"),
                    evidence=rule.get("evidence"),
                    severity=rule["severity"],
                    confidence=rule["confidence"],
                    priority=rule["priority"],
                    threshold_config=rule.get("threshold_config"),
                    generated_sql=rule.get("generated_sql"),
                    validation_status=v_status,
                    test_status=t_status,
                    test_result=rule.get("test_result"),
                    rule_fingerprint=rule.get("rule_fingerprint"),
                    business_explanation=explanation.get("business_explanation"),
                    business_impact=explanation.get("business_impact"),
                    false_positive_risk=explanation.get("false_positive_risk"),
                )
            stored_rules.append(
                {
                    **rule,
                    "rule_id": rule_id,
                    "approval_status": "PENDING",
                    **explanation,
                }
            )

        update_scan_status(scan_id, status="COMPLETED", progress_percentage=100, mark_ended=True)
        log_agent_run(
            scan_id,
            "main",
            "AWAITING_APPROVAL",
            "COMPLETED",
            message=f"Awaiting approval ({len(stored_rules)} rules)",
        )
        return {
            "scan_id": scan_id,
            "metadata": result["metadata"],
            "column_profiles": result["column_profiles"],
            "table_profile": result["table_profile"],
            "recommended_rules": stored_rules,
            "table_classification": result.get("table_classification"),
            "errors": result["errors"],
        }
    except Exception as exc:
        update_scan_status(scan_id, status="FAILED", error_message=str(exc), mark_ended=True)
        raise


def recommend_rules_for_tables(
    database_name: str, schema_name: str, table_names: list[str]
) -> list[dict[str, Any]]:
    """Loop recommend_rules_for_table() over a given list of tables in one
    schema, sequentially, inside the caller's request/job. Shared by the
    schema-scope, selected-tables-scope, and full-database-scope HTTP
    routes, and by the scheduler's RESCAN job -- all of them are "run the
    per-table workflow over N tables," differing only in how table_names
    is produced.

    One table's failure does not abort the rest: recorded in that table's
    "error" field and the loop continues -- same don't-abort-on-one-failure
    convention as DQWorkflowState's per-node error capture
    (graphs/dq_workflow_graph.py) and alert_agent.py's per-rule handling,
    just applied at the per-table level here.
    """
    table_results = []
    for table_name in table_names:
        try:
            result = recommend_rules_for_table(database_name, schema_name, table_name)
            table_results.append(
                {
                    "table_name": table_name,
                    "scan_id": result["scan_id"],
                    "rule_count": len(result["recommended_rules"]),
                    "error": None,
                }
            )
        except Exception as exc:
            table_results.append(
                {"table_name": table_name, "scan_id": None, "rule_count": 0, "error": str(exc)}
            )
    return table_results


def stream_recommend_rules_for_tables(
    database_name: str, schema_name: str, table_names: list[str]
) -> Iterator[str]:
    """Streaming version of recommend_rules_for_table() -- yields one
    newline-delimited JSON (NDJSON) line per table event so the frontend
    can update the UI progressively instead of waiting for the entire scan
    to finish.

    Two event types:
      {"event":"started",   "table_name": str}
      {"event":"completed", "table_name": str, "scan_id": str|null,
       "rule_count": int, "error": str|null}

    The "started" event is emitted immediately before running each table's
    workflow, giving the frontend a `currentTableName` signal with zero
    polling latency. The "completed" event carries the real scan_id so the
    frontend can fetch that table's logs directly -- no latest-scan polling
    needed for completed tables.

    Same one-table-failure-doesn't-abort-the-rest convention as
    recommend_rules_for_tables() -- errors land in the completed event's
    "error" field, not as a stream abort.
    """
    for table_name in table_names:
        yield json.dumps({"event": "started", "table_name": table_name}) + "\n"
        try:
            result = recommend_rules_for_table(database_name, schema_name, table_name)
            yield json.dumps({
                "event": "completed",
                "table_name": table_name,
                "scan_id": result["scan_id"],
                "rule_count": len(result["recommended_rules"]),
                "error": None,
            }) + "\n"
        except Exception as exc:
            yield json.dumps({
                "event": "completed",
                "table_name": table_name,
                "scan_id": None,
                "rule_count": 0,
                "error": str(exc),
            }) + "\n"


def run_all_approved_rules() -> list[dict]:
    """Run every approved rule now -- the shared loop behind both the
    manual "Run Rules" bulk HTTP route and scheduler.py's periodic
    RULE_EXECUTION job (extracted so the scheduler doesn't duplicate this
    logic; same per-rule agent, same don't-abort-on-one-failure loop,
    whether triggered by a click or a timer).

    Loops run_rule_execution_agent() (unchanged, same per-rule fetch ->
    revalidate -> execute -> store history -> alert-if-failed) over every
    row from list_approved_rules(). Inactive rules are included in the loop
    (not filtered out here) because run_rule_execution_agent() already has
    its own IS_ACTIVE check and reports SKIPPED for them -- that distinction
    (attempted-and-skipped vs. never-listed) is worth keeping visible in the
    response rather than silently excluding inactive rules before the agent
    ever sees them.

    Auto-resolve (architecture.md §8: "a passing rule auto-clears its alert
    on a later run") lives here: on a real PASSED result, look up that
    rule's most recent OPEN alert (get_open_alert_for_rule()) and transition
    it to RESOLVED. This was always update_alert_status()'s own documented
    intent, but had no caller until a real recurring trigger (this
    function, now also called by the scheduler) existed to produce the
    "later run" architecture.md describes -- a single manual "Run now"
    click was never that, since a human clicking it isn't "time passing,"
    it's the same kind of one-off check a scan already does.

    One rule's failure to run does not abort the batch -- same
    don't-abort-on-one-failure convention as every other loop in this
    codebase. Returns per-rule results so a caller can show a summary
    (N passed / N failed / N error / N skipped) after one call.
    """
    rules = list_approved_rules()

    results = []
    for rule in rules:
        try:
            result = run_rule_execution_agent(rule["rule_id"])
            if result.get("status") == "PASSED":
                try:
                    open_alert = get_open_alert_for_rule(rule["rule_id"])
                    if open_alert is not None:
                        update_alert_status(open_alert["alert_id"], "RESOLVED")
                except Exception:  # noqa: BLE001 -- auto-resolve must not fail the run itself
                    pass
            results.append({"rule_id": rule["rule_id"], "rule_name": rule["rule_name"], **result})
        except Exception as exc:
            results.append(
                {
                    "rule_id": rule["rule_id"],
                    "rule_name": rule["rule_name"],
                    "status": "ERROR",
                    "error_message": str(exc),
                }
            )
    return results


def recommend_instances_for_table(database_name: str, schema_name: str, table_name: str) -> dict[str, Any]:
    """Library-aware sibling of recommend_rules_for_table() (docs/rules-
    architecture.md §5.9/§5.10): runs metadata -> profiling -> PII directly
    (not via graphs/dq_workflow_graph.py, which still wires the OLD
    run_rule_recommendation_agent() -- touching that graph is out of scope
    here, so this function duplicates its first 3 nodes inline rather than
    risking the still-live single-table recommend-rules route), then calls
    agents.rule_recommendation_agent.run_instance_recommendation_agent()
    and persists the result via the §4.7 fingerprint dedup priority order
    (skip already-active instances -- already excluded by the recommendation
    agent itself; refresh a PENDING match; otherwise insert new).

    Same create_scan_run/update_scan_status/raise-on-failure shape as
    recommend_rules_for_table() -- see that function's docstring.
    """
    scan_id = create_scan_run(
        scan_name=f"Recommend instances: {database_name}.{schema_name}.{table_name}",
        target_database=database_name,
        target_schema=schema_name,
        target_table=table_name,
    )
    try:
        update_scan_status(scan_id, status="RUNNING", current_step="RECOMMENDING_INSTANCES")

        metadata_result = run_metadata_agent(database_name, schema_name, table_name)
        profiling_result = run_profiling_agent(
            database_name, schema_name, table_name, metadata=metadata_result["metadata"]
        )
        pii_result = run_pii_agent(
            database_name, schema_name, table_name, profiling_result["column_profiles"]
        )
        column_profiles = pii_result["column_profiles"]
        table_profile = profiling_result["table"]

        store_profile_result(
            scan_id, database_name, schema_name, table_name,
            {"table": table_profile, "columns": column_profiles},
        )

        row_count = table_profile.get("row_count", 0) or 0
        reco_result = run_instance_recommendation_agent(
            database_name, schema_name, table_name, row_count, column_profiles
        )

        pending_by_fingerprint = get_pending_instance_by_fingerprint(database_name, schema_name, table_name)

        stored_instances = []
        for instance in reco_result["recommended_instances"]:
            fingerprint = instance.get("rule_fingerprint")
            column_name = (instance.get("target_config") or {}).get("column") if instance.get("scope") == "COLUMN" else None

            # Adaptation for run_rule_explanation_agent(), which was written
            # for the old flat column_name shape (§5.10's persist-time note):
            # lossy for MULTI_COLUMN/TABLE/CROSS_TABLE/CONDITIONAL scopes,
            # which have no single column_name to give it.
            explanation_input = {**instance, "column_name": column_name}
            explanation = run_rule_explanation_agent(explanation_input)

            existing_rule_id = pending_by_fingerprint.get(fingerprint) if fingerprint else None

            if existing_rule_id is not None:
                refresh_pending_rule(
                    rule_id=existing_rule_id,
                    scan_id=scan_id,
                    description=instance.get("description"),
                    reason=instance.get("reason"),
                    evidence=instance.get("evidence"),
                    severity=instance["severity"],
                    confidence=instance["confidence"],
                    priority=instance["priority"],
                    threshold_config=instance.get("threshold_config"),
                    generated_sql=instance.get("generated_sql"),
                    validation_status="PENDING",
                    test_status="PENDING",
                    test_result=None,
                    rule_fingerprint=fingerprint,
                    business_explanation=explanation.get("business_explanation"),
                    business_impact=explanation.get("business_impact"),
                    false_positive_risk=explanation.get("false_positive_risk"),
                    scope=instance.get("scope"),
                    target_config=instance.get("target_config"),
                    definition_id=instance.get("definition_id"),
                    is_new_definition=instance.get("is_new_definition"),
                    proposed_definition=instance.get("proposed_definition"),
                    suggested_group_id=instance.get("suggested_group_id"),
                )
                rule_id = existing_rule_id
            else:
                # Fingerprint-less (staged new-definition) candidates always
                # insert -- never deduped/refreshed (§5.4's rework agent
                # note: each scan re-proposing the same new-definition
                # concept is an accepted limitation of this phase).
                rule_id = store_recommended_rule(
                    scan_id=scan_id,
                    rule_name=instance.get("rule_name"),
                    rule_type=instance.get("rule_type"),
                    database_name=database_name,
                    schema_name=schema_name,
                    table_name=table_name,
                    column_name=column_name,
                    description=instance.get("description"),
                    reason=instance.get("reason"),
                    evidence=instance.get("evidence"),
                    severity=instance["severity"],
                    confidence=instance["confidence"],
                    priority=instance["priority"],
                    threshold_config=instance.get("threshold_config"),
                    generated_sql=instance.get("generated_sql"),
                    validation_status="PENDING",
                    test_status="PENDING",
                    rule_fingerprint=fingerprint,
                    business_explanation=explanation.get("business_explanation"),
                    business_impact=explanation.get("business_impact"),
                    false_positive_risk=explanation.get("false_positive_risk"),
                    scope=instance.get("scope"),
                    target_config=instance.get("target_config"),
                    definition_id=instance.get("definition_id"),
                    is_new_definition=instance.get("is_new_definition"),
                    proposed_definition=instance.get("proposed_definition"),
                    suggested_group_id=instance.get("suggested_group_id"),
                )

            stored_instances.append({**instance, "rule_id": rule_id, "approval_status": "PENDING", **explanation})

        update_scan_status(scan_id, status="COMPLETED", progress_percentage=100, mark_ended=True)
        log_agent_run(
            scan_id, "main", "AWAITING_APPROVAL", "COMPLETED",
            message=f"Awaiting approval ({len(stored_instances)} instances)",
        )
        return {
            "scan_id": scan_id,
            "metadata": metadata_result["metadata"],
            "column_profiles": column_profiles,
            "table_profile": table_profile,
            "recommended_instances": stored_instances,
            "table_classification": reco_result.get("table_classification"),
            "new_definitions_staged": reco_result.get("new_definitions_staged", []),
            "errors": [],
        }
    except Exception as exc:
        update_scan_status(scan_id, status="FAILED", error_message=str(exc), mark_ended=True)
        raise
