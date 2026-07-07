from pydantic import BaseModel
from typing import List, Optional, Any, Dict
from datetime import datetime
from app.models.agent_run import AgentRunStatus, AgentTaskStatus


class AgentRunCreateRequest(BaseModel):
    database: str
    schema_name: str
    table: str


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
    database: str
    schema_name: str
    table: str
    status: AgentRunStatus
    scan_id: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    findings_count: int = 0
    ai_rules_count: int = 0
    rule_review_state: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    created_at: datetime
    tasks: List[AgentTaskResponse] = []

    class Config:
        from_attributes = True


class RuleReviewEntry(BaseModel):
    """A single rule entry in the review state (active or skipped)."""
    code: str
    name: str
    description: str
    severity: str
    original_severity: str = ""
    reason: str = ""
    is_ai_generated: bool = False
    category: str = "data_quality"
    applies_to: List[str] = []
    violated: bool = False
    ai_violation_evidence: str = ""


class RuleReviewRequest(BaseModel):
    """Payload for POST /runs/{id}/review-rules."""
    active: List[RuleReviewEntry]
    skipped: List[RuleReviewEntry]


class AgentRunListResponse(BaseModel):
    total: int
    runs: List[AgentRunResponse]


class AgentRuleSuggestion(BaseModel):
    rule_id: str
    code: str
    name: str
    description: str
    category: str
    severity: str
    applies_to: List[str]
    rationale: str
    rule_status: str  # actual status: pending / active / rejected / disabled
