from pydantic import BaseModel
from typing import List, Optional, Any, Dict
from datetime import datetime
from app.core.enums import AgentRunStatus, AgentTaskStatus


class AgentRunCreateRequest(BaseModel):
    database: str
    schema_name: str
    table: str
    connection_id: Optional[str] = None


class AgentBatchCreateRequest(BaseModel):
    """
    Start a workflow over a scope. Expands into per-table AgentRuns processed
    sequentially (each pauses for rule review, then auto-advances to the next).
      - scope="table"    : requires database, schema_name, table
      - scope="schema"   : requires database, schema_name  (all tables in schema)
      - scope="database" : requires database               (all tables, all schemas)
    When workflow_template_id is provided the run uses the saved rule patterns
    directly — rule intelligence is skipped and no rule review pause occurs.
    """
    scope: str  # "table" | "schema" | "database"
    database: str
    schema_name: Optional[str] = None
    table: Optional[str] = None
    connection_id: Optional[str] = None
    workflow_template_id: Optional[str] = None


class AgentTaskResponse(BaseModel):
    id: str
    run_id: str
    agent_name: str
    status: AgentTaskStatus
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    output: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    duration_seconds: Optional[float] = None

    class Config:
        from_attributes = True


class AgentRunResponse(BaseModel):
    id: str
    connection_id: Optional[str] = None
    batch_id: Optional[str] = None
    batch_index: int = 0
    database: str
    schema_name: str
    table: str
    status: AgentRunStatus
    scan_id: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    findings_count: int = 0
    ai_rules_count: int = 0  # approved instances after review
    ai_rules_proposed: int = 0  # new definitions proposed by AI before review
    instance_review_state: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    created_at: datetime
    schedule_id: Optional[str] = None  # set when the run was fired by a schedule
    tasks: List[AgentTaskResponse] = []

    class Config:
        from_attributes = True


class RuleReviewEntry(BaseModel):
    """A single instance entry in the review state (active or skipped)."""
    instance_id: str
    definition_id: str
    name: str
    description: str
    severity: str
    original_severity: str = ""
    reason: str = ""
    is_new_instance: bool = False
    is_new_definition: bool = False
    source: str = "llm"  # "existing" | "llm" | "deterministic"
    scope: str = "table"
    target_config: Dict[str, Any] = {}
    violated: bool = False
    violation_evidence: str = ""


class RuleReviewRequest(BaseModel):
    """Payload for POST /runs/{id}/review-rules."""
    active: List[RuleReviewEntry]
    skipped: List[RuleReviewEntry]


class BulkInstanceActionRequest(BaseModel):
    """Payload for POST /runs/{id}/review-rules/bulk-approve|bulk-reject."""
    instance_ids: List[str]
    reason: Optional[str] = None  # used by bulk-reject


class AgentRunListResponse(BaseModel):
    total: int
    runs: List[AgentRunResponse]


class AgentBatchResponse(BaseModel):
    """All runs belonging to a batch, in order."""
    batch_id: str
    scope: str
    database: str
    schema_name: Optional[str] = None
    total: int
    runs: List[AgentRunResponse]


class AgentRuleSuggestion(BaseModel):
    instance_id: str
    definition_id: str
    name: str
    description: str
    category: str
    severity: str
    scope: str
    rationale: str
    instance_status: str  # pending / active / rejected / disabled
