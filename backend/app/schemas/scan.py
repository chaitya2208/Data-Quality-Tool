from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List
from datetime import datetime
from app.core.enums import ScanStatus, ScanType


class ScanBase(BaseModel):
    asset_id: str
    scan_type: ScanType = ScanType.METADATA
    scan_config: Optional[Dict[str, Any]] = None


class ScanCreate(ScanBase):
    pass


class ScanResponse(ScanBase):
    id: str
    status: ScanStatus
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    rules_checked: int = 0
    findings_count: int = 0
    error_message: Optional[str] = None
    scan_results: Optional[Dict[str, Any]] = None
    created_at: datetime

    class Config:
        from_attributes = True


class ScanListResponse(BaseModel):
    total: int
    scans: List[ScanResponse]
