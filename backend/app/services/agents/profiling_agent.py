"""
Profiling Agent — runs in parallel with Metadata and Rules Fetch.

Computes per-column data statistics (null %, distinct, min/max/avg/stddev,
top values, category, outlier hints) via profiling_service, and distills a
compact anomaly summary. The result is handed to the Rule Intelligence agent
so Claude generates rules grounded in the actual data — catching issues static
rules can't (e.g. an AGE column whose max is 999, or a supposedly-unique key
with duplicates).

Runs its own DB session when invoked from a coordinator thread. Pure read
against Snowflake — no writes.
"""
import logging
from typing import Any, Dict, List
from sqlalchemy.orm import Session

from app.services.profiling_service import profile_table

logger = logging.getLogger(__name__)


class ProfilingAgent:
    def __init__(self, db: Session):
        self.db = db  # not used for reads, kept for interface symmetry with other agents

    def run(self, database: str, schema: str, table: str) -> Dict[str, Any]:
        logger.info(f"[ProfilingAgent] Profiling {database}.{schema}.{table}")
        profile = profile_table(database, schema, table)
        profile["anomalies"] = self._summarize_anomalies(profile)
        logger.info(
            f"[ProfilingAgent] Done — {len(profile.get('columns', []))} columns, "
            f"{len(profile['anomalies'])} anomaly signals"
        )
        return profile

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
