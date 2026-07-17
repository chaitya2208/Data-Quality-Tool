"""
Mute API — silence a specific (rule instance, asset) pair for a window.

Scans still run and RULE_EXECUTIONS still logs during a mute; the incident
lifecycle in scan_finalizer.finalize_scan just skips UPDATE/REOPEN/CREATE on
failures for muted pairs, so no new incident appears until the mute expires.
Passing during a mute still auto-resolves — mutes silence noise, not fixes.
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime, timedelta

from app.services import storage
from app.services.snowflake_session import session as sf_session

router = APIRouter()


class MuteCreate(BaseModel):
    instance_id: str
    asset_id: str
    duration_hours: Optional[int] = None    # convenience: relative window
    muted_until: Optional[datetime] = None  # explicit end time
    reason: Optional[str] = None


class MuteResponse(BaseModel):
    id: str
    instance_id: str
    asset_id: str
    muted_until: datetime
    reason: Optional[str] = None
    muted_by: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


@router.get("", response_model=List[MuteResponse])
def list_active_mutes(
    instance_id: Optional[str] = None, asset_id: Optional[str] = None,
    active_only: bool = True,
):
    return storage.list_mutes(instance_id=instance_id, asset_id=asset_id, active_only=active_only)


@router.post("", response_model=MuteResponse, status_code=201)
def create_mute(body: MuteCreate):
    if body.muted_until is None and body.duration_hours is None:
        raise HTTPException(status_code=400, detail="Provide muted_until or duration_hours")
    until = body.muted_until or (datetime.utcnow() + timedelta(hours=int(body.duration_hours or 0)))
    muted_by = (sf_session.get_cached_context() or {}).get("user")
    return storage.create_mute(
        instance_id=body.instance_id, asset_id=body.asset_id,
        muted_until=until, reason=body.reason, muted_by=muted_by,
    )


@router.delete("/{mute_id}", status_code=204)
def delete_mute(mute_id: str):
    storage.delete_mute(mute_id)
    return None
