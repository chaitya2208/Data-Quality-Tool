"""
Deterministic Statistical Profiler — computes objective, mechanically-
derivable facts about a table's columns from live Snowflake data, once per
table, reused by everything downstream (RuleIntelligenceAgent's prompt AND
its deterministic candidate generation).

This exists because objective facts (a PK-shaped column has duplicates; a
timestamp column is stale; a value isn't in the observed set) were
previously buried inside an unstructured stats blob competing for attention
against everything else in one LLM judgment pass — so whether Claude
noticed them was inconsistent across otherwise-identical tables. Computing
them here guarantees they surface every run, without hardcoding anything
about the specific columns/domain of any one table: PK-shape and date-shape
detection reuse the same generic naming-convention regexes dynamic_rules.py
already uses for its column-level checks, not new table-specific heuristics.

Freshness is surfaced as a signal only (max value + age) — never an
auto-generated check with a made-up staleness threshold, since "how stale is
too stale" is a business judgment call, not a deterministic fact.
"""
import logging
import re
from typing import Any, Dict, List

from app.services.snowflake_session import session as sf_session

logger = logging.getLogger(__name__)

# Same PK-shape pattern dynamic_rules.py uses (check_no_primary_key /
# check_nullable_id_column) — reused, not reinvented, so "looks like a key"
# means the same thing everywhere in this codebase.
_PK_SHAPE_RE = re.compile(r"(^ID$|_ID$|^PK_|_PK$|_KEY$|_SEQ$|_SURROGATE)", re.I)

# Same date/timestamp-shaped type groups dynamic_rules.py's NAME_TYPE_RULES
# checks against.
_DATE_TYPES = {"DATE"}
_TIMESTAMP_TYPES = {"TIMESTAMP_NTZ", "TIMESTAMP_LTZ", "TIMESTAMP_TZ", "TIMESTAMP", "DATETIME"}
_TEMPORAL_TYPES = _DATE_TYPES | _TIMESTAMP_TYPES

_CLOSED_SET_MAX_DISTINCT = 50
_CLOSED_SET_QUERY_LIMIT = 200
_MAX_PROFILED_COLUMNS = 20  # matches the cap the old _fetch_column_stats used


class DeterministicProfilerAgent:
    """
    One profiling pass per table, reused by RuleIntelligenceAgent instead of
    each caller re-querying Snowflake for the same per-column stats.
    """

    def run(self, table_asset: Any, column_assets: List[Any]) -> Dict[str, Any]:
        fqn = table_asset.fqn
        column_stats: Dict[str, dict] = {}
        pk_shaped_candidates: List[dict] = []
        freshness_signals: List[dict] = []
        closed_set_columns: Dict[str, dict] = {}

        for col in column_assets[:_MAX_PROFILED_COLUMNS]:
            name = col.column_name
            if not name:
                continue

            stat = self._fetch_basic_stats(fqn, name)
            if stat is None:
                continue
            column_stats[name] = stat

            if _PK_SHAPE_RE.search(name.upper()):
                total = stat["total"]
                non_null_total = total - stat["nulls"]
                distinct = stat["distinct"]
                pk_shaped_candidates.append({
                    "column": name,
                    "total": total,
                    "non_null_total": non_null_total,
                    "distinct": distinct,
                    "is_unique": distinct >= non_null_total if non_null_total else True,
                    "duplicate_rows": max(non_null_total - distinct, 0),
                })

            data_type = self._column_data_type(col)
            if data_type in _TEMPORAL_TYPES:
                fresh = self._fetch_freshness(fqn, name)
                if fresh is not None:
                    freshness_signals.append({
                        "signal_id": f"freshness:{name}",
                        "column": name,
                        "data_type": data_type,
                        "max_value": fresh["max_value"],
                        "age_days": fresh["age_days"],
                    })

            if stat["distinct"] and stat["distinct"] <= _CLOSED_SET_MAX_DISTINCT:
                values = self._fetch_distinct_values(fqn, name)
                if values is not None:
                    closed_set_columns[name] = {"values": values, "distinct_count": stat["distinct"]}

        return {
            "column_stats": column_stats,
            "pk_shaped_candidates": pk_shaped_candidates,
            "freshness_signals": freshness_signals,
            "closed_set_columns": closed_set_columns,
        }

    # ── Per-column queries ────────────────────────────────────────────────

    def _fetch_basic_stats(self, fqn: str, column_name: str) -> Dict[str, Any] | None:
        try:
            rows = sf_session.query(
                f"""
                SELECT
                    COUNT(*) AS TOTAL,
                    COUNT_IF({column_name} IS NULL) AS NULLS,
                    COUNT(DISTINCT {column_name}) AS DISTINCT_COUNT
                FROM {fqn}
                """
            )
            if not rows:
                return None
            row = rows[0]
            total = row.get("TOTAL", 0) or 0
            nulls = row.get("NULLS", 0) or 0
            distinct = row.get("DISTINCT_COUNT", 0) or 0
            null_pct = round((nulls / total * 100), 1) if total else 0.0

            top_rows = sf_session.query(
                f"""
                SELECT {column_name} AS VAL, COUNT(*) AS CNT
                FROM {fqn}
                WHERE {column_name} IS NOT NULL
                GROUP BY {column_name}
                ORDER BY CNT DESC
                LIMIT 5
                """
            )
            top_values = [{"value": r["VAL"], "count": r["CNT"]} for r in top_rows]

            return {
                "total": total, "nulls": nulls, "null_pct": null_pct,
                "distinct": distinct, "top_values": top_values,
            }
        except Exception as e:
            logger.debug(f"[Profiler] Skipping stats for column {column_name}: {e}")
            return None

    def _fetch_freshness(self, fqn: str, column_name: str) -> Dict[str, Any] | None:
        try:
            rows = sf_session.query(
                f"""
                SELECT MAX({column_name}) AS MAX_VAL,
                       DATEDIFF('day', MAX({column_name}), CURRENT_TIMESTAMP()) AS AGE_DAYS
                FROM {fqn}
                """
            )
            if not rows or rows[0].get("MAX_VAL") is None:
                return None
            return {"max_value": str(rows[0]["MAX_VAL"]), "age_days": rows[0].get("AGE_DAYS")}
        except Exception as e:
            logger.debug(f"[Profiler] Skipping freshness for column {column_name}: {e}")
            return None

    def _fetch_distinct_values(self, fqn: str, column_name: str) -> List[Any] | None:
        try:
            rows = sf_session.query(
                f"""
                SELECT DISTINCT {column_name} AS VAL
                FROM {fqn}
                WHERE {column_name} IS NOT NULL
                LIMIT {_CLOSED_SET_QUERY_LIMIT}
                """
            )
            return [r["VAL"] for r in rows]
        except Exception as e:
            logger.debug(f"[Profiler] Skipping closed-set fetch for column {column_name}: {e}")
            return None

    @staticmethod
    def _column_data_type(col: Any) -> str:
        meta = col.raw_metadata or {}
        return str(meta.get("data_type", "")).upper()
