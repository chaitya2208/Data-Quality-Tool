"""
Connections API — manage saved data sources (Snowflake, Postgres/RDS).

Secrets are write-only: accepted on create/update, never returned (responses
expose `has_secret` only).

Connections persist in the Snowflake DQ_APP.CONNECTIONS table via
app.services.storage; the old SQLAlchemy ORM/session is gone.
"""
import logging
from fastapi import APIRouter, HTTPException

from app.schemas.connection import (
    ConnectionCreate, ConnectionUpdate, ConnectionResponse,
    ConnectionListResponse, ConnectionTestResult,
)
from app.services import storage, secrets_manager
from app.services.connection_types import ConnectionType
from app.services.datasources import get_source, clear_cached_source

router = APIRouter()
logger = logging.getLogger(__name__)


def _to_response(c) -> ConnectionResponse:
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


def _validate_type(t: str) -> str:
    """Validate an incoming type string against ConnectionType; return the value."""
    try:
        return ConnectionType(t).value
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Unsupported connection type '{t}'")


@router.get("", response_model=ConnectionListResponse)
def list_connections():
    rows = storage.list_connections()
    return ConnectionListResponse(total=len(rows), connections=[_to_response(c) for c in rows])


@router.post("", response_model=ConnectionResponse, status_code=201)
def create_connection(req: ConnectionCreate):
    conn_type = _validate_type(req.type)
    postgres_with_secret = conn_type == ConnectionType.POSTGRES.value and req.secret

    # For Postgres/RDS the password goes to AWS Secrets Manager, never the
    # CONNECTIONS.SECRET column. Create the row first (no raw secret) so we have
    # the connection id, then store the password under that id and persist only
    # the pointer. Snowflake connections pass their secret through unchanged.
    conn = storage.create_connection(
        name=req.name,
        type=conn_type,
        host=req.host,
        port=req.port,
        database=req.database,
        schema_=req.schema_name,
        username=req.username,
        secret=None if postgres_with_secret else req.secret,
        auth_method=req.auth_method,
        extra=req.extra,
        is_active=req.is_active,
    )

    if postgres_with_secret:
        try:
            pointer = secrets_manager.put_secret(conn.id, req.secret)
            conn = storage.update_connection(conn.id, secret=pointer)
        except Exception as e:
            # Don't leave a half-created row with no usable secret.
            storage.delete_connection(conn.id)
            logger.error(f"[Connections] Secrets Manager store failed, rolled back {conn.id}: {e}")
            raise HTTPException(
                status_code=502,
                detail=f"Could not store the password in AWS Secrets Manager: {e}",
            )

    logger.info(f"[Connections] Created {conn.type} connection {conn.id} ({conn.name})")
    return _to_response(conn)


@router.patch("/{connection_id}", response_model=ConnectionResponse)
def update_connection(connection_id: str, req: ConnectionUpdate):
    conn = storage.get_connection_record(connection_id)
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")

    data = req.model_dump(exclude_unset=True)
    fields = {}
    if "schema_name" in data:
        fields["schema_"] = data.pop("schema_name")
    # Only overwrite the secret when a non-empty value is explicitly provided
    if "secret" in data and not data["secret"]:
        data.pop("secret")

    # A new Postgres/RDS password goes to Secrets Manager; persist only the
    # pointer in the SECRET column. Snowflake secrets pass through unchanged.
    if "secret" in data and conn.type == ConnectionType.POSTGRES.value:
        raw_secret = data.pop("secret")
        try:
            data["secret"] = secrets_manager.put_secret(connection_id, raw_secret)
        except Exception as e:
            logger.error(f"[Connections] Secrets Manager store failed for {connection_id}: {e}")
            raise HTTPException(
                status_code=502,
                detail=f"Could not store the password in AWS Secrets Manager: {e}",
            )

    fields.update(data)

    if fields:
        conn = storage.update_connection(connection_id, **fields)
    clear_cached_source(connection_id)  # rebuild adapter with new settings
    return _to_response(conn)


@router.delete("/{connection_id}", status_code=204)
def delete_connection(connection_id: str):
    conn = storage.get_connection_record(connection_id)
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")
    # Clean up the AWS Secrets Manager entry for Postgres/RDS connections whose
    # secret was stored there (best-effort — never blocks the delete).
    if conn.type == ConnectionType.POSTGRES.value and secrets_manager.is_pointer(conn.secret):
        secrets_manager.delete_secret(conn.secret)
    storage.delete_connection(connection_id)
    clear_cached_source(connection_id)
    return None


@router.post("/{connection_id}/test", response_model=ConnectionTestResult)
def test_connection(connection_id: str):
    conn = storage.get_connection_record(connection_id)
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")
    try:
        source = get_source(connection_id)
        result = source.test_connection()
    except Exception as e:
        result = {"ok": False, "user": None, "detail": str(e)}
    return ConnectionTestResult(**result)


@router.get("/{connection_id}/status", response_model=ConnectionTestResult)
def connection_status(connection_id: str):
    return test_connection(connection_id)
