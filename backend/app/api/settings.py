"""Settings API — read/update tunable app preferences + read-only system info."""
import logging
from typing import Any, Dict
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.services import settings_service

router = APIRouter()
logger = logging.getLogger(__name__)


class SettingsUpdate(BaseModel):
    updates: Dict[str, Any]


@router.get("")
def get_settings(db: Session = Depends(get_db)):
    """Effective values + metadata (default/min/max/label/help) for every setting."""
    return settings_service.get_all(db)


@router.patch("")
def update_settings(req: SettingsUpdate, db: Session = Depends(get_db)):
    """Persist a batch of setting changes; returns the full effective settings."""
    return settings_service.update(req.updates, db)


@router.get("/system-info")
def system_info(db: Session = Depends(get_db)):
    """
    Read-only status: backend health + every saved connection with its live
    reachability (a quick test_connection per source).
    """
    from app.models.connection import Connection
    from app.services.datasources import get_source

    conns = db.query(Connection).order_by(Connection.created_at.asc()).all()
    out = []
    for c in conns:
        status = {"ok": False, "user": None, "detail": None}
        try:
            status = get_source(c.id, db=db).test_connection()
        except Exception as e:
            status = {"ok": False, "user": None, "detail": str(e)}
        extra = c.extra or {}
        out.append({
            "id": c.id,
            "name": c.name,
            "type": c.type.value if hasattr(c.type, "value") else str(c.type),
            "host": c.host,
            "database": c.database,
            "username": c.username,
            "warehouse": extra.get("warehouse"),
            "role": extra.get("role"),
            "connected": bool(status.get("ok")),
            "connected_user": status.get("user"),
            "detail": status.get("detail"),
        })

    return {
        "backend": "healthy",
        "connections_count": len(conns),
        "connections": out,
    }
