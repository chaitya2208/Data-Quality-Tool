"""Alert Agent -- turns a FAILED rule execution into an ALERTS row.

Previously this logic lived inlined inside agents/rule_execution_agent.py
(a direct storage_tools.store_alert() call), flagged there as a stand-in
for a real Alert Agent module since none existed yet. This module is that
promised follow-up -- rule_execution_agent.py now calls run_alert_agent()
instead of storage_tools.store_alert() directly.

Logic (per the ask):
    execution status == FAILED -> create one ALERTS row
    execution status == PASSED / ERROR / SKIPPED -> no alert

PASSED means the rule ran and the data was clean -- nothing to alert on.
ERROR/SKIPPED mean the rule never actually told us whether the data is
good or bad (the SQL broke, or was never attempted) -- alerting on those
would raise a false "your data failed a check" signal for a check that
never really ran. Only FAILED means "we ran the check and the data
violated it," which is the one case architecture.md §4b's alerting step
is for.

Alert fields (per the ask): title, description, severity, failed_count,
failure_percentage, status="OPEN", created_at. Status and created_at are
set by storage_tools.store_alert() itself (STATUS hardcoded 'OPEN' on
insert; CREATED_AT/UPDATED_AT are TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
on ALERTS.ALERTS, see infra/snowflake/05_create_alert_tables.sql) -- this
agent doesn't set them itself, just supplies the fields storage doesn't
already default.

Business-friendly explanation now exists ("Add Better Claude Explanations"
ask, architecture.md §5's "Alert Creation... Turn failures into explained,
grouped alerts" -- the "explained" half): agents/alert_explanation_agent.py
calls Claude for business_explanation/business_impact/false_positive_risk
on every FAILED alert, purely additive text alongside the deterministic
title/description/severity/failed_count below.

Still not built here (real future work, not silently expanded into this
task): grouping/deduplicating alerts across repeated failures of the same
rule (the "grouped" half). ALERT_VIOLATION_SAMPLES (previously deferred,
now built): rule_execution_agent.py computes sample_failed_rows and passes
it in here; this agent stores it via storage_tools.store_violation_samples()
(previously dead code -- this is its first real caller) right after the
alert itself is created, since alert_id doesn't exist until store_alert()
returns.
"""

from __future__ import annotations

from typing import Any

from agents.alert_explanation_agent import run_alert_explanation_agent
from tools.storage_tools import store_alert, store_violation_samples


def run_alert_agent(
    rule: dict[str, Any],
    execution_id: str,
    execution_result: dict[str, Any],
    sample_failed_rows: dict[str, Any] | None = None,
) -> str | None:
    """Create an alert if the execution failed. Returns the new alert_id,
    or None if execution_result['status'] != 'FAILED' (no alert created).

    rule is the dict shape storage_tools.get_approved_rule() returns
    (rule_name, rule_type, database_name, schema_name, table_name,
    column_name, severity, ...). execution_result is the dict shape
    rule_execution_agent._execute() produces (status, failed_count,
    total_count, failure_percentage, error_message). sample_failed_rows is
    the {rows, note, evidence} dict from
    tools/sample_query_tools.build_sample_failed_rows(), or None -- optional/
    defaults to None so this function's older call shape still works.
    """
    if execution_result["status"] != "FAILED":
        return None

    # Business-friendly explanation (Claude, via
    # agents/alert_explanation_agent.py) -- purely additive text on top of
    # an alert whose title/description/severity/failed_count are already
    # fully decided below; never blocks alert creation on failure (that
    # agent falls back to a templated sentence internally).
    explanation = run_alert_explanation_agent(rule, execution_result)

    alert_id = store_alert(
        rule_id=rule["rule_id"],
        execution_id=execution_id,
        instance_id=rule["rule_id"],
        title=f"{rule['rule_name']} failed",
        description=(
            f"{rule['rule_type']} rule on "
            f"{rule['database_name']}.{rule['schema_name']}.{rule['table_name']}"
            + (f".{rule['column_name']}" if rule["column_name"] else "")
            + f" failed: {execution_result['failed_count']} of "
            f"{execution_result['total_count']} rows."
        ),
        severity=rule["severity"],
        failed_count=execution_result["failed_count"],
        failure_percentage=execution_result["failure_percentage"],
        business_explanation=explanation.get("business_explanation"),
        business_impact=explanation.get("business_impact"),
        false_positive_risk=explanation.get("false_positive_risk"),
    )

    if sample_failed_rows is not None:
        try:
            store_violation_samples(alert_id, sample_failed_rows)
        except Exception:  # noqa: BLE001 -- sampling must not block a real alert from existing
            pass

    return alert_id
