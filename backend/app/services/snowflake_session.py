"""
Single Snowflake session manager.

One SSO login at backend startup, everything reuses that connection.
- get_connection()         → reusable read connection (never mutated)
- execute_with_context()   → run SQL as a specific role+warehouse,
                             then restore original state; thread-safe
- get_cached_context()     → user info, roles, warehouses, databases
                             populated once at startup, served from memory
"""

import threading
from typing import List, Dict, Any, Optional
import snowflake.connector
from snowflake.connector import DictCursor
from app.core.config import settings
import logging
import ssl
import os

# Corporate proxy intercepts HTTPS (including S3 result-batch downloads) with
# its own cert. insecure_mode=True on connect() covers the Snowflake API call
# but not the result-batch S3 fetches which go through the vendored urllib3.
# Patch the full SSL chain here so every download in this process skips
# cert verification — acceptable since insecure_mode is already on.
os.environ.setdefault("PYTHONHTTPSVERIFY", "0")
ssl._create_default_https_context = ssl._create_unverified_context  # stdlib path

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Also silence the warning from Snowflake's bundled copy of urllib3
try:
    import snowflake.connector.vendored.urllib3 as _sf_urllib3
    _sf_urllib3.disable_warnings(_sf_urllib3.exceptions.InsecureRequestWarning)
except Exception:
    pass

# Patch Snowflake's vendored pyopenssl — the S3 result-batch downloader uses
# this path and it bypasses the stdlib ssl._create_default_https_context hook.
try:
    import snowflake.connector.vendored.urllib3.contrib.pyopenssl as _sf_pyopenssl
    _orig_wrap = _sf_pyopenssl.PyOpenSSLContext.wrap_socket
    def _insecure_wrap(self, *args, **kwargs):
        try:
            from OpenSSL import SSL as _SSL
            self._ctx.set_verify(_SSL.VERIFY_NONE, lambda *a: True)
        except Exception:
            pass
        return _orig_wrap(self, *args, **kwargs)
    _sf_pyopenssl.PyOpenSSLContext.wrap_socket = _insecure_wrap
except Exception as _e:
    logging.getLogger(__name__).debug(f"pyopenssl patch skipped: {_e}")

logger = logging.getLogger(__name__)


