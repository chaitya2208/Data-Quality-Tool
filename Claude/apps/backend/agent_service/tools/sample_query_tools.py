"""Sample failed rows -- shared builder for the one {rows, note, evidence}
shape stored in both RECOMMENDED_INSTANCES.TEST_RESULT.sample_failed_rows (test-
execution path, agents/rule_test_execution_agent.py) and
ALERTS.ALERT_VIOLATION_SAMPLES.SAMPLE_ROWS (real-execution path,
agents/rule_execution_agent.py + agents/alert_agent.py).

Factored out here so both agents dispatch identically rather than duplicating
the row-level-vs-table-level-vs-no-SQL fallback logic twice. Per the ask:
sample rows are attempted for every rule type, predefined and Claude-sourced
alike -- COMPLETENESS/UNIQUENESS/VALIDITY get real sampled rows (masked per
column policy); FRESHNESS/VOLUME (table-level aggregates, no per-row
predicate) and any rule with no generated_sql at all get a combination of a
descriptive note plus a stand-in evidence fact instead of a row list.

Shape (always this dict when there's anything to say at all; see
build_sample_failed_rows()'s own docstring for when it returns None instead):
    {
      "rows": list[dict] | None,   # masked row dicts, or None if not row-shaped
      "note": str | None,          # human-readable explanation when rows is None
      "evidence": dict | None,     # stand-in aggregate fact for FRESHNESS/VOLUME
    }
"""

from __future__ import annotations

from typing import Any

from tools.pii_detection_tools import mask_sample_rows
from tools.rule_template_tools import freshness_evidence_sql, render_sample_sql_for_rule
from tools.snowflake_connection import run_query

_SAMPLE_QUERY_TIMEOUT_SECONDS = 30

_NO_ROWS_NOTE = "No rows returned by the sample query."
_TABLE_LEVEL_NOTE_TEMPLATE = (
    "This is a table-level {kind} check -- no individual failing rows to sample."
)
_NO_SQL_NOTE = "No SQL available to sample -- this rule type has no template-generated check."


def build_sample_failed_rows(
    rule: dict[str, Any],
    failed_count: int | None,
    total_count: int | None,
    column_policies: dict[str, str | None],
) -> dict[str, Any] | None:
    """Build the sample_failed_rows dict for one rule's already-run check.

    failed_count/total_count come from the aggregate query the caller
    already ran (rule_test_execution_agent._run_one_test() /
    rule_execution_agent._execute()) -- this function never re-runs that
    query, only (conditionally) a second, row-returning or evidence query.

    Returns None when there's nothing worth attaching at all: a row-level
    rule type (COMPLETENESS/UNIQUENESS/VALIDITY) that passed (failed_count
    in (None, 0)) has no failing rows and no evidence fact worth
    manufacturing -- callers should leave sample_failed_rows unset/None for
    those rather than storing an empty shape. FRESHNESS/VOLUME/no-SQL rule
    types always return a dict (their table-level nature is itself the
    thing being communicated, regardless of pass/fail).
    """
    rule_type = rule.get("rule_type")

    if rule_type == "FRESHNESS":
        column_name = rule.get("column_name")
        try:
            evidence_rows = run_query(
                freshness_evidence_sql(
                    rule["database_name"], rule["schema_name"], rule["table_name"], column_name
                ),
                timeout=_SAMPLE_QUERY_TIMEOUT_SECONDS,
            )
            most_recent_value = evidence_rows[0].get("MOST_RECENT_VALUE") if evidence_rows else None
        except Exception:  # noqa: BLE001 -- evidence is best-effort, must not fail the real result
            most_recent_value = None
        return {
            "rows": None,
            "note": _TABLE_LEVEL_NOTE_TEMPLATE.format(kind="freshness"),
            "evidence": {"most_recent_value": most_recent_value, "column": column_name},
        }

    if rule_type == "VOLUME":
        threshold_config = rule.get("threshold_config") or {}
        return {
            "rows": None,
            "note": _TABLE_LEVEL_NOTE_TEMPLATE.format(kind="volume"),
            "evidence": {
                "current_row_count": total_count,
                "historical_avg_row_count": threshold_config.get("historical_avg_row_count"),
            },
        }

    sample_sql = render_sample_sql_for_rule(rule)
    if sample_sql is None:
        # No per-row predicate exists for this rule_type (e.g. a Claude-
        # sourced type the template dispatcher doesn't recognize) -- same
        # "recommended, not yet executable" treatment this codebase gives
        # those rules elsewhere (sql_generation_agent.py/sql_validation_agent.py).
        return {"rows": None, "note": _NO_SQL_NOTE, "evidence": None}

    if not failed_count:
        # Row-level type with nothing failing -- nothing to show, and no
        # reason to spend a query proving that.
        return None

    try:
        raw_rows = run_query(sample_sql, timeout=_SAMPLE_QUERY_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 -- sampling must not fail the real result it's attached to
        return {"rows": [], "note": _NO_ROWS_NOTE, "evidence": None}

    if not raw_rows:
        return {"rows": [], "note": _NO_ROWS_NOTE, "evidence": None}

    return {"rows": mask_sample_rows(raw_rows, column_policies), "note": None, "evidence": None}
