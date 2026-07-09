"""Volume Skill -- suggests row count rules.

Logic (per spec): once enough history exists, compare row count to
historical average. Below that, a simple row count > 0 rule.

Historical-average comparison (previously deferred -- see
deferred-and-future-work.md #6/#26): once >= _MIN_HISTORY_FOR_AVERAGE prior
PROFILING.TABLE_PROFILES rows exist for this table (fetched by the caller,
agents/rule_recommendation_agent.py, via
storage_tools.list_table_profile_history() -- this skill stays a pure
function, no I/O, per skills/_shared.py's convention), propose a
historical-average check instead of the static ">0" rule: WARNING once the
current row count deviates beyond _WARNING_BAND_PCT of the average, with the
rule's initial severity set to CRITICAL if it's already past
_CRITICAL_BAND_PCT at proposal time. Replaces (not adds to) the static rule
once there's enough history -- the historical-average check is a strict
superset of ">0" (an empty table after a positive average is itself a huge
deviation), so proposing both would just be two approval decisions about the
same underlying concern.

Below the history threshold, behavior is unchanged from before this feature.
"""

from __future__ import annotations

from typing import Any

from skills._shared import build_candidate
from tools.rule_template_tools import volume_historical_sql, volume_sql

_MIN_HISTORY_FOR_AVERAGE = 3
_WARNING_BAND_PCT = 30
_CRITICAL_BAND_PCT = 50


def suggest_volume_rules(
    database_name: str,
    schema_name: str,
    table_name: str,
    row_count: int,
    row_count_history: list[int] | None = None,
) -> list[dict[str, Any]]:
    """One table-level candidate: either the static ">0" rule (fewer than
    _MIN_HISTORY_FOR_AVERAGE prior scans) or a historical-average deviation
    check (enough history exists).

    column_name is None -- this is a table-level rule, not a per-column one.
    row_count_history: prior scans' row counts, most-recent-first or in any
    order (only used via sum()/len(), order doesn't matter) -- does not
    include the current scan's own row_count.
    """
    history = row_count_history or []

    if len(history) < _MIN_HISTORY_FOR_AVERAGE:
        return [
            build_candidate(
                rule_name=f"{table_name} should have at least one row",
                rule_type="VOLUME",
                database_name=database_name,
                schema_name=schema_name,
                table_name=table_name,
                column_name=None,
                description=f"{table_name} should not be empty.",
                reason="MVP volume check: table currently has rows; row count should stay above 0.",
                evidence=[f"Current row count: {row_count}"],
                severity="CRITICAL" if row_count > 0 else "INFO",
                confidence=0.9,
                threshold_config={"min_row_count": 0, "exclusive": True},
                generated_sql=volume_sql(database_name, schema_name, table_name),
            )
        ]

    avg = sum(history) / len(history)
    if avg == 0:
        deviation_pct = float("inf") if row_count > 0 else 0.0
    else:
        deviation_pct = abs(row_count - avg) / avg * 100

    severity = "CRITICAL" if deviation_pct >= _CRITICAL_BAND_PCT else "WARNING"
    threshold_config = {
        "historical_avg_row_count": round(avg, 2),
        "warning_band_pct": _WARNING_BAND_PCT,
        "critical_band_pct": _CRITICAL_BAND_PCT,
        "profiles_considered": len(history),
        "current_row_count": row_count,
    }
    return [
        build_candidate(
            rule_name=f"{table_name} row count should match its historical average",
            rule_type="VOLUME",
            database_name=database_name,
            schema_name=schema_name,
            table_name=table_name,
            column_name=None,
            description=(
                f"{table_name}'s row count should stay within "
                f"{_WARNING_BAND_PCT}% of its historical average."
            ),
            reason=(
                "Historical-average volume check: enough scan history exists "
                f"({len(history)} prior scans) to detect an unusual row-count swing "
                "instead of only checking for an empty table."
            ),
            evidence=[
                f"Historical average over last {len(history)} scans: {avg:.0f} rows; "
                f"current: {row_count} ({deviation_pct:.1f}% deviation)"
                if deviation_pct != float("inf")
                else f"Historical average was 0 over last {len(history)} scans; current: {row_count}"
            ],
            severity=severity,
            confidence=0.9,
            threshold_config=threshold_config,
            generated_sql=volume_historical_sql(
                database_name, schema_name, table_name, avg, _WARNING_BAND_PCT
            ),
        )
    ]
