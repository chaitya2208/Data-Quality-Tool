from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List
from datetime import datetime
from app.core.enums import RuleSeverity, RuleCategory, RuleStatus


class RuleBase(BaseModel):
    code:        str = Field(..., description="Unique UPPER_SNAKE_CASE rule code")
    name:        str
    description: str
    category:    RuleCategory
    severity:    RuleSeverity
    applies_to:  List[str]
    rule_config: Optional[Dict[str, Any]] = None
    owner:       str = Field(..., description="Required — team or person responsible")
    jira_ticket: Optional[str] = None
    created_by:  Optional[str] = None


class RuleCreate(RuleBase):
    pass


class RuleUpdate(BaseModel):
    name:        Optional[str]                    = None
    description: Optional[str]                   = None
    category:    Optional[RuleCategory]           = None
    severity:    Optional[RuleSeverity]           = None
    applies_to:  Optional[List[str]]              = None
    rule_config: Optional[Dict[str, Any]]         = None
    is_active:   Optional[bool]                   = None
    owner:       Optional[str]                    = None
    jira_ticket: Optional[str]                    = None
    status:      Optional[RuleStatus]             = None
    rejection_reason: Optional[str]               = None


class RuleResponse(RuleBase):
    id:              str
    version:         int
    status:          RuleStatus
    is_active:       bool
    rejection_reason: Optional[str] = None
    created_at:      datetime
    updated_at:      datetime
    approved_at:     Optional[datetime] = None
    rejected_at:     Optional[datetime] = None
    approved_by:     Optional[str] = None
    rejected_by:     Optional[str] = None
    source:          Optional[str] = None   # 'user' (Add Rule) | 'claude' | 'deterministic' | 'system'

    class Config:
        from_attributes = True


class RuleListResponse(BaseModel):
    total: int
    rules: List[RuleResponse]


# ── Rule library: RULE_DEFINITIONS / RULE_INSTANCES / RULE_EXECUTIONS ────────
# Plain str fields (not the RuleCategory/RuleStatus enums above) since these
# tables carry their own value sets (check_kind, source, definition status
# proposed/active/disabled) that don't map onto the legacy Rule enums.

class RuleDefinitionResponse(BaseModel):
    id: str
    name: str
    category: str
    description: str
    check_kind: str
    handler_key: Optional[str] = None
    template_shape: Optional[str] = None
    sql_template: Optional[str] = None
    default_severity: str
    allowed_scopes: List[str]
    source: str
    status: str
    instance_count: int
    approval_count: int
    owner: Optional[str] = None
    created_by: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class RuleDefinitionListResponse(BaseModel):
    total: int
    definitions: List[RuleDefinitionResponse]


class RuleInstanceResponse(BaseModel):
    id: str
    definition_id: str
    scope: str
    database_name: str
    schema_name: Optional[str] = None
    table_name: Optional[str] = None
    target_config: Dict[str, Any]
    threshold_config: Optional[Dict[str, Any]] = None
    severity: str
    rule_sql: Optional[str] = None
    status: str
    is_active: bool
    rationale: Optional[str] = None
    rejection_reason: Optional[str] = None
    owner: Optional[str] = None
    created_by: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    approved_at: Optional[datetime] = None
    rejected_at: Optional[datetime] = None
    approved_by: Optional[str] = None
    rejected_by: Optional[str] = None

    class Config:
        from_attributes = True


class RuleInstanceListResponse(BaseModel):
    total: int
    instances: List[RuleInstanceResponse]


class RuleExecutionResponse(BaseModel):
    id: str
    instance_id: str
    scan_id: Optional[str] = None
    run_id: Optional[str] = None
    status: str
    evidence: Optional[Dict[str, Any]] = None
    executed_at: datetime

    class Config:
        from_attributes = True


class RuleExecutionListResponse(BaseModel):
    total: int
    executions: List[RuleExecutionResponse]
