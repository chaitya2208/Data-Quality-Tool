"""
DataSource — the dialect-agnostic interface every backend adapter implements.

Callers (discovery endpoints, profiling_service, scan_service, coordinator,
AI-fix) talk to a resolved DataSource instead of issuing raw SQL. Each adapter
owns its dialect specifics (SHOW vs information_schema, quoting, row counts,
type spellings). All row dicts returned here use **lowercase** keys.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class DataSource(ABC):
    """One live connection to a data source, built from a Connection row."""

    # Per-dialect type spellings so profiling category detection is portable.
    # Adapters override with their own prefixes.
    numeric_type_prefixes: tuple = ("NUMBER", "DECIMAL", "INT", "FLOAT", "DOUBLE")
    date_type_prefixes: tuple    = ("DATE", "TIME", "TIMESTAMP")
    text_type_prefixes: tuple    = ("VARCHAR", "CHAR", "STRING", "TEXT")

    # ── connection / status ────────────────────────────────────────────────
    @abstractmethod
    def test_connection(self) -> Dict[str, Any]:
        """Return {ok: bool, user: str|None, detail: str|None}. Never raises."""

    # ── discovery ──────────────────────────────────────────────────────────
    @abstractmethod
    def list_databases(self) -> List[str]:
        ...

    @abstractmethod
    def list_schemas(self, database: str) -> List[str]:
        ...

    @abstractmethod
    def list_tables(self, database: str, schema: str) -> List[Dict[str, Any]]:
        """[{name, row_count, bytes, kind, owner, comment}]."""

    @abstractmethod
    def list_columns(self, database: str, schema: str, table: str) -> List[Dict[str, Any]]:
        """[{column_name, data_type, is_nullable(bool), primary_key(bool), unique_key(bool), comment}]."""

    @abstractmethod
    def table_info(self, database: str, schema: str, table: str) -> Dict[str, Any]:
        """{name, row_count, bytes, kind, owner, comment}."""

    # ── profiling primitives ─────────────────────────────────────────────────
    @abstractmethod
    def column_stats(self, database: str, schema: str, table: str,
                     columns: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """
        {column_name: {total, non_null, distinct_count, min_value, max_value,
        avg_value, stddev}} over the FULL table, batched into as few scans as
        possible. `columns` items carry at least column_name + data_type.
        """

    @abstractmethod
    def top_values(self, database: str, schema: str, table: str, column: str,
                   limit: int = 5) -> List[Dict[str, Any]]:
        """[{value, count}] most-frequent non-null values."""

    @abstractmethod
    def duplicate_count(self, database: str, schema: str, table: str, column: str) -> Optional[int]:
        """Rows sharing a non-null value on this column (candidate-key dup check)."""

    @abstractmethod
    def sample_values(self, database: str, schema: str, table: str, column: str,
                      limit: int = 200) -> List[Any]:
        """A sample of non-null values (used for email/phone pattern matching)."""

    # ── mutation (AI-fix) ─────────────────────────────────────────────────────
    @abstractmethod
    def execute_sql(self, sql: str, *, role: Optional[str] = None,
                    warehouse: Optional[str] = None) -> None:
        """Run a mutating statement. role/warehouse honored only where meaningful."""

    # ── AI helper ─────────────────────────────────────────────────────────────
    def ask_ai(self, prompt: str) -> Optional[str]:
        """
        Optional native LLM (e.g. Snowflake Cortex). Return None to signal the
        caller should use the Bedrock/Claude fallback. Default: no native AI.
        """
        return None
