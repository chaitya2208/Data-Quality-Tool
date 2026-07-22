"""Notifications inbox API.

Dashboard-level bell icon reads /notifications for unread count +
recent items. Anomaly Tier A only emits kind='anomaly_proposals'
today — the same inbox will absorb future event types (drift alerts,
incident summaries) once wired.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.snowflake_session import session as sf

logger = logging.getLogger(__name__)

router = APIRouter()

# ── Unread-count cache ────────────────────────────────────────────────────────
# The bell polls every 60s; hitting Snowflake on every request adds noise to
# logs and wastes warehouse time. Cache the count for 30s so multiple tabs /
# users share one Snowflake round-trip per interval.
_unread_cache_lock = threading.Lock()
_unread_cache_value: int = 0
_unread_cache_ts: float = 0.0
_UNREAD_CACHE_TTL = 30.0  # seconds


def _get_unread_count() -> int:
    global _unread_cache_value, _unread_cache_ts
    now = time.monotonic()
    with _unread_cache_lock:
        if now - _unread_cache_ts < _UNREAD_CACHE_TTL:
            return _unread_cache_value
    rows = sf.query("SELECT COUNT(*) AS N FROM NOTIFICATIONS WHERE READ_AT IS NULL", {})
    n = int((rows[0].get("N") if rows else 0) or 0)
    with _unread_cache_lock:
        _unread_cache_value = n
        _unread_cache_ts = time.monotonic()
    return n


def _invalidate_unread_cache() -> None:
    """Call after marking notifications read so the next poll reflects the change."""
    global _unread_cache_ts
    with _unread_cache_lock:
        _unread_cache_ts = 0.0


class NotificationOut(BaseModel):
    id: str
    kind: str
    title: str
    body: Optional[str] = None
    ref_table: Optional[str] = None
    ref_id: Optional[str] = None
    severity: Optional[str] = None
    read_at: Optional[str] = None
    created_at: Optional[str] = None


def _row_to_out(r: dict) -> NotificationOut:
    return NotificationOut(
        id=r["ID"],
        kind=r["KIND"],
        title=r["TITLE"],
        body=r.get("BODY"),
        ref_table=r.get("REF_TABLE"),
        ref_id=r.get("REF_ID"),
        severity=r.get("SEVERITY"),
        read_at=str(r["READ_AT"]) if r.get("READ_AT") else None,
        created_at=str(r["CREATED_AT"]) if r.get("CREATED_AT") else None,
    )


@router.get("")
def list_notifications(unread_only: bool = False, limit: int = 50):
    where = "WHERE READ_AT IS NULL" if unread_only else ""
    rows = sf.query(
        f"""
        SELECT * FROM NOTIFICATIONS {where}
        ORDER BY CREATED_AT DESC
        LIMIT %(limit)s
        """,
        {"limit": max(1, min(limit, 200))},
    )
    return {"items": [_row_to_out(r).model_dump() for r in rows]}


@router.get("/unread-count")
def unread_count():
    return {"unread": _get_unread_count()}


@router.post("/{notification_id}/read")
def mark_read(notification_id: str):
    sf.execute(
        "UPDATE NOTIFICATIONS SET READ_AT = CURRENT_TIMESTAMP() WHERE ID = %(id)s AND READ_AT IS NULL",
        {"id": notification_id},
    )
    _invalidate_unread_cache()
    return {"ok": True}


@router.post("/read-all")
def mark_all_read():
    sf.execute(
        "UPDATE NOTIFICATIONS SET READ_AT = CURRENT_TIMESTAMP() WHERE READ_AT IS NULL",
        {},
    )
    _invalidate_unread_cache()
    return {"ok": True}
