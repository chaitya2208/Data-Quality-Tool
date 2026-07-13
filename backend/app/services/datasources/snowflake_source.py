"""
SnowflakeSource — DataSource adapter over the existing Snowflake session.

Wraps app.services.snowflake_session.session (the SSO singleton) and the SQL
shapes previously inlined in profiling_service / assets / scan_service:
SHOW TABLES/SCHEMAS/DATABASES, DESCRIBE TABLE, 3-part db.INFORMATION_SCHEMA.X,
Cortex, and USE ROLE / USE WAREHOUSE for mutating execution.

Normalizes all returned rows to lowercase keys.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from app.services.datasources.base import DataSource
from app.services.snowflake_session import session as sf_session

logger = logging.getLogger(__name__)

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


class SnowflakeSource(DataSource):
    numeric_type_prefixes = ("NUMBER", "DECIMAL", "INT", "FLOAT", "DOUBLE")
    date_type_prefixes    = ("DATE", "TIME", "TIMESTAMP")
    text_type_prefixes    = ("VARCHAR", "CHAR", "STRING", "TEXT")

    _MIN_MAX_PREFIXES = numeric_type_prefixes + text_type_prefixes + date_type_prefixes
    _AGG_BATCH_SIZE = 40

    def __init__(self, conn):
        # conn is the Connection ORM row. The Snowflake session is a process
        # singleton driven by .env, so we don't rebuild per-connection here —
        # we reuse the established SSO session (matches prior behavior).
        self.conn = conn
        # Snowflake ignores role/warehouse args on plain queries; execute uses them.
        extra = conn.extra or {} if conn else {}
        self._role = extra.get("role")
        self._warehouse = extra.get("warehouse")

    # ── identifiers ─────────────────────────────────────────────────────────
    def quote_ident(self, name: str) -> str:
        if _IDENT_RE.match(name or ""):
            return name
        return '"' + (name or "").replace('"', '""') + '"'

    def _fqn(self, database: str, schema: str, table: str) -> str:
        return f"{self.quote_ident(database)}.{self.quote_ident(schema)}.{self.quote_ident(table)}"

    # ── connection / status ──────────────────────────────────────────────────
    def test_connection(self) -> Dict[str, Any]:
        try:
            rows = sf_session.query("SELECT CURRENT_USER() AS u, CURRENT_ROLE() AS r")
            r = rows[0] if rows else {}
            return {"ok": True, "user": r.get("U") or r.get("u"), "detail": None}
        except Exception as e:
            return {"ok": False, "user": None, "detail": str(e)}

    # ── discovery ─────────────────────────────────────────────────────────────
    def list_databases(self) -> List[str]:
        ctx = sf_session.get_cached_context()
        if ctx and ctx.get("databases"):
            return list(ctx["databases"])
        rows = sf_session.query("SHOW DATABASES")
        return [r.get("name") or r.get("NAME") for r in rows if r.get("name") or r.get("NAME")]

    def list_schemas(self, database: str) -> List[str]:
        rows = sf_session.query(f"SHOW SCHEMAS IN DATABASE {self.quote_ident(database)}")
        names = [r.get("name") or r.get("NAME") for r in rows if r.get("name") or r.get("NAME")]
        return [n for n in names if n.upper() != "INFORMATION_SCHEMA"]

    def list_tables(self, database: str, schema: str) -> List[Dict[str, Any]]:
        rows = sf_session.query(f"SHOW TABLES IN {self.quote_ident(database)}.{self.quote_ident(schema)}")
        out = []
        for r in rows:
            name = r.get("name") or r.get("NAME")
            if not name:
                continue
            out.append({
                "name":      name,
                "row_count": r.get("rows"),
                "bytes":     r.get("bytes"),
                "kind":      r.get("kind"),
                "owner":     r.get("owner"),
                "comment":   r.get("comment"),
            })
        return out

    def list_columns(self, database: str, schema: str, table: str) -> List[Dict[str, Any]]:
        rows = sf_session.query(f"""
            SELECT column_name    AS COLUMN_NAME,
                   data_type      AS DATA_TYPE,
                   is_nullable    AS IS_NULLABLE,
                   comment        AS COMMENT,
                   ordinal_position AS ORDINAL_POSITION
            FROM {self.quote_ident(database)}.INFORMATION_SCHEMA.COLUMNS
            WHERE table_schema = '{schema}' AND table_name = '{table}'
            ORDER BY ordinal_position
        """)
        cols = [{
            "column_name": r.get("COLUMN_NAME"),
            "data_type":   r.get("DATA_TYPE"),
            "is_nullable": str(r.get("IS_NULLABLE", "YES")).upper() in ("Y", "YES"),
            "primary_key": False,
            "unique_key":  False,
            "comment":     r.get("COMMENT"),
        } for r in rows]

        # PK / unique via DESCRIBE TABLE (lowercase 'primary key' / 'unique key')
        try:
            described = sf_session.describe_table(database, schema, table)

            def _truthy(row, *keys):
                return any(str(row.get(k, "")).upper() in ("Y", "YES", "TRUE") for k in keys)

            pk  = {(r.get("name") or "").upper() for r in described if _truthy(r, "primary key", "PRIMARY KEY")}
            uq  = {(r.get("name") or "").upper() for r in described if _truthy(r, "unique key", "UNIQUE KEY")}
            for c in cols:
                nm = (c["column_name"] or "").upper()
                if nm in pk: c["primary_key"] = True
                if nm in uq: c["unique_key"] = True
        except Exception as e:
            logger.warning(f"[SnowflakeSource] key detection failed for {database}.{schema}.{table}: {e}")
        return cols

    def table_info(self, database: str, schema: str, table: str) -> Dict[str, Any]:
        try:
            rows = sf_session.query(f"SHOW TABLES LIKE '{table}' IN {self.quote_ident(database)}.{self.quote_ident(schema)}")
            m = next((r for r in rows if (r.get("name") or "").upper() == table.upper()), {})
        except Exception as e:
            logger.warning(f"[SnowflakeSource] table_info failed: {e}")
            m = {}
        return {
            "name": table, "row_count": m.get("rows"), "bytes": m.get("bytes"),
            "kind": m.get("kind"), "owner": m.get("owner"), "comment": m.get("comment"),
        }

    # ── profiling primitives ──────────────────────────────────────────────────
    def column_stats(self, database: str, schema: str, table: str,
                     columns: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        fqn = self._fqn(database, schema, table)
        results: Dict[str, Dict[str, Any]] = {}

        def _exprs(idx, col, prefix):
            p = [f"COUNT(*) AS C{idx}_TOTAL", f"COUNT({col}) AS C{idx}_NON_NULL",
                 f"COUNT(DISTINCT {col}) AS C{idx}_DISTINCT"]
            if prefix in self._MIN_MAX_PREFIXES:
                p += [f"MIN({col}) AS C{idx}_MIN", f"MAX({col}) AS C{idx}_MAX"]
            if prefix in self.numeric_type_prefixes:
                p += [f"AVG({col}) AS C{idx}_AVG", f"STDDEV({col}) AS C{idx}_STDDEV"]
            return p

        def _unpack(row, idx, prefix):
            mm = prefix in self._MIN_MAX_PREFIXES
            num = prefix in self.numeric_type_prefixes
            return {
                "total": row.get(f"C{idx}_TOTAL", 0) or 0,
                "non_null": row.get(f"C{idx}_NON_NULL", 0) or 0,
                "distinct_count": row.get(f"C{idx}_DISTINCT", 0) or 0,
                "min_value": row.get(f"C{idx}_MIN") if mm else None,
                "max_value": row.get(f"C{idx}_MAX") if mm else None,
                "avg_value": row.get(f"C{idx}_AVG") if num else None,
                "stddev": row.get(f"C{idx}_STDDEV") if num else None,
            }

        for start in range(0, len(columns), self._AGG_BATCH_SIZE):
            batch = columns[start:start + self._AGG_BATCH_SIZE]
            parts, meta = [], []
            for i, cm in enumerate(batch):
                name = cm["column_name"]
                prefix = (cm.get("data_type") or "").split("(")[0].upper()
                parts += _exprs(i, self.quote_ident(name), prefix)
                meta.append((i, name, prefix))
            try:
                row = sf_session.query(f"SELECT {', '.join(parts)} FROM {fqn}")[0]
                for idx, name, prefix in meta:
                    results[name] = _unpack(row, idx, prefix)
            except Exception as e:
                logger.warning(f"[SnowflakeSource] batch agg failed ({e}); per-column fallback")
                for idx, name, prefix in meta:
                    try:
                        single = sf_session.query(f"SELECT {', '.join(_exprs(idx, self.quote_ident(name), prefix))} FROM {fqn}")[0]
                        results[name] = _unpack(single, idx, prefix)
                    except Exception as e2:
                        logger.warning(f"[SnowflakeSource] column {name} agg failed: {e2}")
                        results[name] = {"error": str(e2), "total": 0, "non_null": 0,
                                         "distinct_count": 0, "min_value": None, "max_value": None,
                                         "avg_value": None, "stddev": None}
        return results

    def top_values(self, database: str, schema: str, table: str, column: str, limit: int = 5) -> List[Dict[str, Any]]:
        col = self.quote_ident(column)
        rows = sf_session.query(f"""
            SELECT {col} AS VALUE, COUNT(*) AS OCCURRENCES
            FROM {self._fqn(database, schema, table)}
            WHERE {col} IS NOT NULL GROUP BY {col}
            ORDER BY OCCURRENCES DESC LIMIT {int(limit)}
        """)
        return [{"value": r.get("VALUE"), "count": r.get("OCCURRENCES")} for r in rows]

    def duplicate_count(self, database: str, schema: str, table: str, column: str) -> Optional[int]:
        col = self.quote_ident(column)
        try:
            rows = sf_session.query(f"""
                SELECT COUNT(*) AS DUP_ROWS FROM (
                    SELECT {col} FROM {self._fqn(database, schema, table)}
                    WHERE {col} IS NOT NULL GROUP BY {col} HAVING COUNT(*) > 1
                )
            """)
            return rows[0].get("DUP_ROWS", 0) or 0
        except Exception:
            return None

    def sample_values(self, database: str, schema: str, table: str, column: str, limit: int = 200) -> List[Any]:
        col = self.quote_ident(column)
        try:
            rows = sf_session.query(f"SELECT {col} AS V FROM {self._fqn(database, schema, table)} WHERE {col} IS NOT NULL LIMIT {int(limit)}")
            return [r.get("V") for r in rows if r.get("V") is not None]
        except Exception:
            return []

    # ── mutation ──────────────────────────────────────────────────────────────
    def execute_sql(self, sql: str, *, role: Optional[str] = None, warehouse: Optional[str] = None) -> None:
        sf_session.execute_with_context(
            sql,
            role=role or self._role,
            warehouse=warehouse or self._warehouse,
        )

    # ── native AI ─────────────────────────────────────────────────────────────
    def ask_ai(self, prompt: str) -> Optional[str]:
        try:
            return sf_session.ask_cortex(prompt, model="claude-opus-4-8")
        except Exception as e:
            logger.warning(f"[SnowflakeSource] Cortex failed ({e}); caller should fall back to Bedrock")
            return None
