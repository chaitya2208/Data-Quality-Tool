import uuid
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from typing import List

from app.core.database import get_db, SessionLocal
from app.models.agent_run import (
    AgentRun, AgentTask,
    AgentRunStatus, AgentTaskStatus,
)
from app.models.finding import Finding
from app.models.rule import Rule, RuleStatus
from app.schemas.agent_run import (
    AgentRunCreateRequest, AgentRunResponse, AgentRunListResponse,
    AgentRuleSuggestion, AgentTaskResponse, RuleReviewRequest,
    AgentBatchCreateRequest, AgentBatchResponse,
)
from app.services.agents.coordinator import WorkflowCoordinator, DB_AGENT_ORDER
from app.services.datasources import get_source
import logging

router = APIRouter()
logger = logging.getLogger(__name__)


def _task_duration(task: AgentTask) -> float | None:
    if task.started_at and task.completed_at:
        return (task.completed_at - task.started_at).total_seconds()
    return None


def _build_task_response(task: AgentTask) -> AgentTaskResponse:
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


def _build_run_response(run: AgentRun) -> AgentRunResponse:
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
        rule_review_state=getattr(run, "rule_review_state", None),
        error_message=run.error_message,
        created_at=run.created_at,
        tasks=[_build_task_response(t) for t in run.tasks],
    )


