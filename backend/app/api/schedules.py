"""
Scheduled workflow runs — CRUD + run-now + toggle.

A schedule fires a batch (database/schema/table scope, optionally applying a
saved workflow) on a cadence. The schedule_runner tick loop reads NEXT_RUN_AT
and calls the same run_batch() service the manual endpoint uses, so a scheduled
run is identical to a manual one. Times are server-local (see schedule_calc).
"""
import logging
from datetime import datetime
from fastapi import APIRouter, HTTPException
from typing import List

from app.services import storage
from app.services.batch_runner import run_batch
from app.services.schedule_calc import compute_next_run
from app.schemas.schedule import (
    ScheduleCreateRequest, ScheduleUpdateRequest, ScheduleResponse,
)

router = APIRouter()
logger = logging.getLogger(__name__)

# Fields whose change requires recomputing NEXT_RUN_AT.
_TIMING_FIELDS = {
    "cadence", "time_of_day", "day_of_week", "day_of_month",
    "month_of_year", "interval_value", "interval_unit",
}


def _to_response(s) -> ScheduleResponse:
    return ScheduleResponse(
        id=s.id,
        name=s.name,
        enabled=s.enabled,
        connection_id=s.connection_id,
        scope=s.scope,
        database=s.database_name,
        schema_name=s.schema_name,
        table=s.table_name,
        workflow_template_id=s.workflow_template_id,
        cadence=s.cadence,
        time_of_day=s.time_of_day,
        day_of_week=s.day_of_week,
        day_of_month=s.day_of_month,
        month_of_year=s.month_of_year,
        interval_value=s.interval_value,
        interval_unit=s.interval_unit,
        next_run_at=s.next_run_at,
        last_run_at=s.last_run_at,
        last_batch_id=s.last_batch_id,
        last_status=s.last_status,
        last_error=s.last_error,
        created_at=s.created_at,
        created_by=s.created_by,
    )


@router.get("", response_model=List[ScheduleResponse])
def list_schedules():
    return [_to_response(s) for s in storage.list_schedules()]


@router.get("/{schedule_id}", response_model=ScheduleResponse)
def get_schedule(schedule_id: str):
    s = storage.get_schedule(schedule_id)
    if not s:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return _to_response(s)


@router.post("", response_model=ScheduleResponse, status_code=201)
def create_schedule(request: ScheduleCreateRequest):
    # compute_next_run reads cadence/timing attrs off the request directly.
    next_run = compute_next_run(request, datetime.now())
    s = storage.create_schedule(
        name=request.name,
        enabled=request.enabled,
        connection_id=request.connection_id,
        scope=request.scope.lower(),
        database_name=request.database,
        schema_name=request.schema_name,
        table_name=request.table,
        workflow_template_id=request.workflow_template_id,
        cadence=request.cadence.lower(),
        time_of_day=request.time_of_day,
        day_of_week=request.day_of_week,
        day_of_month=request.day_of_month,
        month_of_year=request.month_of_year,
        interval_value=request.interval_value,
        interval_unit=(request.interval_unit or None),
        next_run_at=next_run,
        created_by=request.created_by,
    )
    logger.info(f"[API] Created schedule '{s.name}' ({s.id}) — next run {next_run.isoformat()}")
    return _to_response(s)


@router.put("/{schedule_id}", response_model=ScheduleResponse)
def update_schedule(schedule_id: str, request: ScheduleUpdateRequest):
    existing = storage.get_schedule(schedule_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Schedule not found")

    # Map request field names → storage column names; skip unset fields.
    field_map = {
        "name": "name", "enabled": "enabled", "connection_id": "connection_id",
        "scope": "scope", "database": "database_name", "schema_name": "schema_name",
        "table": "table_name", "workflow_template_id": "workflow_template_id",
        "cadence": "cadence", "time_of_day": "time_of_day", "day_of_week": "day_of_week",
        "day_of_month": "day_of_month", "month_of_year": "month_of_year",
        "interval_value": "interval_value", "interval_unit": "interval_unit",
    }
    provided = request.model_dump(exclude_unset=True)
    updates = {}
    for req_key, value in provided.items():
        col = field_map.get(req_key)
        if not col:
            continue
        if req_key in ("scope", "cadence") and isinstance(value, str):
            value = value.lower()
        updates[col] = value

    if updates:
        storage.update_schedule(schedule_id, **updates)

    # Recompute NEXT_RUN_AT when any timing field changed.
    if _TIMING_FIELDS & set(provided.keys()):
        refreshed = storage.get_schedule(schedule_id)
        next_run = compute_next_run(refreshed, datetime.now())
        storage.update_schedule(schedule_id, next_run_at=next_run)

    return _to_response(storage.get_schedule(schedule_id))


@router.delete("/{schedule_id}", status_code=204)
def delete_schedule(schedule_id: str):
    if not storage.get_schedule(schedule_id):
        raise HTTPException(status_code=404, detail="Schedule not found")
    storage.delete_schedule(schedule_id)


@router.post("/{schedule_id}/toggle", response_model=ScheduleResponse)
def toggle_schedule(schedule_id: str):
    s = storage.get_schedule(schedule_id)
    if not s:
        raise HTTPException(status_code=404, detail="Schedule not found")
    new_enabled = not s.enabled
    updates = {"enabled": new_enabled}
    # Re-enabling: refresh NEXT_RUN_AT so it doesn't immediately fire on a
    # long-past time (or stay stuck in the past while disabled).
    if new_enabled:
        updates["next_run_at"] = compute_next_run(s, datetime.now())
    storage.update_schedule(schedule_id, **updates)
    return _to_response(storage.get_schedule(schedule_id))


@router.post("/{schedule_id}/run-now", status_code=202)
def run_now(schedule_id: str):
    """Fire the schedule's batch immediately without disturbing NEXT_RUN_AT."""
    s = storage.get_schedule(schedule_id)
    if not s:
        raise HTTPException(status_code=404, detail="Schedule not found")
    try:
        batch_id, runs = run_batch(
            connection_id=s.connection_id,
            scope=s.scope,
            database=s.database_name,
            schema_name=s.schema_name,
            table=s.table_name,
            workflow_template_id=s.workflow_template_id,
            schedule_id=s.id,
            # Fail loud if the schedule's connection is missing/deleted rather
            # than silently running against a different datasource.
            strict_connection=True,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to start run: {e}")

    storage.update_schedule(schedule_id, last_batch_id=batch_id, last_status="ok", last_error=None)
    return {"message": "Run started", "batch_id": batch_id, "total": len(runs)}
