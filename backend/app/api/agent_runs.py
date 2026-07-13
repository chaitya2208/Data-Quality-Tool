import threading
import uuid
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
from app.services.datasources import get_source
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
        instance_review_state=getattr(run, "instance_review_state", None),
        error_message=run.error_message,
        created_at=run.created_at,
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


def _expand_scope(req: AgentBatchCreateRequest, source) -> List[tuple]:
    """Resolve a scope request into an ordered list of (database, schema, table)."""
    scope = (req.scope or "table").lower()

    if scope == "table":
        if not (req.schema_name and req.table):
            raise HTTPException(status_code=400, detail="scope=table requires schema_name and table")
        return [(req.database, req.schema_name, req.table)]

    if scope == "schema":
        if not req.schema_name:
            raise HTTPException(status_code=400, detail="scope=schema requires schema_name")
        tables = [t["name"] for t in source.list_tables(req.database, req.schema_name)]
        return [(req.database, req.schema_name, t) for t in tables]

    if scope == "database":
        targets: List[tuple] = []
        for sch in source.list_schemas(req.database):
            for t in source.list_tables(req.database, sch):
                targets.append((req.database, sch, t["name"]))
        return targets

    raise HTTPException(status_code=400, detail=f"Unknown scope '{req.scope}'")


@router.post("/runs/batch", response_model=AgentBatchResponse, status_code=202)
def start_batch(request: AgentBatchCreateRequest):
    """
    Start a workflow over a table / schema / database scope.
    Creates one AgentRun per table sharing a batch_id, processed sequentially:
    the first run starts immediately; each subsequent run auto-starts once the
    previous one reaches rule review / awaiting fixes.
    """
    try:
        source = get_source(request.connection_id)
        targets = _expand_scope(request, source)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to enumerate scope: {e}")

    if not targets:
        raise HTTPException(status_code=404, detail="No tables found for the selected scope")

    batch_id = str(uuid.uuid4())
    runs = []
    for idx, (database, schema_name, table) in enumerate(targets):
        run = storage.create_agent_run(
            connection_id=request.connection_id,
            database=database,
            schema_name=schema_name,
            table=table,
            status="pending",
            batch_id=batch_id,
            batch_index=idx,
        )
        storage.create_agent_tasks(run.id, DB_AGENT_ORDER)
        runs.append(storage.get_agent_run(run.id))

    # Kick off only the first table — the coordinator advances the rest sequentially
    first = runs[0]
    threading.Thread(target=WorkflowCoordinator(run_id=first.id).run, daemon=True).start()

    logger.info(
        f"[API] Started batch {batch_id} scope={request.scope} — {len(runs)} tables, "
        f"first={first.database}.{first.schema_name}.{first.table}"
    )
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

            storage.update_agent_task(task2.id, status="completed", completed_at=datetime.utcnow(), output=result)

            # Auto-complete when fully resolved; otherwise stay awaiting fixes
            if result.get("fully_resolved"):
                storage.update_agent_run(run_id, status="completed", completed_at=datetime.utcnow())
                # No need to re-schedule — workflow is done
            else:
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
