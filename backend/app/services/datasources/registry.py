"""
Registry — resolve a connection_id to a live DataSource adapter, cached.

Mirrors the old single-singleton reuse, but keyed by connection_id so multiple
sources coexist. Adapters are cheap wrappers; the Snowflake one reuses the
process-wide SSO session, the Postgres one holds its own connection.
"""
from __future__ import annotations

import logging
import threading
from typing import Dict, Optional

from app.core.database import SessionLocal
from app.models.connection import Connection, ConnectionType
from app.services.datasources.base import DataSource

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_cache: Dict[str, DataSource] = {}


def _build(conn: Connection) -> DataSource:
    if conn.type == ConnectionType.SNOWFLAKE:
        from app.services.datasources.snowflake_source import SnowflakeSource
        return SnowflakeSource(conn)
    if conn.type == ConnectionType.POSTGRES:
        from app.services.datasources.postgres_source import PostgresSource
        return PostgresSource(conn)
    raise ValueError(f"Unsupported connection type: {conn.type}")


def get_source(connection_id: Optional[str], db=None) -> DataSource:
    """
    Resolve a connection_id to a DataSource. If connection_id is None, fall back
    to the first Snowflake connection (backward compat with single-source callers).
    """
    own_db = False
    if db is None:
        db = SessionLocal()
        own_db = True
    try:
        conn = None
        if connection_id:
            conn = db.query(Connection).filter(Connection.id == connection_id).first()
        if conn is None:
            # Fallback: first active connection (prefer Snowflake for legacy behavior)
            conn = (db.query(Connection)
                    .filter(Connection.type == ConnectionType.SNOWFLAKE)
                    .first()
                    or db.query(Connection).first())
        if conn is None:
            raise ValueError("No connections configured")

        with _lock:
            cached = _cache.get(conn.id)
            if cached is not None:
                return cached
            source = _build(conn)
            _cache[conn.id] = source
            return source
    finally:
        if own_db:
            db.close()


def clear_cached_source(connection_id: str) -> None:
    """Drop a cached adapter (call on connection edit/delete)."""
    with _lock:
        _cache.pop(connection_id, None)
