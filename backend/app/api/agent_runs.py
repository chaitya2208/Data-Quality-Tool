from datetime import datetime
from fastapi import APIRouter, HTTPException, BackgroundTasks
from typing import List, Optional

from app.services import storage
from app.services.snowflake_session import session as sf_session
from app.schemas.agent_run import (
    AgentRunCreateRequest, AgentRunResponse, AgentRunListResponse,
    AgentRuleSuggestion, AgentTaskResponse, RuleReviewRequest,
    AgentBatchCreateRequest, AgentBatchResponse, BulkInstanceActionRequest,
)
from app.services.agents.coordinator import WorkflowCoordinator, DB_AGENT_ORDER
from app.services.batch_runner import run_batch
import logging

router = APIRouter()
logger = logging.getLogger(__name__)


def _task_duration(task) -> Optional[float]:
    if task.started_at and task.completed_at:
        return (task.completed_at - task.started_at).total_seconds()
    return None


def _build_task_response(task) -> AgentTaskResponse:
    return AgentTaskResponse(
        id=task.id,
        run_id=task.run_id,
        agent_name=task.agent_name,
        status=task.status,
        started_at=task.started_at,
        completed_at=task.completed_at,
        output=task.output,
        error_message=task.error_message,
        duration_seconds=_task_duration(task),
    )


def _build_run_response(run) -> AgentRunResponse:
    return AgentRunResponse(
        id=run.id,
        connection_id=getattr(run, "connection_id", None),
        batch_id=getattr(run, "batch_id", None),
        batch_index=getattr(run, "batch_index", 0) or 0,
        database=run.database,
        schema_name=run.schema_name,
        table=run.table,
        status=run.status,
        scan_id=run.scan_id,
        started_at=run.started_at,
        completed_at=run.completed_at,
        findings_count=run.findings_count,
        ai_rules_count=getattr(run, "ai_rules_count", 0) or 0,
        ai_rules_proposed=(getattr(run, "instance_review_state", None) or {}).get("ai_rules_proposed")
            or getattr(run, "ai_rules_count", 0) or 0,
        instance_review_state=getattr(run, "instance_review_state", None),
        error_message=run.error_message,
        created_at=run.created_at,
        schedule_id=getattr(run, "schedule_id", None),
        tasks=[_build_task_response(t) for t in run.tasks],
    )


@router.post("/runs", response_model=AgentRunResponse, status_code=202)
def start_workflow(
    request: AgentRunCreateRequest,
    background_tasks: BackgroundTasks,
):
    """Start a new agent workflow run. Returns immediately; pipeline runs in background."""
    run = storage.create_agent_run(
        connection_id=request.connection_id,
        database=request.database,
        schema_name=request.schema_name,
        table=request.table,
        status="pending",
    )
    storage.create_agent_tasks(run.id, DB_AGENT_ORDER)
    run = storage.get_agent_run(run.id)

    background_tasks.add_task(WorkflowCoordinator(run_id=run.id).run)
    logger.info(f"[API] Started run {run.id} for {request.database}.{request.schema_name}.{request.table}")
    return _build_run_response(run)


