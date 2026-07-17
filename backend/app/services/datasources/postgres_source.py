"""
PostgresSource — DataSource adapter for AWS RDS Postgres (and any Postgres).

Uses psycopg (v3). A Postgres connection is scoped to a single database, so
list_databases() returns just the connection's DB. Discovery/profiling use
information_schema + pg_catalog. execute_sql runs plainly (no role/warehouse).

All returned rows use lowercase keys, matching the DataSource contract.
"""
from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

import psycopg

from app.services.datasources.base import DataSource

logger = logging.getLogger(__name__)

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class PostgresSource(DataSource):
    # Postgres type spellings (information_schema.columns.data_type)
    numeric_type_prefixes = ("SMALLINT", "INTEGER", "BIGINT", "DECIMAL", "NUMERIC",
                             "REAL", "DOUBLE", "INT", "SERIAL", "MONEY")
    date_type_prefixes    = ("DATE", "TIME", "TIMESTAMP")
    text_type_prefixes    = ("CHARACTER", "VARCHAR", "CHAR", "TEXT")

    _MIN_MAX_PREFIXES = numeric_type_prefixes + text_type_prefixes + date_type_prefixes
    _AGG_BATCH_SIZE = 40

    def __init__(self, conn):
        self.conn_row = conn
        self._database = conn.database
        self._default_schema = conn.schema_ or "public"
        self._password = None  # resolved lazily once, then cached (see _resolve_password)
        self._session_cx = None  # one reusable connection held during a profiling pass

    # ── connection ────────────────────────────────────────────────────────────
    def _resolve_password(self):
        """
        The stored SECRET is normally a Secrets Manager pointer — resolve it to
        the real password. A non-pointer value is treated as a legacy plaintext
        password (defensive; migration replaces these with pointers). Raises
        loudly if a pointer can't be fetched — never silently connects blank.

        Cached on the instance so we hit Secrets Manager ONCE, not on every
        _connect(). Profiling/discovery open many short-lived connections; an
        AWS round-trip per connection added seconds of latency. The registry
        caches this source per connection and evicts it (clear_cached_source)
        when the connection is edited, so a rotated password rebuilds the source.
        """
        if self._password is not None:
            return self._password
        from app.services import secrets_manager
        secret = self.conn_row.secret
        if secrets_manager.is_pointer(secret):
            self._password = secrets_manager.get_secret(secret)
        else:
            self._password = secret
        return self._password

    def _connect(self):
        extra = self.conn_row.extra or {}
        return psycopg.connect(
            host=self.conn_row.host,
            port=self.conn_row.port or 5432,
            dbname=self.conn_row.database,
            user=self.conn_row.username,
            password=self._resolve_password(),
            sslmode=extra.get("sslmode", "require"),
            connect_timeout=extra.get("connect_timeout", 10),
        )

    @contextmanager
    def profiling_session(self):
        """
        Hold ONE connection open for a profiling pass so the dozen+ per-table
        stat queries don't each pay a fresh TLS handshake to RDS. _query reuses
        self._session_cx while it's set. Nested use is a no-op (keeps the
        outermost session). Always torn down, even on error.
        """
        if self._session_cx is not None:
            yield  # already inside a session — reuse it
            return
        cx = self._connect()
        self._session_cx = cx
        try:
            yield
        finally:
            self._session_cx = None
            try:
                cx.close()
            except Exception as e:
                logger.warning(f"[PostgresSource] closing profiling session failed: {e}")

    def _query(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        # Reuse the open profiling-session connection when present; otherwise
        # open a short-lived one (context-managed → auto-closed).
        if self._session_cx is not None:
            with self._session_cx.cursor() as cur:
                cur.execute(sql, params)
                if cur.description is None:
                    return []
                cols = [d[0].lower() for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
        with self._connect() as cx:
            with cx.cursor() as cur:
                cur.execute(sql, params)
                if cur.description is None:
                    return []
                cols = [d[0].lower() for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]

    def quote_ident(self, name: str) -> str:
        if _IDENT_RE.match(name or ""):
            return name
        return '"' + (name or "").replace('"', '""') + '"'

    def _rel(self, schema: str, table: str) -> str:
        # Postgres references are schema.table within the connected database.
        return f"{self.quote_ident(schema)}.{self.quote_ident(table)}"

    # ── status ──────────────────────────────────────────────────────────────
    def test_connection(self) -> Dict[str, Any]:
        try:
            rows = self._query("SELECT current_user AS u")
            return {"ok": True, "user": rows[0]["u"] if rows else None, "detail": None}
        except Exception as e:
            return {"ok": False, "user": None, "detail": str(e)}

    # ── discovery ─────────────────────────────────────────────────────────────
    def list_databases(self) -> List[str]:
        # A pg connection is bound to one DB; expose that DB only.
        return [self._database] if self._database else []

    def list_schemas(self, database: str) -> List[str]:
        rows = self._query("""
            SELECT schema_name FROM information_schema.schemata
            WHERE schema_name NOT IN ('pg_catalog','information_schema')
              AND schema_name NOT LIKE 'pg_%%'
            ORDER BY schema_name
        """)
        return [r["schema_name"] for r in rows]

    def list_tables(self, database: str, schema: str) -> List[Dict[str, Any]]:
        rows = self._query("""
            SELECT c.relname AS name,
                   c.relkind AS relkind,
                   c.reltuples::bigint AS row_count,
                   pg_total_relation_size(c.oid) AS bytes,
                   pg_get_userbyid(c.relowner) AS owner,
                   obj_description(c.oid) AS comment
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = %s AND c.relkind IN ('r','p','v','m')
            ORDER BY c.relname
        """, (schema,))
        kind_map = {"r": "TABLE", "p": "TABLE", "v": "VIEW", "m": "MATERIALIZED VIEW"}
        return [{
            "name": r["name"],
            "row_count": max(int(r["row_count"]), 0) if r["row_count"] is not None else None,
            "bytes": r["bytes"],
            "kind": kind_map.get(r["relkind"], r["relkind"]),
            "owner": r["owner"],
            "comment": r["comment"],
        } for r in rows]

    def list_columns(self, database: str, schema: str, table: str) -> List[Dict[str, Any]]:
        cols = self._query("""
            SELECT column_name, data_type, is_nullable, ordinal_position,
                   col_description(
                     (quote_ident(%s) || '.' || quote_ident(%s))::regclass,
                     ordinal_position
                   ) AS comment
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            ORDER BY ordinal_position
        """, (schema, table, schema, table))

        # PK / unique from constraints
        keyrows = self._query("""
            SELECT kcu.column_name, tc.constraint_type
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
            WHERE tc.table_schema = %s AND tc.table_name = %s
              AND tc.constraint_type IN ('PRIMARY KEY','UNIQUE')
        """, (schema, table))
        pk = {r["column_name"] for r in keyrows if r["constraint_type"] == "PRIMARY KEY"}
        uq = {r["column_name"] for r in keyrows if r["constraint_type"] == "UNIQUE"}

        return [{
            "column_name": c["column_name"],
            "data_type": c["data_type"],
            "is_nullable": str(c["is_nullable"]).upper() in ("YES", "Y"),
            "primary_key": c["column_name"] in pk,
            "unique_key": c["column_name"] in uq,
            "comment": c["comment"],
        } for c in cols]

    def table_info(self, database: str, schema: str, table: str) -> Dict[str, Any]:
        rows = self.list_tables(database, schema)
        m = next((t for t in rows if t["name"] == table), {})
        row_count = m.get("row_count")
        # pg_class.reltuples is a planner estimate that stays 0 (or -1 on PG14+)
        # until ANALYZE/VACUUM runs — freshly-loaded tables report 0. For the
        # single-table view, get an exact COUNT(*) when the estimate is unusable.
        if not row_count or row_count <= 0:
            try:
                cnt = self._query(f"SELECT COUNT(*) AS n FROM {self._rel(schema, table)}")
                row_count = cnt[0]["n"] if cnt else row_count
            except Exception as e:
                logger.warning(f"[PostgresSource] exact count failed for {schema}.{table}: {e}")
        return {
            "name": table, "row_count": row_count, "bytes": m.get("bytes"),
            "kind": m.get("kind"), "owner": m.get("owner"), "comment": m.get("comment"),
        }

    # ── profiling primitives ──────────────────────────────────────────────────
    def column_stats(self, database: str, schema: str, table: str,
                     columns: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        rel = self._rel(schema, table)
        results: Dict[str, Dict[str, Any]] = {}

        def _exprs(idx, col, prefix):
            p = [f"COUNT(*) AS c{idx}_total", f"COUNT({col}) AS c{idx}_non_null",
                 f"COUNT(DISTINCT {col}) AS c{idx}_distinct"]
            if prefix in self._MIN_MAX_PREFIXES:
                p += [f"MIN({col}) AS c{idx}_min", f"MAX({col}) AS c{idx}_max"]
            if prefix in self.numeric_type_prefixes:
                p += [f"AVG({col}::double precision) AS c{idx}_avg",
                      f"STDDEV({col}::double precision) AS c{idx}_stddev"]
            return p

        def _unpack(row, idx, prefix):
            mm = prefix in self._MIN_MAX_PREFIXES
            num = prefix in self.numeric_type_prefixes
            return {
                "total": row.get(f"c{idx}_total", 0) or 0,
                "non_null": row.get(f"c{idx}_non_null", 0) or 0,
                "distinct_count": row.get(f"c{idx}_distinct", 0) or 0,
                "min_value": row.get(f"c{idx}_min") if mm else None,
                "max_value": row.get(f"c{idx}_max") if mm else None,
                "avg_value": row.get(f"c{idx}_avg") if num else None,
                "stddev": row.get(f"c{idx}_stddev") if num else None,
            }

        for start in range(0, len(columns), self._AGG_BATCH_SIZE):
            batch = columns[start:start + self._AGG_BATCH_SIZE]
            parts, meta = [], []
            for i, cm in enumerate(batch):
                name = cm["column_name"]
                prefix = (cm.get("data_type") or "").split("(")[0].strip().upper()
                parts += _exprs(i, self.quote_ident(name), prefix)
                meta.append((i, name, prefix))
            try:
                rows = self._query(f"SELECT {', '.join(parts)} FROM {rel}")
                row = rows[0] if rows else {}
                for idx, name, prefix in meta:
                    results[name] = _unpack(row, idx, prefix)
            except Exception as e:
                logger.warning(f"[PostgresSource] batch agg failed ({e}); per-column fallback")
                for idx, name, prefix in meta:
                    try:
                        r = self._query(f"SELECT {', '.join(_exprs(idx, self.quote_ident(name), prefix))} FROM {rel}")
                        results[name] = _unpack(r[0] if r else {}, idx, prefix)
                    except Exception as e2:
                        logger.warning(f"[PostgresSource] column {name} agg failed: {e2}")
                        results[name] = {"error": str(e2), "total": 0, "non_null": 0,
                                         "distinct_count": 0, "min_value": None, "max_value": None,
                                         "avg_value": None, "stddev": None}
        return results

    def top_values(self, database: str, schema: str, table: str, column: str, limit: int = 5) -> List[Dict[str, Any]]:
        col = self.quote_ident(column)
        rows = self._query(f"""
            SELECT {col} AS value, COUNT(*) AS count
            FROM {self._rel(schema, table)}
            WHERE {col} IS NOT NULL GROUP BY {col}
            ORDER BY count DESC LIMIT {int(limit)}
        """)
        return [{"value": r["value"], "count": r["count"]} for r in rows]

    def bottom_values(self, database: str, schema: str, table: str, column: str, limit: int = 5) -> List[Dict[str, Any]]:
        col = self.quote_ident(column)
        rows = self._query(f"""
            SELECT {col} AS value, COUNT(*) AS count
            FROM {self._rel(schema, table)}
            WHERE {col} IS NOT NULL GROUP BY {col}
            ORDER BY count ASC LIMIT {int(limit)}
        """)
        return [{"value": r["value"], "count": r["count"]} for r in rows]

    def duplicate_count(self, database: str, schema: str, table: str, column: str) -> Optional[int]:
        col = self.quote_ident(column)
        try:
            rows = self._query(f"""
                SELECT COUNT(*) AS dup_rows FROM (
                    SELECT {col} FROM {self._rel(schema, table)}
                    WHERE {col} IS NOT NULL GROUP BY {col} HAVING COUNT(*) > 1
                ) d
            """)
            return rows[0]["dup_rows"] if rows else 0
        except Exception:
            return None

    def sample_values(self, database: str, schema: str, table: str, column: str, limit: int = 200) -> List[Any]:
        col = self.quote_ident(column)
        try:
            rows = self._query(f"SELECT {col} AS v FROM {self._rel(schema, table)} WHERE {col} IS NOT NULL LIMIT {int(limit)}")
            return [r["v"] for r in rows if r.get("v") is not None]
        except Exception:
            return []

    # ── mutation ──────────────────────────────────────────────────────────────
    def execute_sql(self, sql: str, *, role: Optional[str] = None, warehouse: Optional[str] = None) -> None:
        # role/warehouse are Snowflake concepts — ignored for Postgres.
        with self._connect() as cx:
            with cx.cursor() as cur:
                for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
                    cur.execute(stmt)
            cx.commit()

    def query(self, sql: str) -> List[Dict[str, Any]]:
        """Read-only query (lowercase keys) — used to run sql_template checks."""
        return self._query(sql)
