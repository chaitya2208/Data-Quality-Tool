from pydantic import BaseModel, model_validator
from typing import Optional
from datetime import datetime


_VALID_SCOPES = {"table", "schema", "database"}
_VALID_CADENCES = {"daily", "weekly", "monthly", "yearly", "custom"}


class ScheduleCreateRequest(BaseModel):
    """
    Create a scheduled workflow run.

    Scope semantics mirror AgentBatchCreateRequest:
      - scope="table"    : requires database, schema_name, table
      - scope="schema"   : requires database, schema_name
      - scope="database" : requires database
    When workflow_template_id is set, the scheduled run applies that saved
    workflow (rule intelligence skipped); otherwise it runs the AI pipeline.

    Cadence fields:
      - daily   : time_of_day
      - weekly  : time_of_day + day_of_week (0=Mon..6=Sun)
      - monthly : time_of_day + day_of_month (1..31, clamped)
      - yearly  : time_of_day + month_of_year (1..12) + day_of_month
      - custom  : interval_value + interval_unit ('hours'|'days')
    """
    name: str
    enabled: bool = True
    connection_id: Optional[str] = None

    scope: str
    database: str
    schema_name: Optional[str] = None
    table: Optional[str] = None
    workflow_template_id: Optional[str] = None

    cadence: str
    time_of_day: Optional[str] = None       # 'HH:MM'
    day_of_week: Optional[int] = None        # 0..6
    day_of_month: Optional[int] = None       # 1..31
    month_of_year: Optional[int] = None      # 1..12
    interval_value: Optional[int] = None     # custom
    interval_unit: Optional[str] = None      # 'hours'|'days'

    created_by: Optional[str] = None

    @model_validator(mode="after")
    def _validate(self):
        scope = (self.scope or "").lower()
        if scope not in _VALID_SCOPES:
            raise ValueError(f"scope must be one of {sorted(_VALID_SCOPES)}")
        if scope in ("table", "schema") and not self.schema_name:
            raise ValueError(f"scope={scope} requires schema_name")
        if scope == "table" and not self.table:
            raise ValueError("scope=table requires table")

        cadence = (self.cadence or "").lower()
        if cadence not in _VALID_CADENCES:
            raise ValueError(f"cadence must be one of {sorted(_VALID_CADENCES)}")
        if cadence == "custom":
            if not self.interval_value or self.interval_value < 1:
                raise ValueError("custom cadence requires interval_value >= 1")
            if (self.interval_unit or "").lower() not in ("hours", "days"):
                raise ValueError("custom cadence requires interval_unit 'hours' or 'days'")
        else:
            if not self.time_of_day:
                raise ValueError(f"cadence={cadence} requires time_of_day 'HH:MM'")
            if cadence == "weekly" and self.day_of_week is None:
                raise ValueError("weekly cadence requires day_of_week (0=Mon..6=Sun)")
            if cadence in ("monthly", "yearly") and not self.day_of_month:
                raise ValueError(f"{cadence} cadence requires day_of_month")
            if cadence == "yearly" and not self.month_of_year:
                raise ValueError("yearly cadence requires month_of_year")
        return self


class ScheduleUpdateRequest(BaseModel):
    """Partial update — any subset of the mutable fields. Cadence/timing
    changes trigger a NEXT_RUN_AT recompute in the endpoint."""
    name: Optional[str] = None
    enabled: Optional[bool] = None
    connection_id: Optional[str] = None
    scope: Optional[str] = None
    database: Optional[str] = None
    schema_name: Optional[str] = None
    table: Optional[str] = None
    workflow_template_id: Optional[str] = None
    cadence: Optional[str] = None
    time_of_day: Optional[str] = None
    day_of_week: Optional[int] = None
    day_of_month: Optional[int] = None
    month_of_year: Optional[int] = None
    interval_value: Optional[int] = None
    interval_unit: Optional[str] = None


class ScheduleResponse(BaseModel):
    id: str
    name: str
    enabled: bool
    connection_id: Optional[str] = None
    scope: str
    database: Optional[str] = None
    schema_name: Optional[str] = None
    table: Optional[str] = None
    workflow_template_id: Optional[str] = None
    cadence: str
    time_of_day: Optional[str] = None
    day_of_week: Optional[int] = None
    day_of_month: Optional[int] = None
    month_of_year: Optional[int] = None
    interval_value: Optional[int] = None
    interval_unit: Optional[str] = None
    next_run_at: Optional[datetime] = None
    last_run_at: Optional[datetime] = None
    last_batch_id: Optional[str] = None
    last_status: Optional[str] = None
    last_error: Optional[str] = None
    created_at: Optional[datetime] = None
    created_by: Optional[str] = None
