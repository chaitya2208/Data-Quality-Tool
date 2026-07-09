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

    class Config:
        from_attributes = True


class RuleListResponse(BaseModel):
    total: int
    rules: List[RuleResponse]
