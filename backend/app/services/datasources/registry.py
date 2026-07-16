"""
Registry — resolve a connection_id to a live DataSource adapter, cached.

Mirrors the old single-singleton reuse, but keyed by connection_id so multiple
sources coexist. Adapters are cheap wrappers; the Snowflake one reuses the
process-wide SSO session, the Postgres one holds its own connection.

Connection records now live in the Snowflake DQ_APP.CONNECTIONS table (via
app.services.storage), not the old SQLAlchemy ORM. `storage` returns a
SimpleNamespace with the same attribute names the adapters expect
(.type, .id, .extra, .host, .schema_, ...).
"""
from __future__ import annotations

import logging
import threading
from typing import Dict, Optional

from app.services import storage
from app.services.connection_types import ConnectionType
from app.services.datasources.base import DataSource

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_cache: Dict[str, DataSource] = {}


def _build(conn) -> DataSource:
    if conn.type == ConnectionType.SNOWFLAKE.value:
        from app.services.datasources.snowflake_source import SnowflakeSource
        return SnowflakeSource(conn)
    if conn.type == ConnectionType.POSTGRES.value:
        from app.services.datasources.postgres_source import PostgresSource
        return PostgresSource(conn)
    raise ValueError(f"Unsupported connection type: {conn.type}")


def get_source(connection_id: Optional[str], db=None, strict: bool = False) -> DataSource:
    """
    Resolve a connection_id to a DataSource. If connection_id is None, fall back
    to the first Snowflake connection (backward compat with single-source callers).

    `strict=True` disables the fallback: a run whose connection is null or whose
    connection record no longer exists must FAIL LOUDLY rather than silently
    running against some other datasource. Used by schedule-originated runs,
    which store their own connection_id and must never misroute (a schedule
    saved with no connection, or whose connection was later deleted, should
    error clearly instead of scanning the wrong source).

    `db` is accepted and ignored — kept so existing call sites that pass a
    session don't break during the ORM→storage migration.
    """
    if strict:
        if not connection_id:
            raise ValueError(
                "This run has no connection configured — set a data source on the schedule."
            )
        conn = storage.get_connection_record(connection_id)
        if conn is None:
            raise ValueError(
                f"Connection {connection_id} no longer exists — it may have been deleted. "
                "Edit the schedule to pick a valid data source."
            )
    else:
        conn = None
        if connection_id:
            conn = storage.get_connection_record(connection_id)
        if conn is None:
            # Fallback: first connection (prefer Snowflake for legacy behavior)
            conn = storage.get_first_connection(prefer_type=ConnectionType.SNOWFLAKE.value)
        if conn is None:
            raise ValueError("No connections configured")

    with _lock:
        cached = _cache.get(conn.id)
        if cached is not None:
            return cached
        source = _build(conn)
        _cache[conn.id] = source
        return source


def clear_cached_source(connection_id: str) -> None:
    """Drop a cached adapter (call on connection edit/delete)."""
    with _lock:
        _cache.pop(connection_id, None)
