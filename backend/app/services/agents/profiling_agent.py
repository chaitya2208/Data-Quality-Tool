"""
Profiling Agent — runs in the parallel group (with Metadata, Rules Fetch,
Relationship Discovery).

Single source of profiling truth. One pass over the table via the DataSource
abstraction (`profile_table`, portable across Snowflake + Postgres) produces
BOTH:

  1. The rich per-column stats + anomaly narrative used by the UI / Data
     Explorer (`profile` + `profile["anomalies"]`).
  2. The deterministic FACTS consumed by RuleIntelligenceAgent to drive
     deterministic rule-candidate generation:
        column_stats, pk_shaped_candidates, freshness_signals, closed_set_columns

This merges what used to be two agents (ProfilingAgent + the sequential,
Snowflake-only DeterministicProfilerAgent). The facts are now derived IN PYTHON
from the profile the UI already computes — no second round of per-column
queries, no dialect-specific SQL (the old DATEDIFF/COUNT_IF are gone), and it
works for every DataSource. Because everything comes from (database, schema,
table, connection_id) + the source, this runs in parallel — it no longer waits
for column metadata to be persisted.

The deterministic facts exist because objective signals (a PK-shaped column has
duplicates; a timestamp column is stale; a value isn't in the observed set)
must surface every run rather than being buried in one unstructured LLM pass.
PK-shape / temporal detection reuse the same generic regexes/type groups
dynamic_rules.py uses — not table-specific heuristics.
"""
import logging
import re
from typing import Any, Dict, List, Optional

from app.services.profiling_service import profile_table

logger = logging.getLogger(__name__)

# Same PK-shape pattern dynamic_rules.py uses (check_no_primary_key /
# check_nullable_id_column) — reused so "looks like a key" means the same thing
# everywhere in this codebase.
_PK_SHAPE_RE = re.compile(r"(^ID$|_ID$|^PK_|_PK$|_KEY$|_SEQ$|_SURROGATE)", re.I)

# Same date/timestamp-shaped type groups dynamic_rules.py's NAME_TYPE_RULES uses.
_DATE_TYPES = {"DATE"}
_TIMESTAMP_TYPES = {"TIMESTAMP_NTZ", "TIMESTAMP_LTZ", "TIMESTAMP_TZ", "TIMESTAMP", "DATETIME"}
_TEMPORAL_TYPES = _DATE_TYPES | _TIMESTAMP_TYPES

_CLOSED_SET_MAX_DISTINCT = 50
_CLOSED_SET_SAMPLE_LIMIT = 200

# Cardinality ceiling for tail-value fetch. Above this we skip — the tail on a
# high-cardinality column is a bag of one-offs that reveals nothing about the
# domain, and pulling it costs a full scan.
_TAIL_VALUES_MAX_DISTINCT = 200
_TAIL_VALUES_LIMIT = 5


