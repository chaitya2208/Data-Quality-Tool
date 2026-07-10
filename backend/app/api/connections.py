"""
Connections API — manage saved data sources (Snowflake, Postgres/RDS).

Secrets are write-only: accepted on create/update, never returned (responses
expose `has_secret` only).
"""
import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.connection import Connection, ConnectionType
from app.schemas.connection import (
    ConnectionCreate, ConnectionUpdate, ConnectionResponse,
    ConnectionListResponse, ConnectionTestResult,
)
from app.services.datasources import get_source, clear_cached_source

router = APIRouter()
logger = logging.getLogger(__name__)


def _to_response(c: Connection) -> ConnectionResponse:
    return ConnectionResponse(
        id=c.id,
        name=c.name,
        type=c.type.value if hasattr(c.type, "value") else str(c.type),
        host=c.host,
        port=c.port,
        database=c.database,
        schema_name=c.schema_,
        username=c.username,
        has_secret=bool(c.secret),
        auth_method=c.auth_method,
        extra=c.extra,
        is_active=c.is_active,
        created_at=c.created_at,
    )


def _parse_type(t: str) -> ConnectionType:
    try:
        return ConnectionType(t)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Unsupported connection type '{t}'")


@router.get("", response_model=ConnectionListResponse)
def list_connections(db: Session = Depends(get_db)):
    rows = db.query(Connection).order_by(Connection.created_at.asc()).all()
    return ConnectionListResponse(total=len(rows), connections=[_to_response(c) for c in rows])


@router.post("", response_model=ConnectionResponse, status_code=201)
def create_connection(req: ConnectionCreate, db: Session = Depends(get_db)):
    conn = Connection(
        name=req.name,
        type=_parse_type(req.type),
        host=req.host,
        port=req.port,
        database=req.database,
        schema_=req.schema_name,
        username=req.username,
        secret=req.secret,
        auth_method=req.auth_method,
        extra=req.extra,
        is_active=req.is_active,
    )
    db.add(conn)
    db.commit()
    db.refresh(conn)
    logger.info(f"[Connections] Created {conn.type} connection {conn.id} ({conn.name})")
    return _to_response(conn)


@router.patch("/{connection_id}", response_model=ConnectionResponse)
def update_connection(connection_id: str, req: ConnectionUpdate, db: Session = Depends(get_db)):
    conn = db.query(Connection).filter(Connection.id == connection_id).first()
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")

    data = req.model_dump(exclude_unset=True)
    if "schema_name" in data:
        conn.schema_ = data.pop("schema_name")
    # Only overwrite the secret when a non-empty value is explicitly provided
    if "secret" in data and not data["secret"]:
        data.pop("secret")
    for k, v in data.items():
        setattr(conn, k, v)

    db.commit()
    db.refresh(conn)
    clear_cached_source(connection_id)  # rebuild adapter with new settings
    return _to_response(conn)


@router.delete("/{connection_id}", status_code=204)
def delete_connection(connection_id: str, db: Session = Depends(get_db)):
    conn = db.query(Connection).filter(Connection.id == connection_id).first()
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")
    db.delete(conn)
    db.commit()
    clear_cached_source(connection_id)
    return None


@router.post("/{connection_id}/test", response_model=ConnectionTestResult)
def test_connection(connection_id: str, db: Session = Depends(get_db)):
    conn = db.query(Connection).filter(Connection.id == connection_id).first()
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")
    try:
        source = get_source(connection_id, db=db)
        result = source.test_connection()
    except Exception as e:
        result = {"ok": False, "user": None, "detail": str(e)}
    return ConnectionTestResult(**result)


@router.get("/{connection_id}/status", response_model=ConnectionTestResult)
def connection_status(connection_id: str, db: Session = Depends(get_db)):
    return test_connection(connection_id, db)
