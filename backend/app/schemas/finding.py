from pydantic import BaseModel
from typing import Optional, Dict, Any, List
from datetime import datetime
from app.core.enums import FindingStatus


class FindingBase(BaseModel):
    asset_id: str
    scan_id: str
    rule_id: Optional[str] = None
    instance_id: Optional[str] = None
    title: str
    description: str
    severity: str
    context: Optional[Dict[str, Any]] = None
    evidence: Optional[Dict[str, Any]] = None


class FindingCreate(FindingBase):
    status: FindingStatus = FindingStatus.OPEN


class FindingUpdate(BaseModel):
    status: Optional[FindingStatus] = None
    assigned_to: Optional[str] = None
    resolution_notes: Optional[str] = None


class FindingResponse(FindingBase):
    id: str
    status: FindingStatus
    assigned_to: Optional[str] = None
    resolution_notes: Optional[str] = None
    detected_at: datetime
    resolved_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    updated_at: datetime

    # Incident-lifecycle fields — populated by the new scan finalizer. Older
    # rows are backfilled at migration time; new rows always have these.
    first_detected_at: Optional[datetime] = None
    last_seen_at: Optional[datetime] = None
    last_scan_id: Optional[str] = None
    reopened_count: Optional[int] = 0
    current_fail_count: Optional[int] = None
    current_total_count: Optional[int] = None
    fail_history: Optional[List[Dict[str, Any]]] = None

    class Config:
        from_attributes = True


class FindingListResponse(BaseModel):
    total: int
    findings: List[FindingResponse]