@router.post("/runs", response_model=AgentRunResponse, status_code=202)
def start_workflow(
    request: AgentRunCreateRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Start a new agent workflow run. Returns immediately; pipeline runs in background."""
    run = AgentRun(
        id=str(uuid.uuid4()),
        connection_id=request.connection_id,
        database=request.database,
        schema_name=request.schema_name,
        table=request.table,
        status=AgentRunStatus.PENDING,
    )
    db.add(run)
    db.flush()

    for agent_name in DB_AGENT_ORDER:
        db.add(AgentTask(
            run_id=run.id,
            agent_name=agent_name,
            status=AgentTaskStatus.PENDING,
        ))

    db.commit()
    db.refresh(run)

    background_tasks.add_task(WorkflowCoordinator(run_id=run.id).run)
    logger.info(f"[API] Started run {run.id} for {request.database}.{request.schema_name}.{request.table}")
    return _build_run_response(run)


def _expand_scope(req: AgentBatchCreateRequest, source) -> List[tuple[str, str, str]]:
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
        targets: List[tuple[str, str, str]] = []
        for sch in source.list_schemas(req.database):
            for t in source.list_tables(req.database, sch):
                targets.append((req.database, sch, t["name"]))
        return targets

    raise HTTPException(status_code=400, detail=f"Unknown scope '{req.scope}'")


@router.post("/runs/batch", response_model=AgentBatchResponse, status_code=202)
def start_batch(
    request: AgentBatchCreateRequest,
    db: Session = Depends(get_db),
):
    """
    Start a workflow over a table / schema / database scope.
    Creates one AgentRun per table sharing a batch_id, processed sequentially:
    the first run starts immediately; each subsequent run auto-starts once the
    previous one reaches rule review / awaiting fixes.
    """
    try:
        source = get_source(request.connection_id, db=db)
        targets = _expand_scope(request, source)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to enumerate scope: {e}")

    if not targets:
        raise HTTPException(status_code=404, detail="No tables found for the selected scope")

    batch_id = str(uuid.uuid4())
    runs: List[AgentRun] = []
    for idx, (database, schema_name, table) in enumerate(targets):
        run = AgentRun(
            id=str(uuid.uuid4()),
            connection_id=request.connection_id,
            batch_id=batch_id,
            batch_index=idx,
            database=database,
            schema_name=schema_name,
            table=table,
            status=AgentRunStatus.PENDING,
        )
        db.add(run)
        db.flush()
        for agent_name in DB_AGENT_ORDER:
            db.add(AgentTask(
                run_id=run.id,
                agent_name=agent_name,
                status=AgentTaskStatus.PENDING,
            ))
        runs.append(run)

    db.commit()
    for r in runs:
        db.refresh(r)

    # Kick off only the first table — the coordinator advances the rest sequentially
    first = runs[0]
    import threading
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
def get_batch(batch_id: str, db: Session = Depends(get_db)):
    runs = (
        db.query(AgentRun)
        .filter(AgentRun.batch_id == batch_id)
        .order_by(AgentRun.batch_index.asc())
        .all()
    )
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
def list_runs(limit: int = 20, db: Session = Depends(get_db)):
    runs = (
        db.query(AgentRun)
        .order_by(AgentRun.created_at.desc())
        .limit(limit)
        .all()
    )
    return AgentRunListResponse(total=len(runs), runs=[_build_run_response(r) for r in runs])


@router.get("/runs/{run_id}", response_model=AgentRunResponse)
def get_run(run_id: str, db: Session = Depends(get_db)):
    run = db.query(AgentRun).filter(AgentRun.id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Agent run not found")
    return _build_run_response(run)


@router.post("/runs/{run_id}/review-rules", response_model=AgentRunResponse)
def save_rule_review(
    run_id: str,
    request: RuleReviewRequest,
    db: Session = Depends(get_db),
):
    """
    Save the user's rule review decisions (active/skipped lists with edits).
    Does not trigger pipeline — call /run-pipeline after this.
    """
    run = db.query(AgentRun).filter(AgentRun.id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Agent run not found")
    if run.status != AgentRunStatus.AWAITING_RULE_REVIEW:
        raise HTTPException(
            status_code=400,
            detail=f"Run is in '{run.status}' state, expected 'awaiting_rule_review'"
        )

    run.rule_review_state = {
        "active":  [e.model_dump() for e in request.active],
        "skipped": [e.model_dump() for e in request.skipped],
    }
    db.commit()
    db.refresh(run)
    logger.info(f"[API] Saved rule review for run {run_id}: {len(request.active)} active, {len(request.skipped)} skipped")
    return _build_run_response(run)


@router.post("/runs/{run_id}/run-pipeline", status_code=202)
def run_pipeline(
    run_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    Trigger FindingsAgent using the approved rule set from rule_review_state.
    Persists approved AI rules permanently to the rules library.
    """
    run = db.query(AgentRun).filter(AgentRun.id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Agent run not found")
    if run.status != AgentRunStatus.AWAITING_RULE_REVIEW:
        raise HTTPException(
            status_code=400,
            detail=f"Run is in '{run.status}' state, expected 'awaiting_rule_review'"
        )
    if not run.rule_review_state:
        raise HTTPException(status_code=400, detail="No rule review state found. Call /review-rules first.")

    # Get current Snowflake user for rule ownership
    from app.services.snowflake_session import session as sf_session
    ctx = sf_session.get_cached_context()
    snowflake_user = (ctx or {}).get("user", "data-governance-team")

    background_tasks.add_task(
        WorkflowCoordinator(run_id=run_id).run_pipeline_after_review,
        snowflake_user,
    )
    logger.info(f"[API] Running pipeline for run {run_id} as {snowflake_user}")
    return {"message": "Pipeline started", "run_id": run_id}



@router.get("/runs/{run_id}/rule-suggestions", response_model=List[AgentRuleSuggestion])
def get_rule_suggestions(run_id: str, db: Session = Depends(get_db)):
    """Get AI-suggested rules created during this run."""
    run = db.query(AgentRun).filter(AgentRun.id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Agent run not found")

    # Return ALL rules from this run regardless of their current approval status
    rules = (
        db.query(Rule)
        .filter(Rule.created_by == "rule_suggestion_agent")
        .order_by(Rule.created_at.desc())
        .limit(50)
        .all()
    )
    run_rules = [r for r in rules if (r.rule_config or {}).get("source_run_id") == run_id]

    return [
        AgentRuleSuggestion(
            rule_id=r.id,
            code=r.code,
            name=r.name,
            description=r.description,
            category=r.category.value if hasattr(r.category, "value") else str(r.category),
            severity=r.severity.value if hasattr(r.severity, "value") else str(r.severity),
            applies_to=r.applies_to or [],
            rationale=_extract_rationale(r.description),
            rule_status=r.status.value if hasattr(r.status, "value") else str(r.status),
        )
        for r in run_rules
    ]


@router.post("/runs/{run_id}/verify", status_code=202)
def trigger_verification(
    run_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    Triggered manually after developer has fixed some issues.
    Checks current DB finding statuses — no re-scan needed.
    """
    run = db.query(AgentRun).filter(AgentRun.id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Agent run not found")
    if run.status not in (
        AgentRunStatus.AWAITING_FIXES,
        AgentRunStatus.COMPLETED,
        AgentRunStatus.FAILED,
    ):
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
        db2 = SessionLocal()
        try:
            run2 = db2.query(AgentRun).filter(AgentRun.id == run_id).first()
            task2 = db2.query(AgentTask).filter(
                AgentTask.run_id == run_id,
                AgentTask.agent_name == "verification_agent"
            ).first()
            if not task2:
                return

            task2.status = AgentTaskStatus.RUNNING
            task2.started_at = datetime.utcnow()
            db2.commit()

            from app.services.agents.verification_agent import VerificationAgent
            result = VerificationAgent(db2).run(run2, task2)

            task2.status = AgentTaskStatus.COMPLETED
            task2.completed_at = datetime.utcnow()
            task2.output = result
            db2.commit()

            # Auto-complete when fully resolved; otherwise stay awaiting fixes
            if result.get("fully_resolved"):
                run2.status = AgentRunStatus.COMPLETED
                run2.completed_at = datetime.utcnow()
                db2.commit()
                # No need to re-schedule — workflow is done
            else:
                run2.status = AgentRunStatus.AWAITING_FIXES
                db2.commit()
                # Reschedule auto-verify for next cycle
                schedule_auto_verify(run_id)

        except Exception as e:
            logger.error(f"Verification failed: {e}")
            db2.close()
            db3 = SessionLocal()
            try:
                t = db3.query(AgentTask).filter(
                    AgentTask.run_id == run_id,
                    AgentTask.agent_name == "verification_agent"
                ).first()
                if t:
                    t.status = AgentTaskStatus.FAILED
                    t.error_message = str(e)[:1024]
                    db3.commit()
            finally:
                db3.close()
            # Reschedule even after failure
            schedule_auto_verify(run_id)
            return
        finally:
            db2.close()

    background_tasks.add_task(_run_verification)
    return {"message": "Verification started", "run_id": run_id}


def _extract_rationale(description: str) -> str:
    marker = "[AI Rationale]"
    if marker in description:
        return description.split(marker, 1)[1].strip()
    return ""