class ProfilingAgent:
    def __init__(self, db=None):
        self.db = db  # unused — kept for interface symmetry with other agents

    def run(self, database: str, schema: str, table: str, connection_id: str = None) -> Dict[str, Any]:
        logger.info(f"[ProfilingAgent] Profiling {database}.{schema}.{table}")
        from app.services.datasources import get_source
        source = get_source(connection_id)

        # One profiling pass (portable) — one open connection for the whole pass.
        with source.profiling_session():
            profile = profile_table(source, database, schema, table)
            profile["anomalies"] = self._summarize_anomalies(profile)
            facts = self._derive_facts(profile, source, database, schema, table)

        logger.info(
            f"[ProfilingAgent] Done — {len(profile.get('columns', []))} columns, "
            f"{len(profile['anomalies'])} anomaly signals, "
            f"{len(facts['pk_shaped_candidates'])} pk-shaped, "
            f"{len(facts['freshness_signals'])} freshness, "
            f"{len(facts['closed_set_columns'])} closed-set"
        )
        # Return the UI profile fields AND the deterministic facts in one dict.
        # RuleIntelligenceAgent reads column_stats / pk_shaped_candidates /
        # freshness_signals / closed_set_columns straight off this.
        return {**profile, **facts}

    # ── Deterministic facts (derived in Python from the profile) ──────────────

    def _derive_facts(
        self,
        profile: Dict[str, Any],
        source: Any,
        database: str,
        schema: str,
        table: str,
    ) -> Dict[str, Any]:
        """
        Build the four deterministic-fact keys RuleIntelligenceAgent consumes,
        entirely from fields profile_table already computed per column — no new
        per-column queries and no dialect-specific SQL. The only optional extra
        read is closed-set distinct values via source.sample_values (portable),
        and only for low-cardinality columns.
        """
        row_count = (profile.get("table") or {}).get("row_count") or 0

        column_stats: Dict[str, dict] = {}
        pk_shaped_candidates: List[dict] = []
        freshness_signals: List[dict] = []
        closed_set_columns: Dict[str, dict] = {}

        for c in profile.get("columns", []):
            name = c.get("column_name")
            if not name:
                continue

            null_count = c.get("null_count") or 0
            distinct = c.get("distinct_count") or 0
            # total: prefer the table row_count; fall back to null_count+distinct
            # only if row_count is unavailable.
            total = row_count or (null_count + distinct)
            non_null_total = max(total - null_count, 0)
            null_pct = c.get("null_percentage")
            if null_pct is None:
                null_pct = round((null_count / total * 100), 1) if total else 0.0
            top_values = c.get("top_values") or []

            # Tail values (least-frequent) are where typos, legacy codes, and
            # data-entry one-offs hide — the exact material rules should
            # catch. Fetch only for low-cardinality columns so we don't do a
            # full scan on wide-domain text columns.
            tail_values: List[dict] = []
            if distinct and distinct <= _TAIL_VALUES_MAX_DISTINCT:
                try:
                    tail_values = source.bottom_values(
                        database, schema, table, name, limit=_TAIL_VALUES_LIMIT,
                    )
                except Exception as e:
                    logger.debug(f"[ProfilingAgent] bottom_values failed for {name}: {e}")
                    tail_values = []

            column_stats[name] = {
                "total": total,
                "nulls": null_count,
                "null_pct": null_pct,
                "distinct": distinct,
                "top_values": top_values,
                "tail_values": tail_values,
            }

            # PK-shaped uniqueness signal — only when the column NAME looks like a
            # key and live distinct/non-null counts reveal duplicates.
            if _PK_SHAPE_RE.search(str(name).upper()):
                is_unique = (distinct >= non_null_total) if non_null_total else True
                pk_shaped_candidates.append({
                    "column": name,
                    "total": total,
                    "non_null_total": non_null_total,
                    "distinct": distinct,
                    "is_unique": is_unique,
                    "duplicate_rows": max(non_null_total - distinct, 0),
                })

            # Freshness signal — temporal columns with a known newest value +
            # age. freshness_days is already computed by profile_table (portably).
            data_type = str(c.get("data_type") or "").upper()
            if data_type in _TEMPORAL_TYPES and c.get("max_value") is not None:
                freshness_signals.append({
                    "signal_id": f"freshness:{name}",
                    "column": name,
                    "data_type": data_type,
                    "max_value": str(c.get("max_value")),
                    "age_days": c.get("freshness_days"),
                })

            # Closed-set (enum-like) columns — low cardinality. Prefer distinct
            # values already in the profile; otherwise sample (portable).
            if distinct and distinct <= _CLOSED_SET_MAX_DISTINCT:
                values = self._closed_set_values(c, source, database, schema, table, name)
                if values is not None:
                    closed_set_columns[name] = {"values": values, "distinct_count": distinct}

        return {
            "column_stats": column_stats,
            "pk_shaped_candidates": pk_shaped_candidates,
            "freshness_signals": freshness_signals,
            "closed_set_columns": closed_set_columns,
        }

    @staticmethod
    def _closed_set_values(
        col: Dict[str, Any],
        source: Any,
        database: str,
        schema: str,
        table: str,
        name: str,
    ) -> Optional[List[Any]]:
        """Distinct value list for a low-cardinality column. Uses the profile's
        top_values when they already cover every distinct value; otherwise falls
        back to a portable non-null sample via the DataSource."""
        top_values = col.get("top_values") or []
        distinct = col.get("distinct_count") or 0
        # top_values already enumerates the whole domain when it's at least as
        # long as the distinct count — no extra query needed.
        if top_values and len(top_values) >= distinct:
            return [tv.get("value") for tv in top_values]
        try:
            sample = source.sample_values(database, schema, table, name, limit=_CLOSED_SET_SAMPLE_LIMIT)
            # De-dupe preserving order.
            seen, out = set(), []
            for v in sample:
                if v not in seen:
                    seen.add(v)
                    out.append(v)
            return out
        except Exception as e:
            logger.debug(f"[ProfilingAgent] closed-set sample failed for {name}: {e}")
            return None

    # ── UI anomaly narrative (unchanged) ──────────────────────────────────────

    @staticmethod
    def _summarize_anomalies(profile: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Distill statistical red flags a rule author would care about. These are
        *signals*, not findings — Rule Intelligence decides what to do with them.
        """
        anomalies: List[Dict[str, Any]] = []
        for c in profile.get("columns", []):
            name = c.get("column_name")
            null_pct = c.get("null_percentage")
            if null_pct is not None and null_pct >= 30:
                anomalies.append({
                    "column": name, "type": "high_null_rate",
                    "detail": f"{null_pct}% null",
                })
            if c.get("outlier_hint"):
                anomalies.append({
                    "column": name, "type": "numeric_outlier",
                    "detail": f"max {c.get('max_value')} is far from avg {c.get('avg_value')} (stddev {c.get('stddev')})",
                })
            if c.get("category") == "id" and (c.get("duplicate_count") or 0) > 0:
                anomalies.append({
                    "column": name, "type": "duplicate_key",
                    "detail": f"{c.get('duplicate_count')} values duplicated on an ID-like column",
                })
            fresh = c.get("freshness_days")
            if fresh is not None and fresh > 7:
                anomalies.append({
                    "column": name, "type": "stale_data",
                    "detail": f"newest value is {fresh} days old",
                })
            if c.get("category") in ("email", "phone"):
                pm = c.get("pattern_match_pct")
                if pm is not None and pm < 95:
                    anomalies.append({
                        "column": name, "type": "format_mismatch",
                        "detail": f"only {pm}% match expected {c.get('category')} format",
                    })
        return anomalies
