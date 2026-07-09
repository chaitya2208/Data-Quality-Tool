"""Snowflake connection management for the DQ platform.

Single shared connection for both source queries and app writes.
All SQL in storage_tools.py uses fully-qualified names (SCHEMA.TABLE)
so no USE DATABASE/SCHEMA switching is needed — cursors are opened
directly with no locking overhead, which means parallel dashboard
requests execute concurrently.

One SSO browser prompt ever: connect() is called once (POST
/api/connection/connect) and reused for the lifetime of the process.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from typing import Any, Iterator

# pyrefly: ignore [missing-import]
import snowflake.connector
# pyrefly: ignore [missing-import]
from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"Missing required env var {name!r}. Copy .env.example to .env and fill it in."
        )
    return value


# ── Single shared connection ──────────────────────────────────────────────────

_conn: snowflake.connector.SnowflakeConnection | None = None
_conn_lock = threading.Lock()   # guards initial creation only (SSO serialisation)


def _connect() -> snowflake.connector.SnowflakeConnection:
    """Open the shared connection via externalbrowser SSO.

    client_store_temporary_credential caches the SSO token locally so
    repeated connect() calls within the same OS session skip the browser.
    insecure_mode bypasses corporate proxy certificate interception on
    Snowflake's S3 large-result downloads.
    """
    role = os.getenv("SNOWFLAKE_ROLE") or os.getenv("APP_SNOWFLAKE_ROLE") or None
    return snowflake.connector.connect(
        account=_require("SNOWFLAKE_ACCOUNT"),
        user=_require("SNOWFLAKE_USER"),
        authenticator=os.getenv("SNOWFLAKE_AUTHENTICATOR", "externalbrowser"),
        role=role,
        warehouse=_require("SNOWFLAKE_WAREHOUSE"),
        database=_require("APP_SNOWFLAKE_DATABASE"),
        schema=os.getenv("APP_SNOWFLAKE_SCHEMA", "CORE"),
        client_store_temporary_credential=True,
        network_timeout=120,
        insecure_mode=True,
    )


def is_source_connected() -> bool:
    return _conn is not None and not _conn.is_closed()


def get_source_connection() -> snowflake.connector.SnowflakeConnection:
    """Return the shared connection, opening it (SSO) if needed.
    Thread-safe: concurrent callers wait for the first SSO flow to finish."""
    global _conn
    if is_source_connected():
        return _conn
    with _conn_lock:
        if not is_source_connected():
            _conn = _connect()
    return _conn


def get_app_connection() -> snowflake.connector.SnowflakeConnection:
    """Same shared connection as get_source_connection().
    All SQL uses fully-qualified names so no context switching needed."""
    return get_source_connection()


# ── Query helpers ─────────────────────────────────────────────────────────────

def _run(
    sql: str,
    params: dict[str, Any] | None = None,
    timeout: int | None = None,
) -> list[dict[str, Any]]:
    """Execute sql on the shared connection. Each call opens its own cursor
    so concurrent requests don't block each other."""
    conn = get_source_connection()
    cur = conn.cursor()
    try:
        if timeout is not None:
            cur.execute(sql, params or {}, timeout=timeout)
        else:
            cur.execute(sql, params or {})
        if cur.description is None:
            return []
        columns = [c[0] for c in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]
    finally:
        cur.close()


def run_query(
    sql: str,
    params: dict[str, Any] | None = None,
    timeout: int | None = None,
) -> list[dict[str, Any]]:
    """Run a read query against the source data. All SQL must pass the
    SQL validator before reaching here. timeout is optional."""
    return _run(sql, params, timeout=timeout)


def run_app_query(
    sql: str,
    params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Run a query against the app-owned tables (full access: read/write)."""
    return _run(sql, params)


# ── Cursor context managers (kept for any callers that use them) ──────────────

@contextmanager
def source_cursor() -> Iterator[snowflake.connector.cursor.SnowflakeCursor]:
    conn = get_source_connection()
    cur = conn.cursor()
    try:
        yield cur
    finally:
        cur.close()


@contextmanager
def app_cursor() -> Iterator[snowflake.connector.cursor.SnowflakeCursor]:
    conn = get_source_connection()
    cur = conn.cursor()
    try:
        yield cur
    finally:
        cur.close()