class SnowflakeSession:
    _instance = None
    _lock = threading.Lock()          # guards connection creation
    _exec_lock = threading.Lock()     # serialises role-switching executions

    _connection = None
    _context_cache: Optional[Dict[str, Any]] = None

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    # ─────────────────────────────────────────────
    # Connection
    # ─────────────────────────────────────────────

    def connect(self) -> None:
        """
        Open one SSO connection and keep it alive for the process lifetime.
        Called once at startup — all subsequent calls are no-ops if alive.
        """
        if self._connection and self._is_alive():
            return

        logger.info("Opening Snowflake connection (SSO will open browser)…")
        params: Dict[str, Any] = {
            "account":   settings.SNOWFLAKE_ACCOUNT,
            "user":      settings.SNOWFLAKE_USER,
            "warehouse": settings.SNOWFLAKE_WAREHOUSE,
        }
        if settings.SNOWFLAKE_ROLE:
            params["role"] = settings.SNOWFLAKE_ROLE
        # A blank SNOWFLAKE_DATABASE in .env overrides the config default with
        # an empty string, which leaves the session with no current database —
        # then storage.py's unqualified table names (ASSETS, RULES, ...) fail
        # with "does not have a current database". Fall back to the default so
        # the app boots even if .env has the key set empty.
        database = settings.SNOWFLAKE_DATABASE or "PLAYGROUND_DB"
        params["database"] = database
        # Default schema = app storage schema, so storage.py's unqualified
        # table names (ASSETS, RULES, ...) resolve without prefixing every
        # query. Source-table queries always use fully-qualified
        # database.schema.table names, so they're unaffected by this.
        params["schema"] = settings.SNOWFLAKE_APP_SCHEMA or "DQ_APP"

        auth = getattr(settings, "SNOWFLAKE_AUTH_METHOD", "externalbrowser")
        if auth.lower() == "externalbrowser":
            params["authenticator"] = "externalbrowser"
        else:
            params["password"] = settings.SNOWFLAKE_PASSWORD

        self._connection = snowflake.connector.connect(**params, insecure_mode=True, login_timeout=120)
        logger.info("Snowflake connection established.")

    def get_connection(self):
        if not self._connection:
            self.connect()
        return self._connection

    def _is_alive(self) -> bool:
        try:
            cur = self._connection.cursor()
            cur.execute("SELECT 1")
            cur.close()
            return True
        except Exception:
            return False

    # ─────────────────────────────────────────────
    # Read queries (use shared connection directly)
    # ─────────────────────────────────────────────

    def query(
        self,
        sql: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Run a read query on the shared connection. `timeout`, when given, is
        a server-side statement timeout in seconds — Snowflake cancels the
        query if it runs longer, which bounds the blast radius of an expensive
        ad-hoc SELECT (e.g. an AI-authored draft_sql with an accidental
        cartesian join) instead of letting it saturate the warehouse."""
        conn = self.get_connection()
        try:
            cur = conn.cursor(DictCursor)
            try:
                if timeout is not None:
                    cur.execute(sql, params or {}, timeout=timeout)
                else:
                    cur.execute(sql, params or {})
                return cur.fetchall()
            finally:
                cur.close()
        except snowflake.connector.errors.OperationalError:
            # Connection dropped since the last call (idle timeout, network
            # blip) — reconnect once and retry rather than pre-checking
            # liveness on every call, which doubled round trips.
            logger.warning("Snowflake connection dropped — reconnecting and retrying query.")
            self._connection = None
            conn = self.get_connection()
            cur = conn.cursor(DictCursor)
            try:
                if timeout is not None:
                    cur.execute(sql, params or {}, timeout=timeout)
                else:
                    cur.execute(sql, params or {})
                return cur.fetchall()
            finally:
                cur.close()

    def execute(self, sql: str, params: Optional[Dict[str, Any]] = None) -> int:
        """
        Run one INSERT/UPDATE/DELETE (or any non-SELECT) statement with bind
        parameters, on the shared app-storage connection. Returns rowcount.
        Not used for scanned-source-table execution — that path is
        execute_with_context() below, which switches role/warehouse first.
        """
        with self._exec_lock:
            conn = self.get_connection()
            try:
                cur = conn.cursor()
                try:
                    cur.execute(sql, params or {})
                    return cur.rowcount
                finally:
                    cur.close()
            except snowflake.connector.errors.OperationalError:
                logger.warning("Snowflake connection dropped — reconnecting and retrying execute.")
                self._connection = None
                conn = self.get_connection()
                cur = conn.cursor()
                try:
                    cur.execute(sql, params or {})
                    return cur.rowcount
                finally:
                    cur.close()

    def describe_table(self, database: str, schema: str, table: str) -> List[Dict[str, Any]]:
        """
        Returns column info for a table using DESCRIBE TABLE.
        Each row has: name, type, nullable, default, primary_key, comment.
        """
        try:
            rows = self.query(f"DESCRIBE TABLE {database}.{schema}.{table}")
            return rows
        except Exception as e:
            logger.warning(f"DESCRIBE TABLE failed for {database}.{schema}.{table}: {e}")
            return []

    def ask_cortex(
        self,
        prompt: str,
        model: str = "claude-opus-4-8",
    ) -> str:
        """
        Call Snowflake Cortex COMPLETE() using the existing SSO connection.
        Uses claude-opus-4-8 by default (available in auto mode).
        Returns the text response string.
        Raises on failure so caller can fall back.
        """
        # Escape single quotes in prompt
        escaped = prompt.replace("'", "''")
        sql = f"SELECT SNOWFLAKE.CORTEX.COMPLETE('{model}', '{escaped}') AS response"
        rows = self.query(sql)
        if not rows:
            raise ValueError("Cortex returned no rows")
        response = rows[0].get("RESPONSE") or rows[0].get("response") or ""
        if not response:
            raise ValueError(f"Cortex returned empty response: {rows[0]}")
        return response

    # ─────────────────────────────────────────────
    # Execution with role+warehouse context
    # ─────────────────────────────────────────────

    def execute_with_context(self, sql: str, role: str, warehouse: str) -> None:
        """
        Execute `sql` under `role` + `warehouse` on the SAME connection,
        then restore the original role+warehouse.  A lock ensures no two
        executions interleave and corrupt session state.
        """
        with self._exec_lock:
            conn = self.get_connection()
            cur = conn.cursor(DictCursor)

            # Save current session state
            cur.execute(
                "SELECT CURRENT_ROLE() as r, CURRENT_WAREHOUSE() as w"
            )
            state = cur.fetchone() or {}
            orig_role = state.get("R") or state.get("r") or settings.SNOWFLAKE_ROLE
            orig_wh   = state.get("W") or state.get("w") or settings.SNOWFLAKE_WAREHOUSE

            try:
                logger.debug(f"USE ROLE {role}")
                cur.execute(f"USE ROLE {role}")
                logger.debug(f"USE WAREHOUSE {warehouse}")
                cur.execute(f"USE WAREHOUSE {warehouse}")
                clean_sql = _sanitize_sql(sql)
                logger.info(f"Executing SQL as {role}/{warehouse}: {clean_sql[:300]}")
                # Execute each statement individually — execute_string has known
                # parsing issues with multi-statement DDL+DML batches in Snowflake
                statements = [s.strip() for s in clean_sql.split(";") if s.strip()]
                for stmt in statements:
                    logger.debug(f"  Running statement: {stmt[:150]}")
                    cur.execute(stmt)
            finally:
                # Always restore — even if sql raises
                try:
                    cur.execute(f"USE ROLE {orig_role}")
                    if orig_wh:
                        cur.execute(f"USE WAREHOUSE {orig_wh}")
                except Exception as restore_err:
                    logger.warning(f"Failed to restore session state: {restore_err}")
                cur.close()

    # ─────────────────────────────────────────────
    # Startup context cache
    # ─────────────────────────────────────────────

    def warm_up(self) -> Dict[str, Any]:
        """
        Called once at startup after SSO login.
        Fetches user, roles, warehouses and databases and stores them in
        memory so every subsequent API call is instant.
        """
        logger.info("Warming up Snowflake context cache…")

        conn = self.get_connection()

        # --- current user + role ---
        rows = self.query(
            "SELECT CURRENT_USER() as u, CURRENT_ROLE() as r"
        )
        current_user = rows[0].get("U") or rows[0].get("u") or "" if rows else ""
        current_role = rows[0].get("R") or rows[0].get("r") or "" if rows else ""

        # --- roles: APPLICABLE_ROLES covers full hierarchy ---
        roles: List[Dict] = []
        try:
            role_rows = self.query(
                "SELECT ROLE_NAME, IS_DEFAULT, IS_CURRENT_ROLE "
                "FROM SNOWFLAKE.INFORMATION_SCHEMA.APPLICABLE_ROLES "
                "ORDER BY ROLE_NAME"
            )
            for r in role_rows:
                name = r.get("ROLE_NAME") or ""
                if name:
                    roles.append({
                        "name": name,
                        "is_current": r.get("IS_CURRENT_ROLE", "NO") == "YES",
                        "is_default": r.get("IS_DEFAULT", "NO") == "YES",
                    })
        except Exception:
            logger.warning("APPLICABLE_ROLES unavailable — falling back to SHOW GRANTS TO USER")
            try:
                grant_rows = self.query(
                    f"SHOW GRANTS TO USER \"{current_user}\""
                )
                seen: set = set()
                for r in grant_rows:
                    name = r.get("role") or r.get("ROLE") or ""
                    if name and name not in seen:
                        seen.add(name)
                        roles.append({
                            "name": name,
                            "is_current": name == current_role,
                            "is_default": False,
                        })
            except Exception as e2:
                logger.warning(f"SHOW GRANTS TO USER also failed: {e2}")

        # Sort: current first, default second, then alpha
        roles.sort(key=lambda x: (
            0 if x["is_current"] else 1 if x["is_default"] else 2,
            x["name"]
        ))

        # --- warehouses ---
        warehouses: List[Dict] = []
        try:
            wh_rows = self.query("SHOW WAREHOUSES")
            for r in wh_rows:
                name  = r.get("name")  or r.get("NAME")  or ""
                size  = r.get("size")  or r.get("SIZE")  or ""
                state = r.get("state") or r.get("STATE") or ""
                if name:
                    warehouses.append({
                        "name": name, "size": size, "state": state
                    })
        except Exception as e:
            logger.warning(f"Could not fetch warehouses: {e}")

        # --- databases ---
        databases: List[str] = []
        try:
            db_rows = self.query("SHOW DATABASES")
            for r in db_rows:
                name = r.get("name") or r.get("NAME") or ""
                if name:
                    databases.append(name)
        except Exception as e:
            logger.warning(f"Could not fetch databases: {e}")

        self._context_cache = {
            "user":        current_user,
            "current_role": current_role,
            "roles":       roles,
            "warehouses":  warehouses,
            "databases":   databases,
        }

        logger.info(
            f"Context cached — user={current_user}, "
            f"roles={len(roles)}, warehouses={len(warehouses)}, "
            f"databases={len(databases)}"
        )
        return self._context_cache

    def get_cached_context(self) -> Optional[Dict[str, Any]]:
        return self._context_cache


def _sanitize_sql(sql: str) -> str:
    """
    Last-resort safety net — same logic as cortex_client._sanitize_sql.
    Sanitization is also applied at source in cortex_client.py and ai_recommendations.py,
    so by the time SQL reaches here it should already be clean.
    """
    from app.services.cortex_client import _sanitize_sql as _cs
    return _cs(sql)


# Module-level singleton
session = SnowflakeSession()
