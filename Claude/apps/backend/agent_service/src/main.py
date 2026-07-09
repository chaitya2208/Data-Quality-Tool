"""FastAPI app: thin HTTP layer over our Snowflake tools, for the React UI.

Run from repo root:
    ./.venv/Scripts/python.exe -m uvicorn apps.backend.agent_service.src.main:app --reload --port 8000

Routes wrap tools/snowflake_metadata_tools.py directly -- no new query logic
lives here, only request/response shaping and error handling.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from contextlib import asynccontextmanager

from tools.langsmith_tools import configure_langsmith_tracing
from tools.snowflake_connection import get_app_connection, get_source_connection, is_source_connected, run_app_query, run_query
from tools.snowflake_metadata_tools import (
    describe_table,
    list_databases,
    list_schemas,
    list_tables,
)
from tools.snowflake_profiling_tools import profile_and_store_table
from tools.sql_validation_tools import validate_sql
from tools.storage_tools import (
    create_rule_definition,
    create_scan_run,
    create_scan_schedule,
    get_alert,
    get_alerts_summary,
    count_pending_rules,
    ensure_system_rule_definitions_seeded,
    get_approved_rule,
    get_latest_scan_id,
    get_recommended_instances_pending_by_group,
    get_recommended_rule,
    get_rule_definition,
    get_scan_schedule,
    increment_rule_definition_approval_count,
    increment_rule_definition_instance_count,
    list_agent_run_logs,
    list_alerts,
    list_approved_rules,
    list_execution_history,
    list_recommended_rules,
    list_recommended_rules_summary,
    list_rule_definitions,
    list_rule_groups,
    list_scan_runs,
    list_scan_schedules,
    list_table_health,
    log_agent_run,
    set_rule_definition_status,
    set_rule_instance_active,
    set_scan_schedule_active,
    store_approved_rule,
    store_rejected_rule,
    store_user_feedback,
    update_alert_status,
    update_recommended_rule,
    update_scan_status,
)
from agents.rule_execution_agent import run_rule_execution_agent
from scan_operations import (
    _VALIDATION_STATUS_MAP,
    recommend_instances_for_table,
    recommend_rules_for_table,
    recommend_rules_for_tables,
    run_all_approved_rules,
    stream_recommend_rules_for_tables,
)
from scheduler import shutdown_scheduler, start_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Self-heal the SYSTEM rule library on boot -- e.g. after a fresh-start
    # TRUNCATE of RULES.RULE_DEFINITIONS, so the app doesn't need a manual
    # migration re-run before the next scan can recommend anything. No-ops
    # (one lightweight COUNT query) when the library is already seeded.
    try:
        ensure_system_rule_definitions_seeded()
    except Exception as exc:  # noqa: BLE001 -- must not block startup
        print(f"[main] Could not verify/reseed SYSTEM rule definitions on startup: {exc}")
    start_scheduler()
    yield
    shutdown_scheduler()


app = FastAPI(title="Agentic DQ Platform API", lifespan=lifespan)

# Sets a process-wide LangSmith client (with this network's corporate-proxy
# TLS fix applied) so every graphs/dq_workflow_graph.py invoke() and every
# Claude/Bedrock call (tools/claude_tools.py) traces automatically -- see
# tools/langsmith_tools.py. No-ops if LANGSMITH_API_KEY isn't set.
configure_langsmith_tracing()

# _VALIDATION_STATUS_MAP/_TEST_STATUS_MAP now live in scan_operations.py
# (imported above) since recommend_rules_for_table() -- the only place that
# used them -- moved there too. Still imported here because edit_rule()
# below (PATCH /api/rules/{rule_id}/edit) also needs _VALIDATION_STATUS_MAP
# when re-validating edited SQL.

# Dev-only: Vite's default port. Tighten before anything beyond localhost.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/connection/status")
def connection_status() -> dict:
    """Return whether the source connection is already open — does NOT trigger SSO.
    Use POST /api/connection/connect to initiate the SSO flow."""
    if not is_source_connected():
        return {"connected": False}
    try:
        rows = run_query(
            "SELECT CURRENT_USER() AS user, CURRENT_ROLE() AS role, "
            "CURRENT_WAREHOUSE() AS warehouse, CURRENT_ACCOUNT() AS account"
        )
        return {"connected": True, **rows[0]}
    except Exception:
        return {"connected": False}


@app.post("/api/connection/connect")
def connection_connect() -> dict:
    """Initiate the SSO browser flow.

    Opens the source connection first (browser SSO). The token is then
    cached by the Snowflake connector, so the immediately-following app
    connection hits the local credential store and never opens a second
    browser tab.
    """
    try:
        get_source_connection()
        # Prime the app connection while the SSO token is hot in the cache.
        # This must happen on the same thread, serially, so the token written
        # by the source connect() is readable before app connect() checks it.
        get_app_connection()
        rows = run_query(
            "SELECT CURRENT_USER() AS user, CURRENT_ROLE() AS role, "
            "CURRENT_WAREHOUSE() AS warehouse, CURRENT_ACCOUNT() AS account"
        )
        return {"connected": True, **rows[0]}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/databases")
def get_databases() -> list[dict]:
    try:
        return list_databases()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/databases/{database_name}/schemas")
def get_schemas(database_name: str) -> list[dict]:
    try:
        return list_schemas(database_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/databases/{database_name}/schemas/{schema_name}/tables")
def get_tables(database_name: str, schema_name: str) -> list[dict]:
    try:
        return list_tables(database_name, schema_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get(
    "/api/databases/{database_name}/schemas/{schema_name}/tables/{table_name}/columns"
)
def get_columns(database_name: str, schema_name: str, table_name: str) -> list[dict]:
    try:
        return describe_table(database_name, schema_name, table_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post(
    "/api/databases/{database_name}/schemas/{schema_name}/tables/{table_name}/profile"
)
def profile_table(database_name: str, schema_name: str, table_name: str) -> dict:
    """Run profiling for one table (full scan, MVP1 -- no sampling yet) and
    store the results. Creates its own scan run so the profile is tied to a
    SCAN_ID, same as a real Data Profiling Agent run would be.
    """
    scan_id = create_scan_run(
        scan_name=f"Manual profile: {database_name}.{schema_name}.{table_name}",
        target_database=database_name,
        target_schema=schema_name,
        target_table=table_name,
    )
    try:
        update_scan_status(scan_id, status="RUNNING", current_step="PROFILING")
        result = profile_and_store_table(scan_id, database_name, schema_name, table_name)
        update_scan_status(scan_id, status="COMPLETED", progress_percentage=100, mark_ended=True)
        return {"scan_id": scan_id, **result}
    except ValueError as exc:
        update_scan_status(scan_id, status="FAILED", error_message=str(exc), mark_ended=True)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        update_scan_status(scan_id, status="FAILED", error_message=str(exc), mark_ended=True)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/databases/{database_name}/schemas/{schema_name}/latest-scan")
def get_latest_scan(database_name: str, schema_name: str, table_name: str | None = None) -> dict:
    """Look up the most recently started scan for this target (table-scoped
    if table_name is given, schema-scoped umbrella scan otherwise). Lets the
    frontend discover a scan's SCAN_ID and start polling /logs before the
    long-running recommend-rules POST that kicked it off has returned --
    see get_latest_scan_id()'s docstring for why this lookup, rather than
    just waiting on the POST's response, is needed.
    """
    try:
        scan_id, target_table = get_latest_scan_id(database_name, schema_name, table_name)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"scan_id": scan_id, "target_table": target_table}


@app.get("/api/scans")
def get_all_scans(
    limit: int = 100,
    database_name: str | None = None,
    schema_name: str | None = None,
) -> list[dict]:
    """List recent scan runs, newest first."""
    try:
        return list_scan_runs(limit=limit, database_name=database_name, schema_name=schema_name)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/scans/{scan_id}/logs")
def get_scan_logs(scan_id: str) -> list[dict]:
    """Agent-run activity log for one scan, oldest first -- the "what is the
    agent doing right now" progress feed (README requirement). Backed by
    LOGS.AGENT_RUN_LOGS, written to by log_agent_run() calls sprinkled
    through graphs/dq_workflow_graph.py's nodes and this file's
    recommend-rules routes. Returns [] for a scan_id with no logs yet
    (scan not started, or a scan_id from before this feature existed) --
    not a 404, since "no logs yet" is a valid, common state to poll during.
    """
    try:
        return list_agent_run_logs(scan_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post(
    "/api/databases/{database_name}/schemas/{schema_name}/tables/{table_name}/recommend-rules"
)
def recommend_rules(database_name: str, schema_name: str, table_name: str) -> dict:
    """Scan scope: one table. See scan_operations.recommend_rules_for_table()
    for the full workflow; this route just converts a raised exception
    into a 500.
    """
    try:
        return recommend_rules_for_table(database_name, schema_name, table_name)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post(
    "/api/databases/{database_name}/schemas/{schema_name}/tables/{table_name}/recommend-instances"
)
def recommend_instances(database_name: str, schema_name: str, table_name: str) -> dict:
    """New-pipeline sibling of recommend_rules() above: scope/target_config/
    definition_id-aware, library-aware Claude recommendation (docs/rules-
    architecture.md §5.4). See scan_operations.recommend_instances_for_table().
    The old recommend-rules route above is untouched -- both exist side by
    side.
    """
    try:
        return recommend_instances_for_table(database_name, schema_name, table_name)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/databases/{database_name}/schemas/{schema_name}/recommend-rules")
def recommend_rules_for_schema(database_name: str, schema_name: str) -> dict:
    """Scan scope: one schema. Loops recommend_rules_for_tables() over
    every table in the schema, sequentially, inside this one request -- no
    background job queue exists in this codebase yet (see
    docs/deferred-and-future-work.md), so this is a synchronous, potentially
    long-running call, same pattern as the single-table route just repeated.

    Each table gets its own real SCAN_ID (via _recommend_rules_for_table's
    create_scan_run() call) -- there is no parent/child scan grouping
    column in SCAN_RUNS, so this route also creates one umbrella scan run
    (TARGET_TABLE=NULL) purely to represent "a schema-scoped scan was
    kicked off" in SCAN_RUNS; the per-table results live under their own
    scan_ids, and the response lists each one so the frontend can fetch
    GET /api/rules/recommended?scan_id=... per table and aggregate.
    """
    umbrella_scan_id = create_scan_run(
        scan_name=f"Recommend rules (schema scan): {database_name}.{schema_name}",
        target_database=database_name,
        target_schema=schema_name,
    )
    try:
        tables = list_tables(database_name, schema_name)
    except ValueError as exc:
        update_scan_status(umbrella_scan_id, status="FAILED", error_message=str(exc), mark_ended=True)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        update_scan_status(umbrella_scan_id, status="FAILED", error_message=str(exc), mark_ended=True)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    update_scan_status(umbrella_scan_id, status="RUNNING", current_step="RECOMMENDING_RULES")
    table_results = recommend_rules_for_tables(database_name, schema_name, [t["name"] for t in tables])

    failed_count = sum(1 for t in table_results if t["error"] is not None)
    update_scan_status(
        umbrella_scan_id,
        status="FAILED" if failed_count == len(table_results) and table_results else "COMPLETED",
        progress_percentage=100,
        mark_ended=True,
    )
    return {
        "scan_id": umbrella_scan_id,
        "database_name": database_name,
        "schema_name": schema_name,
        "tables": table_results,
    }


@app.post("/api/databases/{database_name}/schemas/{schema_name}/recommend-rules/stream")
def recommend_rules_for_schema_stream(database_name: str, schema_name: str) -> StreamingResponse:
    """Streaming version of recommend_rules_for_schema(). Returns an NDJSON
    stream: one {"event":"started","table_name":...} line immediately before
    each table's workflow begins, and one {"event":"completed",...,"scan_id":...}
    line as soon as it finishes. The frontend reads these as they arrive so
    it can show per-table progress (and real scan_ids for completed tables)
    without waiting for the entire scan to complete.

    No umbrella scan_id is returned -- the frontend receives per-table
    scan_ids directly from the "completed" events, which is cleaner than
    polling latest-scan and avoids the umbrella-scan/TARGET_TABLE-IS-NULL
    confusion the old blocking route created.
    """
    try:
        tables = list_tables(database_name, schema_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    table_names = [t["name"] for t in tables]
    return StreamingResponse(
        stream_recommend_rules_for_tables(database_name, schema_name, table_names),
        media_type="application/x-ndjson",
    )


class SelectedTablesRequest(BaseModel):
    table_names: list[str]


@app.post("/api/databases/{database_name}/schemas/{schema_name}/recommend-rules-selected/stream")
def recommend_rules_for_selected_tables_stream(
    database_name: str, schema_name: str, body: SelectedTablesRequest
) -> StreamingResponse:
    """Streaming version of recommend_rules_for_selected_tables(). Same
    NDJSON event protocol as recommend_rules_for_schema_stream() above.
    """
    if not body.table_names:
        raise HTTPException(status_code=400, detail="table_names must not be empty")
    return StreamingResponse(
        stream_recommend_rules_for_tables(database_name, schema_name, body.table_names),
        media_type="application/x-ndjson",
    )


@app.post("/api/databases/{database_name}/schemas/{schema_name}/recommend-rules-selected")
def recommend_rules_for_selected_tables(
    database_name: str, schema_name: str, body: SelectedTablesRequest
) -> dict:
    """Scan scope: a user-picked subset of tables within one schema. Same
    umbrella-scan + per-table-loop shape as recommend_rules_for_schema(),
    just over body.table_names instead of every table SHOW TABLES returns.
    """
    if not body.table_names:
        raise HTTPException(status_code=400, detail="table_names must not be empty")

    umbrella_scan_id = create_scan_run(
        scan_name=f"Recommend rules (selected tables): {database_name}.{schema_name}",
        target_database=database_name,
        target_schema=schema_name,
    )
    update_scan_status(umbrella_scan_id, status="RUNNING", current_step="RECOMMENDING_RULES")
    table_results = recommend_rules_for_tables(database_name, schema_name, body.table_names)

    failed_count = sum(1 for t in table_results if t["error"] is not None)
    update_scan_status(
        umbrella_scan_id,
        status="FAILED" if failed_count == len(table_results) and table_results else "COMPLETED",
        progress_percentage=100,
        mark_ended=True,
    )
    return {
        "scan_id": umbrella_scan_id,
        "database_name": database_name,
        "schema_name": schema_name,
        "tables": table_results,
    }


@app.get("/api/databases/{database_name}/scan-preview")
def get_database_scan_preview(database_name: str) -> dict:
    """Row-count preview for a full-database scan, so the frontend can warn
    before kicking one off: every table across every schema (except
    INFORMATION_SCHEMA -- its "tables" are metadata views, not real data)
    is a full-table-scan profiling pass since sampling isn't built yet (see
    docs/deferred-and-future-work.md #4) -- a database with a 31M-row table
    could turn "click one button" into a multi-hour run. Returns per-schema
    and total table/row counts; the frontend decides what to show/confirm.
    """
    try:
        schemas = [s for s in list_schemas(database_name) if s["name"] != "INFORMATION_SCHEMA"]
        per_schema = []
        total_tables = 0
        total_rows = 0
        for schema in schemas:
            tables = list_tables(database_name, schema["name"])
            schema_rows = sum(t["row_count"] or 0 for t in tables)
            per_schema.append(
                {
                    "schema_name": schema["name"],
                    "table_count": len(tables),
                    "row_count": schema_rows,
                }
            )
            total_tables += len(tables)
            total_rows += schema_rows
        return {
            "database_name": database_name,
            "total_tables": total_tables,
            "total_rows": total_rows,
            "schemas": per_schema,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/databases/{database_name}/recommend-rules")
def recommend_rules_for_database(database_name: str) -> dict:
    """Scan scope: every schema, every table, in one selected database (not
    account-wide -- scanning across multiple databases stays out of scope,
    see docs/deferred-and-future-work.md). Loops
    recommend_rules_for_schema()'s same per-table-loop logic one level up,
    over every non-INFORMATION_SCHEMA schema in the database, sequentially,
    inside this one request -- same synchronous, no-job-queue caveat as
    every other scan-scope route here.

    A schema's own failure to list tables (e.g. a schema this role can't
    read) is recorded like a single failed "table" entry rather than
    aborting the rest of the database.
    """
    umbrella_scan_id = create_scan_run(
        scan_name=f"Recommend rules (full database scan): {database_name}",
        target_database=database_name,
    )
    try:
        schemas = [s for s in list_schemas(database_name) if s["name"] != "INFORMATION_SCHEMA"]
    except Exception as exc:
        update_scan_status(umbrella_scan_id, status="FAILED", error_message=str(exc), mark_ended=True)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    update_scan_status(umbrella_scan_id, status="RUNNING", current_step="RECOMMENDING_RULES")
    schema_results = []
    for schema in schemas:
        schema_name = schema["name"]
        try:
            tables = list_tables(database_name, schema_name)
            table_results = recommend_rules_for_tables(
                database_name, schema_name, [t["name"] for t in tables]
            )
            schema_results.append({"schema_name": schema_name, "tables": table_results, "error": None})
        except Exception as exc:
            schema_results.append({"schema_name": schema_name, "tables": [], "error": str(exc)})

    all_tables = [t for s in schema_results for t in s["tables"]]
    failed_count = sum(1 for t in all_tables if t["error"] is not None) + sum(
        1 for s in schema_results if s["error"] is not None
    )
    update_scan_status(
        umbrella_scan_id,
        status="FAILED" if failed_count > 0 and failed_count == len(all_tables) + len(schema_results) else "COMPLETED",
        progress_percentage=100,
        mark_ended=True,
    )
    return {
        "scan_id": umbrella_scan_id,
        "database_name": database_name,
        "schemas": schema_results,
    }


# ---------------------------------------------------------------------------
# Human Approval: recommended rules -> approve / reject / edit
# ---------------------------------------------------------------------------


class RejectRuleRequest(BaseModel):
    reason: str | None = None
    rejected_by: str | None = None


class EditRuleRequest(BaseModel):
    severity: str | None = None
    threshold_config: dict | None = None
    generated_sql: str | None = None


class ApproveRuleRequest(BaseModel):
    approved_by: str | None = None


@app.get("/api/rules/recommended")
def get_recommended_rules(scan_id: str | None = None) -> list[dict]:
    """List recommended rules filtered to one scan (scan_id required for full
    history). Without scan_id returns only PENDING rules to avoid large result
    sets that trigger Snowflake S3 streaming failures.
    """
    try:
        return list_recommended_rules(scan_id=scan_id, pending_only=(scan_id is None))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/rules/recommended/summary")
def get_recommended_rules_summary(pending_only: bool = False, scan_id: str | None = None) -> list[dict]:
    """Lightweight list of recommended rules. Returns only scalar columns to avoid
    Snowflake S3 streaming. Full detail is fetched per-rule on demand."""
    try:
        return list_recommended_rules_summary(pending_only=pending_only, scan_id=scan_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/rules/pending-count")
def get_pending_rules_count() -> dict:
    """Return the count of PENDING recommended rules. Lightweight COUNT(*)
    query used by the dashboard — avoids fetching all rule rows which can
    be large enough to trigger Snowflake S3 result streaming failures.
    """
    try:
        return {"count": count_pending_rules()}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/rules/recommended/{rule_id}")
def get_recommended_rule_detail(rule_id: str) -> dict:
    """One recommended rule's full detail, for the approval screen's
    "View details" action.
    """
    try:
        rule = get_recommended_rule(rule_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if rule is None:
        raise HTTPException(status_code=404, detail=f"Rule {rule_id!r} not found")
    return rule


def _require_pending_rule(rule_id: str) -> dict:
    """Shared guard for approve/reject/edit: the rule must exist and not
    already be approved/rejected. A rule's presence in RULE_INSTANCES/
    REJECTED_INSTANCES is a one-way transition in this codebase (no "undo"
    route exists, per architecture.md #8's approval flow) -- so a second
    approve/reject/edit on an already-decided rule is a conflict, not a
    silent no-op or an overwrite.
    """
    rule = get_recommended_rule(rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail=f"Rule {rule_id!r} not found")
    if rule["approval_status"] != "PENDING":
        raise HTTPException(
            status_code=409,
            detail=f"Rule {rule_id!r} is already {rule['approval_status']}",
        )
    return rule


def _approve_one(rule: dict, approved_by: str | None) -> dict:
    """Shared approve body -- extracted so both the single-rule route and
    the bulk-approve route (§6.2) run identical logic. Only a rule whose
    current generated_sql passed validation (validation_status == "PASSED",
    storage's vocabulary -- see _VALIDATION_STATUS_MAP) can be approved:
    architecture.md's SQL Validator is the mandatory hard gate before
    anything is allowed to run, and an approved rule is exactly the thing
    the Rule Execution Agent will run unattended. Raises HTTPException(400)
    if not PASSED -- the caller (single-rule route) lets that propagate;
    the bulk route catches it and reports the instance as skipped instead.

    §6.1 definition graduation: when the rule is a staged new-definition
    (is_new_definition truthy, proposed_definition set), the definition is
    inserted into RULE_DEFINITIONS here, at the moment of approval -- never
    earlier (§4.3.1: a PROPOSED definition that's never approved leaves no
    trace). Either way (new or existing definition), approval_count/
    instance_count are incremented -- best-effort, must not fail the
    approval itself.
    """
    if rule["validation_status"] != "PASSED":
        raise HTTPException(
            status_code=400,
            detail=(
                f"Rule {rule['rule_id']!r} has validation_status={rule['validation_status']!r}; "
                "only a rule with valid, safety-checked SQL can be approved. Edit it with "
                "valid SQL first."
            ),
        )

    definition_id = rule.get("definition_id")
    if rule.get("is_new_definition") and rule.get("proposed_definition"):
        proposed = rule["proposed_definition"]
        if isinstance(proposed, str):
            proposed = json.loads(proposed)
        definition_id = create_rule_definition(
            name=proposed.get("name"),
            category=proposed.get("category"),
            description=proposed.get("description") or "",
            check_logic=proposed.get("check_logic") or "",
            allowed_scopes=proposed.get("allowed_scopes") or [],
            default_severity=proposed.get("default_severity"),
            sql_template=proposed.get("draft_sql_template"),
            source="CLAUDE",
            status="ACTIVE",
        )

    approved_rule_id = store_approved_rule(
        original_recommended_rule_id=rule["rule_id"],
        rule_name=rule["rule_name"],
        rule_type=rule["rule_type"],
        database_name=rule["database_name"],
        schema_name=rule["schema_name"],
        table_name=rule["table_name"],
        column_name=rule["column_name"],
        severity=rule["severity"],
        threshold_config=rule["threshold_config"],
        rule_sql=rule["generated_sql"],
        approved_by=approved_by,
        scope=rule.get("scope"),
        target_config=rule.get("target_config"),
        definition_id=definition_id,
        group_id=rule.get("suggested_group_id"),
    )

    if definition_id:
        try:
            increment_rule_definition_approval_count(definition_id)
            increment_rule_definition_instance_count(definition_id)
        except Exception:  # noqa: BLE001 -- must not fail the approval itself
            pass

    return {"rule_id": approved_rule_id, "approval_status": "APPROVED"}


def _reject_one(rule: dict, reason: str | None, rejected_by: str | None) -> dict:
    """Shared reject body -- extracted so both the single-rule route and
    the bulk-reject route (§6.2) run identical logic.

    Feedback Loop (per the "Add Feedback Loop" ask): every rejection also
    writes a RULES.USER_FEEDBACK row keyed on (rule_type, database, schema,
    table, column) -- agents/rule_recommendation_agent.py's
    _apply_feedback() reads these back on the *next* scan of this table so
    the same rule isn't blindly re-suggested.
    """
    rejected_rule_id = store_rejected_rule(
        original_recommended_rule_id=rule["rule_id"],
        rejection_reason=reason,
        rejected_by=rejected_by,
    )
    store_user_feedback(
        feedback_type="REJECT",
        rule_type=rule["rule_type"],
        database_name=rule["database_name"],
        schema_name=rule["schema_name"],
        table_name=rule["table_name"],
        column_name=rule["column_name"],
        rule_id=rule["rule_id"],
        comment=reason,
        created_by=rejected_by,
    )
    return {"rule_id": rejected_rule_id, "approval_status": "REJECTED"}


@app.post("/api/rules/{rule_id}/approve")
def approve_rule(rule_id: str, body: ApproveRuleRequest) -> dict:
    """Approve -> copy the recommended rule into RULE_INSTANCES. See
    _approve_one() for the shared logic (also used by bulk-approve)."""
    rule = _require_pending_rule(rule_id)
    return _approve_one(rule, body.approved_by)


@app.post("/api/rules/{rule_id}/reject")
def reject_rule(rule_id: str, body: RejectRuleRequest) -> dict:
    """Reject -> store in REJECTED_INSTANCES with an optional reason. See
    _reject_one() for the shared logic (also used by bulk-reject).

    reason may be blank -- per the ask and storage_tools.store_rejected_rule()'s
    own docstring, a rejection without an explanation is still useful signal.
    REJECTED_INSTANCES is keyed on ORIGINAL_RECOMMENDED_RULE_ID, useful for
    "was this exact rule_id rejected," not "was a rule like this rejected
    for this column," which is what the USER_FEEDBACK row _reject_one()
    also writes is for.
    """
    rule = _require_pending_rule(rule_id)
    return _reject_one(rule, body.reason, body.rejected_by)


class BulkApproveRequest(BaseModel):
    group_id: str
    approved_by: str | None = None


class BulkRejectRequest(BaseModel):
    group_id: str
    reason: str | None = None
    rejected_by: str | None = None


@app.post("/api/rules/bulk-approve")
def bulk_approve_rules(body: BulkApproveRequest) -> dict:
    """Approve every PENDING instance in a group whose SQL already passed
    validation (§6.2: "not all-or-not-all-nothing" -- an instance with
    validation_status != PASSED is skipped and reported, not blocking the
    others).
    """
    pending = get_recommended_instances_pending_by_group(body.group_id)
    approved: list[str] = []
    skipped: list[dict] = []
    for rule in pending:
        if rule.get("validation_status") != "PASSED":
            skipped.append({"rule_id": rule["rule_id"], "reason": f"validation_status={rule.get('validation_status')!r}"})
            continue
        try:
            result = _approve_one(rule, body.approved_by)
            approved.append(result["rule_id"])
        except HTTPException as exc:
            skipped.append({"rule_id": rule["rule_id"], "reason": str(exc.detail)})
    return {"approved": approved, "skipped": skipped}


@app.post("/api/rules/bulk-reject")
def bulk_reject_rules(body: BulkRejectRequest) -> dict:
    """Reject every PENDING instance in a group with one shared reason
    (§6.2: "bulk reject only touches PENDING instances" -- already-approved
    members of the same group are untouched, since
    get_recommended_instances_pending_by_group() only returns PENDING ones).
    """
    pending = get_recommended_instances_pending_by_group(body.group_id)
    rejected = [
        _reject_one(rule, body.reason, body.rejected_by)["rule_id"] for rule in pending
    ]
    return {"rejected": rejected}


@app.patch("/api/rules/{rule_id}/deactivate")
def deactivate_rule_instance(rule_id: str) -> dict:
    """Flip IS_ACTIVE=FALSE on an approved instance (§6.3). No deletion --
    the instance is skipped at execution time (SKIPPED status in history).
    """
    if get_approved_rule(rule_id) is None:
        raise HTTPException(status_code=404, detail=f"Rule {rule_id!r} not found")
    set_rule_instance_active(rule_id, False)
    return get_approved_rule(rule_id)


@app.patch("/api/rules/{rule_id}/activate")
def activate_rule_instance(rule_id: str) -> dict:
    """Flip IS_ACTIVE=TRUE on an approved instance (§6.3)."""
    if get_approved_rule(rule_id) is None:
        raise HTTPException(status_code=404, detail=f"Rule {rule_id!r} not found")
    set_rule_instance_active(rule_id, True)
    return get_approved_rule(rule_id)


@app.get("/api/rules/definitions")
def get_rule_definitions(status: str | None = None, source: str | None = None) -> list[dict]:
    """List rule definitions -- the definitions-library view (§11)."""
    try:
        return list_rule_definitions(status=status, source=source)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


class CreateRuleDefinitionRequest(BaseModel):
    name: str
    category: str
    description: str
    check_logic: str
    allowed_scopes: list[str]
    parameters_schema: dict | None = None
    default_threshold_config: dict | None = None
    default_severity: str | None = None
    sql_template: str | None = None
    created_by: str | None = None


@app.post("/api/rules/definitions")
def create_rule_definition_route(body: CreateRuleDefinitionRequest) -> dict:
    """Create a USER-sourced rule definition directly (§11) -- distinct
    from the CLAUDE-sourced graduation path in _approve_one()."""
    try:
        definition_id = create_rule_definition(
            name=body.name,
            category=body.category,
            description=body.description,
            check_logic=body.check_logic,
            allowed_scopes=body.allowed_scopes,
            parameters_schema=body.parameters_schema,
            default_threshold_config=body.default_threshold_config,
            default_severity=body.default_severity,
            sql_template=body.sql_template,
            source="USER",
            status="ACTIVE",
            created_by=body.created_by,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return get_rule_definition(definition_id)


class UpdateRuleDefinitionStatusRequest(BaseModel):
    status: str


@app.patch("/api/rules/definitions/{definition_id}")
def update_rule_definition_status_route(definition_id: str, body: UpdateRuleDefinitionStatusRequest) -> dict:
    """Disable/re-enable a definition (§6.3). Existing live RULE_INSTANCES
    continue executing either way -- only future suggestion behavior reads
    this status."""
    if get_rule_definition(definition_id) is None:
        raise HTTPException(status_code=404, detail=f"Definition {definition_id!r} not found")
    if body.status not in ("ACTIVE", "DISABLED"):
        raise HTTPException(status_code=400, detail="status must be 'ACTIVE' or 'DISABLED'")
    set_rule_definition_status(definition_id, body.status)
    return get_rule_definition(definition_id)


@app.get("/api/rules/groups")
def get_rule_groups(
    database_name: str | None = None, schema_name: str | None = None, table_name: str | None = None
) -> list[dict]:
    """List rule groups (§11), optionally filtered by location."""
    try:
        return list_rule_groups(database_name=database_name, schema_name=schema_name, table_name=table_name)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/rules/groups/{group_id}/instances")
def get_rule_group_instances(group_id: str) -> dict:
    """Instances belonging to one group, split by approval_status (§6.4's
    grouped approval view). PENDING members come from
    get_recommended_instances_pending_by_group(); APPROVED members are
    looked up directly here (a query, not a new storage_tools function --
    this is the only caller today) via RULE_INSTANCES.GROUP_ID.
    """
    pending = get_recommended_instances_pending_by_group(group_id)
    approved_rows = run_app_query(
        "SELECT * FROM RULES.RULE_INSTANCES WHERE GROUP_ID = %(group_id)s ORDER BY APPROVED_AT DESC",
        {"group_id": group_id},
    )
    approved = [
        {
            "rule_id": row["RULE_ID"],
            "rule_name": row["RULE_NAME"],
            "rule_type": row["RULE_TYPE"],
            "database_name": row["DATABASE_NAME"],
            "schema_name": row["SCHEMA_NAME"],
            "table_name": row["TABLE_NAME"],
            "column_name": row["COLUMN_NAME"],
            "severity": row["SEVERITY"],
            "is_active": row["IS_ACTIVE"],
            "approved_at": row["APPROVED_AT"],
            "approval_status": "APPROVED",
        }
        for row in approved_rows
    ]
    return {"pending": pending, "approved": approved}


@app.patch("/api/rules/{rule_id}/edit")
def edit_rule(rule_id: str, body: EditRuleRequest) -> dict:
    """Edit -> update threshold/severity/SQL, then revalidate.

    Only generated_sql changes trigger revalidation (validate_sql(), the
    same mandatory hard gate sql_validation_agent.py runs during a scan) --
    a severity/threshold-only edit doesn't change the SQL's safety, so
    re-running the validator against unchanged SQL would be redundant. The
    revalidated validation_status is mapped into storage's vocabulary the
    same way the initial recommend-rules persistence does (_VALIDATION_STATUS_MAP),
    so a rule fixed via Edit becomes approvable the same way a rule that
    validated cleanly the first time is.

    Feedback Loop (per the "Add Feedback Loop" ask, "if user edited
    threshold, use that as future signal"): a threshold_config edit also
    writes a RULES.USER_FEEDBACK row (feedback_type="EDIT") carrying the
    *new* threshold_config value -- agents/rule_recommendation_agent.py's
    _apply_feedback() seeds a future same-column-same-type candidate's
    threshold_config from the most recent EDIT feedback, rather than the
    skill's own hardcoded default, on the theory that a human's edited
    value is a better starting point than the generic one. Only fires when
    threshold_config is actually part of this edit -- a severity- or
    SQL-only edit has no threshold signal to record.
    """
    rule = _require_pending_rule(rule_id)

    new_validation_status = None
    validation_errors: list[str] = []
    if body.generated_sql is not None:
        allowed_tables = [f"{rule['database_name']}.{rule['schema_name']}.{rule['table_name']}"]
        result = validate_sql(body.generated_sql, allowed_tables=allowed_tables)
        validation_errors = result.errors
        new_validation_status = _VALIDATION_STATUS_MAP["VALID" if result.is_valid else "INVALID"]

    update_recommended_rule(
        rule_id,
        severity=body.severity,
        threshold_config=body.threshold_config,
        generated_sql=body.generated_sql,
        validation_status=new_validation_status,
    )

    if body.threshold_config is not None:
        store_user_feedback(
            feedback_type="EDIT",
            rule_type=rule["rule_type"],
            database_name=rule["database_name"],
            schema_name=rule["schema_name"],
            table_name=rule["table_name"],
            column_name=rule["column_name"],
            rule_id=rule_id,
            threshold_config=body.threshold_config,
        )

    updated = get_recommended_rule(rule_id)
    updated["validation_errors"] = validation_errors
    return updated


# ---------------------------------------------------------------------------
# Rule Execution: run one approved rule now, manually
# ---------------------------------------------------------------------------


@app.get("/api/rules/active")
def get_active_rules() -> list[dict]:
    """List every approved rule (the Active Rules Page), each annotated
    with its most recent execution -- see storage_tools.list_approved_rules().
    """
    try:
        return list_approved_rules()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/rules/{rule_id}/run")
def run_rule(rule_id: str) -> dict:
    """Manually run one approved rule's SQL now.

    Thin wrapper over agents/rule_execution_agent.run_rule_execution_agent(),
    which does the real work (fetch -> revalidate -> execute -> store
    history -> alert-if-failed) -- unlike every other agent in this
    codebase, that agent persists directly rather than returning an
    in-memory result for this route to store, per the ask's own listed flow
    (see that module's docstring for the flagged deviation from this
    codebase's usual agent/route split).
    """
    try:
        return run_rule_execution_agent(rule_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/rules/run-all")
def run_all_rules() -> dict:
    """Manually run every approved, active rule now -- the "Run Rules"
    bulk action the final demo flow calls for (previously only a per-rule
    "Run now" button existed, see docs/deferred-and-future-work.md). Thin
    wrapper over the shared run_all_approved_rules() (see its docstring for
    the full flow, including the auto-resolve step it now performs).
    """
    try:
        return {"results": run_all_approved_rules()}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/rules/execution-history")
def get_execution_history(
    status: str | None = None,
    rule_id: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    """List rule runs, newest first. Pass limit to cap result size."""
    try:
        return list_execution_history(status=status, rule_id=rule_id, limit=limit)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/tables/health")
def get_table_health() -> list[dict]:
    """Per-table DQ health summary (Table Health Page) -- see
    storage_tools.list_table_health() for the aggregation/scoring logic.
    """
    try:
        return list_table_health()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Alerts Dashboard
# ---------------------------------------------------------------------------


@app.get("/api/alerts/summary")
def get_alerts_summary_route() -> dict:
    """Summary tiles for the Alerts Dashboard -- see storage_tools.get_alerts_summary()."""
    try:
        return get_alerts_summary()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/alerts")
def get_alerts(
    database_name: str | None = None,
    schema_name: str | None = None,
    table_name: str | None = None,
    severity: str | None = None,
    status: str | None = None,
    date: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    """List alerts. Pass limit to cap result size."""
    try:
        return list_alerts(
            database_name=database_name,
            schema_name=schema_name,
            table_name=table_name,
            severity=severity,
            status=status,
            date=date,
            limit=limit,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def _require_alert(alert_id: str) -> dict:
    alert = get_alert(alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail=f"Alert {alert_id!r} not found")
    return alert


@app.get("/api/alerts/{alert_id}")
def get_alert_route(alert_id: str) -> dict:
    """Fetch one alert's full detail, including violation_samples (see
    get_alert()) -- the Alerts Dashboard's "View details" expansion needs
    this rather than what list_alerts() already returned, since samples are
    only fetched on this single-alert path to avoid an N+1 on the list view.
    """
    return _require_alert(alert_id)


@app.post("/api/alerts/{alert_id}/accept")
def accept_alert(alert_id: str) -> dict:
    """Mark an alert ACCEPTED -- the admin has seen it and agrees it's a
    real issue (per the README's alert-status requirements). MVP statuses
    only: OPEN/ACCEPTED/FALSE_POSITIVE (per the ask's explicit MVP scope) --
    REJECTED/RESOLVED are not wired to a route here. RESOLVED in particular
    is meant to auto-clear when a rule passes on a later run (architecture.md
    §8: "a passing rule auto-clears its alert on next run"), not something a
    human sets by hand -- no scheduler/re-run trigger exists yet to ever set
    it, so a manual "mark resolved" route would be a status a real re-run
    could never produce, i.e. a lie about how the alert actually closed.
    """
    _require_alert(alert_id)
    update_alert_status(alert_id, "ACCEPTED")
    return get_alert(alert_id)


@app.post("/api/alerts/{alert_id}/false-positive")
def mark_alert_false_positive(alert_id: str) -> dict:
    """Mark an alert FALSE_POSITIVE -- the admin reviewed it and the rule's
    check itself was wrong, not the data (per the README's "user can mark
    an alert as a false positive also as part of feedback system").

    Feedback Loop (per the "Add Feedback Loop" ask, "if similar rule was
    false positive, lower priority"): also writes a RULES.USER_FEEDBACK
    row (feedback_type="FALSE_POSITIVE") keyed on the alert's underlying
    rule's (rule_type, database, schema, table, column) --
    agents/rule_recommendation_agent.py's _apply_feedback() lowers a
    future same-column-same-type candidate's priority when this exists,
    rather than blocking it outright the way a REJECT does (a false
    positive means the rule concept had merit but this particular alert
    didn't, not "never suggest this again").
    """
    alert = _require_alert(alert_id)
    update_alert_status(alert_id, "FALSE_POSITIVE")
    store_user_feedback(
        feedback_type="FALSE_POSITIVE",
        rule_type=alert["rule_type"],
        database_name=alert["database_name"],
        schema_name=alert["schema_name"],
        table_name=alert["table_name"],
        column_name=alert["column_name"],
        alert_id=alert_id,
    )
    return get_alert(alert_id)


# ---------------------------------------------------------------------------
# Scheduler: CORE.SCAN_SCHEDULES CRUD (see scheduler.py for the poll loop
# that actually reads these and fires due jobs)
# ---------------------------------------------------------------------------


class CreateScheduleRequest(BaseModel):
    schedule_type: str  # RULE_EXECUTION / RESCAN
    target_database: str
    target_schema: str | None = None
    target_table: str | None = None
    interval_minutes: int


@app.post("/api/schedules")
def create_schedule(body: CreateScheduleRequest) -> dict:
    """Create one schedule row. RULE_EXECUTION ignores target_schema/
    target_table (it always re-runs every approved rule, see
    scheduler.py's rule-execution job -- a schedule row for this type only
    really needs to exist to carry an interval; target fields are accepted
    for a consistent request shape but not used to filter which rules run).
    RESCAN uses target_database/target_schema (+ optional target_table,
    None meaning "every table in that schema") to know what to re-scan.
    """
    if body.schedule_type not in ("RULE_EXECUTION", "RESCAN"):
        raise HTTPException(
            status_code=400,
            detail="schedule_type must be 'RULE_EXECUTION' or 'RESCAN'",
        )
    schedule_id = create_scan_schedule(
        schedule_type=body.schedule_type,
        target_database=body.target_database,
        target_schema=body.target_schema,
        target_table=body.target_table,
        interval_minutes=body.interval_minutes,
    )
    return {"schedule_id": schedule_id}


@app.get("/api/schedules")
def get_schedules() -> list[dict]:
    """List every schedule (active and inactive) -- for a future settings
    page; no UI consumes this yet, matching this project's convention of
    building the route/storage layer before the frontend for a new feature
    when the ask itself didn't specify UI (see e.g. AGENT_RUN_LOGS's gap
    before the live-progress-feed feature finally displayed it).
    """
    try:
        return list_scan_schedules()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def _require_schedule(schedule_id: str) -> dict:
    schedule = get_scan_schedule(schedule_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail=f"Schedule {schedule_id!r} not found")
    return schedule


@app.post("/api/schedules/{schedule_id}/deactivate")
def deactivate_schedule(schedule_id: str) -> dict:
    """Deactivate one schedule -- the Settings page's only lever over an
    existing schedule (create + list + deactivate is the full scope; no
    edit/delete route). Idempotent: deactivating an already-inactive
    schedule just re-returns the current row rather than 409ing, unlike
    _require_pending_rule()'s one-way-transition 409 -- a toggle isn't a
    domain-irreversible action the way approve/reject is, so a stale UI
    double-click shouldn't be punished.
    """
    _require_schedule(schedule_id)
    set_scan_schedule_active(schedule_id, is_active=False)
    return get_scan_schedule(schedule_id)