@router.post("/runs/batch", response_model=AgentBatchResponse, status_code=202)
def start_batch(request: AgentBatchCreateRequest):
    """
    Start a workflow over a table / schema / database scope.
    Creates one AgentRun per table sharing a batch_id, processed sequentially:
    the first run starts immediately; each subsequent run auto-starts once the
    previous one reaches rule review / awaiting fixes.
    """
    try:
        batch_id, runs = run_batch(
            connection_id=request.connection_id,
            scope=request.scope,
            database=request.database,
            schema_name=request.schema_name,
            table=request.table,
            workflow_template_id=request.workflow_template_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to enumerate scope: {e}")

    return AgentBatchResponse(
        batch_id=batch_id,
        scope=request.scope,
        database=request.database,
        schema_name=request.schema_name,
        total=len(runs),
        runs=[_build_run_response(r) for r in runs],
    )


@router.get("/runs/batch/{batch_id}", response_model=AgentBatchResponse)
def get_batch(batch_id: str):
    runs = storage.list_agent_runs_by_batch(batch_id)
    if not runs:
        raise HTTPException(status_code=404, detail="Batch not found")
    first = runs[0]
    scope = "database" if len({r.schema_name for r in runs}) > 1 else (
        "schema" if len(runs) > 1 else "table"
    )
    return AgentBatchResponse(
        batch_id=batch_id,
        scope=scope,
        database=first.database,
        schema_name=first.schema_name if scope != "database" else None,
        total=len(runs),
        runs=[_build_run_response(r) for r in runs],
    )


@router.get("/runs", response_model=AgentRunListResponse)
def list_runs(limit: int = 20):
    runs = storage.list_agent_runs(limit=limit)
    return AgentRunListResponse(total=len(runs), runs=[_build_run_response(r) for r in runs])


@router.get("/runs/{run_id}", response_model=AgentRunResponse)
def get_run(run_id: str):
    run = storage.get_agent_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Agent run not found")
    return _build_run_response(run)


@router.post("/runs/{run_id}/review-rules", response_model=AgentRunResponse)
def save_rule_review(run_id: str, request: RuleReviewRequest):
    """
    Save the user's instance review decisions (active/skipped lists with edits).
    Does not trigger pipeline — call /run-pipeline after this. Any severity/name
    edit on an existing (non-new) instance is persisted immediately and marks
    the instance edited_by_human, so future recommendation refreshes respect it.
    """
    run = storage.get_agent_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Agent run not found")
    if run.status != "awaiting_rule_review":
        raise HTTPException(
            status_code=400,
            detail=f"Run is in '{run.status}' state, expected 'awaiting_rule_review'"
        )

    for entry in request.active + request.skipped:
        if entry.is_new_instance:
            continue
        instance = storage.get_instance(entry.instance_id)
        if instance and entry.severity != instance.severity:
            storage.update_instance(entry.instance_id, severity=entry.severity, edited_by_human=True)

    run = storage.update_agent_run(
        run_id,
        instance_review_state={
            "active":  [e.model_dump() for e in request.active],
            "skipped": [e.model_dump() for e in request.skipped],
        },
    )
    logger.info(f"[API] Saved instance review for run {run_id}: {len(request.active)} active, {len(request.skipped)} skipped")
    return _build_run_response(run)


@router.post("/runs/{run_id}/review-rules/bulk-approve", response_model=AgentRunResponse)
def bulk_approve_instances(run_id: str, request: BulkInstanceActionRequest):
    """Move a set of PENDING instances from skipped to active in one call,
    without needing to resend the full active/skipped lists."""
    run = storage.get_agent_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Agent run not found")
    if run.status != "awaiting_rule_review":
        raise HTTPException(status_code=400, detail=f"Run is in '{run.status}' state, expected 'awaiting_rule_review'")

    state = run.instance_review_state or {"active": [], "skipped": []}
    ids = set(request.instance_ids)
    moved = [e for e in state.get("skipped", []) if e.get("instance_id") in ids]
    state["skipped"] = [e for e in state.get("skipped", []) if e.get("instance_id") not in ids]
    state["active"] = state.get("active", []) + moved

    run = storage.update_agent_run(run_id, instance_review_state=state)
    logger.info(f"[API] Bulk-approved {len(moved)} instances for run {run_id}")
    return _build_run_response(run)


@router.post("/runs/{run_id}/review-rules/bulk-reject", response_model=AgentRunResponse)
def bulk_reject_instances(run_id: str, request: BulkInstanceActionRequest):
    """Move a set of PENDING instances from active to skipped in one call."""
    run = storage.get_agent_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Agent run not found")
    if run.status != "awaiting_rule_review":
        raise HTTPException(status_code=400, detail=f"Run is in '{run.status}' state, expected 'awaiting_rule_review'")

    state = run.instance_review_state or {"active": [], "skipped": []}
    ids = set(request.instance_ids)
    moved = [e for e in state.get("active", []) if e.get("instance_id") in ids]
    for e in moved:
        e["reason"] = request.reason or e.get("reason") or "Bulk-rejected at review"
    state["active"] = [e for e in state.get("active", []) if e.get("instance_id") not in ids]
    state["skipped"] = state.get("skipped", []) + moved

    run = storage.update_agent_run(run_id, instance_review_state=state)
    logger.info(f"[API] Bulk-rejected {len(moved)} instances for run {run_id}")
    return _build_run_response(run)


@router.post("/runs/{run_id}/run-pipeline", status_code=202)
def run_pipeline(run_id: str, background_tasks: BackgroundTasks):
    """
    Trigger FindingsAgent using the approved instance set from instance_review_state.
    Approves new instances (and their new definitions) permanently in the library.
    """
    run = storage.get_agent_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Agent run not found")
    if run.status != "awaiting_rule_review":
        raise HTTPException(
            status_code=400,
            detail=f"Run is in '{run.status}' state, expected 'awaiting_rule_review'"
        )
    if not run.instance_review_state:
        raise HTTPException(status_code=400, detail="No instance review state found. Call /review-rules first.")

    # Get current Snowflake user for rule ownership
    ctx = sf_session.get_cached_context()
    snowflake_user = (ctx or {}).get("user", "data-governance-team")

    background_tasks.add_task(
        WorkflowCoordinator(run_id=run_id).run_pipeline_after_review,
        snowflake_user,
    )
    logger.info(f"[API] Running pipeline for run {run_id} as {snowflake_user}")
    return {"message": "Pipeline started", "run_id": run_id}


@router.get("/runs/{run_id}/rule-suggestions", response_model=List[AgentRuleSuggestion])
def get_rule_suggestions(run_id: str):
    """Get AI-suggested instances proposed during this run, regardless of
    their current approval status."""
    run = storage.get_agent_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Agent run not found")

    _, instances = storage.list_instances(limit=5000)
    run_instances = [i for i in instances if i.source_run_id == run_id]

    result = []
    for inst in run_instances:
        definition = storage.get_definition(inst.definition_id)
        if not definition:
            continue
        result.append(AgentRuleSuggestion(
            instance_id=inst.id,
            definition_id=definition.id,
            name=definition.name,
            description=definition.description,
            category=definition.category,
            severity=inst.severity,
            scope=inst.scope,
            rationale=definition.description,
            instance_status=inst.status,
        ))
    return result


@router.post("/runs/{run_id}/verify", status_code=202)
def trigger_verification(run_id: str, background_tasks: BackgroundTasks):
    """
    Triggered manually after developer has fixed some issues.
    Checks current DB finding statuses — no re-scan needed.
    """
    run = storage.get_agent_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Agent run not found")
    if run.status not in ("awaiting_fixes", "completed", "failed"):
        raise HTTPException(
            status_code=400,
            detail="Run must be in awaiting_fixes or completed state before verification"
        )

    from app.services.agents.auto_verify_scheduler import (
        cancel as cancel_auto_verify,
        schedule as schedule_auto_verify,
        AUTO_VERIFY_INTERVAL_SECONDS,
    )

    # Cancel any pending auto-verify so it doesn't race with the manual one
    cancel_auto_verify(run_id)

    def _run_verification():
        try:
            run2 = storage.get_agent_run(run_id)
            task2 = storage.get_agent_task(run_id, "verification_agent")
            if not task2:
                return

            storage.update_agent_task(task2.id, status="running", started_at=datetime.utcnow())

            from app.services.agents.verification_agent import VerificationAgent
            result = VerificationAgent().run(run2, task2)

            # Mark task completed only when fully resolved — otherwise reset
            # to pending so the verify node doesn't show a green tick while
            # findings are still open.
            if result and result.get("fully_resolved"):
                storage.update_agent_task(task2.id, status="completed", completed_at=datetime.utcnow(), output=result)
                storage.update_agent_run(run_id, status="completed", completed_at=datetime.utcnow())
            else:
                storage.update_agent_task(task2.id, status="pending", output=result)
                storage.update_agent_run(run_id, status="awaiting_fixes")
                # Reschedule auto-verify for next cycle
                schedule_auto_verify(run_id)

        except Exception as e:
            logger.error(f"Verification failed: {e}")
            try:
                t = storage.get_agent_task(run_id, "verification_agent")
                if t:
                    storage.update_agent_task(t.id, status="failed", error_message=str(e)[:1024])
            except Exception:
                pass
            # Reschedule even after failure
            schedule_auto_verify(run_id)
            return

    background_tasks.add_task(_run_verification)
    return {"message": "Verification started", "run_id": run_id}


# ── Workflow Templates ────────────────────────────────────────────────────────

@router.get("/workflows")
def list_workflows():
    workflows = storage.list_workflows()
    return [_workflow_response(w) for w in workflows]


@router.get("/workflows/{workflow_id}")
def get_workflow(workflow_id: str):
    w = storage.get_workflow(workflow_id)
    if not w:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return _workflow_response(w)


@router.post("/runs/{run_id}/save-as-workflow", status_code=201)
def save_run_as_workflow(run_id: str, request: dict):
    """
    Save a workflow from a run's currently-active rule set. Works for ANY run
    type — AI pipeline, saved-workflow template, or scheduled — because it reads
    the active RULE_INSTANCES on the run's target table rather than relying on
    instance_review_state (which only exists for runs that paused for review).
    """
    run = storage.get_agent_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Agent run not found")

    label = (request.get("label") or "").strip()
    if not label:
        raise HTTPException(status_code=400, detail="label is required")

    # The rule set a run applies = its table-scoped active instances PLUS every
    # global (DATABASE_NAME='*') governance instance, which applies to all
    # tables. This mirrors what the pipeline itself executes (see
    # rule_intelligence_agent._existing_active_or_pending_instances). Without the
    # global union, a run that fires only governance rules — or whose
    # table-specific proposals were all rejected — has zero table-scoped active
    # instances and save-as-workflow wrongly reports "No active rules found".
    _, table_instances = storage.list_instances(
        database_name=run.database,
        schema_name=run.schema_name,
        table_name=run.table,
        status="active",
        is_active=True,
        limit=1000,
    )
    _, global_instances = storage.list_instances(
        database_name="*",
        status="active",
        is_active=True,
        limit=1000,
    )
    seen_ids: set = set()
    instances = []
    for inst in [*table_instances, *global_instances]:
        if inst.id not in seen_ids:
            seen_ids.add(inst.id)
            instances.append(inst)

    patterns = []
    for inst in instances:
        definition = storage.get_definition(inst.definition_id)
        patterns.append({
            "definition_id":   inst.definition_id,
            "definition_name": definition.name if definition else inst.definition_id,
            "scope":           inst.scope,
            "target_config":   inst.target_config or {},
            "threshold_config": inst.threshold_config or {},
            "severity":        inst.severity,
            "template_shape":  (definition.template_shape if definition else None),
            "rationale":       inst.rationale or "",
        })

    if not patterns:
        raise HTTPException(
            status_code=400,
            detail="No active rules found for this run's table to save as a workflow.",
        )

    w = storage.create_workflow(
        label=label,
        description=(request.get("description") or ""),
        rule_patterns=patterns,
        created_by=(request.get("created_by") or ""),
        origin_scope="table",
        origin_database=run.database,
        origin_schema=run.schema_name,
        origin_table=run.table,
    )
    logger.info(f"[API] Saved workflow '{label}' from run {run_id} — {len(patterns)} patterns")
    return _workflow_response(w)


@router.post("/workflows", status_code=201)
def create_workflow(request: dict):
    label = request.get("label", "").strip()
    if not label:
        raise HTTPException(status_code=400, detail="label is required")
    patterns = request.get("rule_patterns", [])
    if not patterns:
        raise HTTPException(status_code=400, detail="rule_patterns must not be empty")
    w = storage.create_workflow(
        label=label,
        description=request.get("description", ""),
        rule_patterns=patterns,
        created_by=request.get("created_by", ""),
        origin_scope=request.get("origin_scope"),
        origin_database=request.get("origin_database"),
        origin_schema=request.get("origin_schema"),
        origin_table=request.get("origin_table"),
    )
    return _workflow_response(w)


@router.put("/workflows/{workflow_id}")
def update_workflow(workflow_id: str, request: dict):
    w = storage.get_workflow(workflow_id)
    if not w:
        raise HTTPException(status_code=404, detail="Workflow not found")
    w = storage.update_workflow(
        workflow_id,
        label=request.get("label"),
        description=request.get("description"),
        rule_patterns=request.get("rule_patterns"),
    )
    return _workflow_response(w)


@router.delete("/workflows/{workflow_id}", status_code=204)
def delete_workflow(workflow_id: str):
    w = storage.get_workflow(workflow_id)
    if not w:
        raise HTTPException(status_code=404, detail="Workflow not found")
    storage.delete_workflow(workflow_id)


def _workflow_response(w) -> dict:
    return {
        "id": w.id,
        "label": w.label,
        "description": w.description,
        "rule_patterns": w.rule_patterns,
        "created_by": w.created_by,
        "created_at": w.created_at.isoformat() if w.created_at else None,
        "updated_at": w.updated_at.isoformat() if w.updated_at else None,
        "pattern_count": len(w.rule_patterns or []),
        "origin_scope": getattr(w, "origin_scope", None),
        "origin_database": getattr(w, "origin_database", None),
        "origin_schema": getattr(w, "origin_schema", None),
        "origin_table": getattr(w, "origin_table", None),
    }
