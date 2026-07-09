"""Alert Explanation Agent -- "Add Better Claude Explanations" ask.

Same golden rule as rule_explanation_agent.py: Claude explains, code
validates/executes, human approves. By the time this runs, the alert's
title/description/severity/failed_count/status are already fully decided
by deterministic code (agents/alert_agent.py, storage_tools.store_alert())
-- this agent only attaches business_explanation/business_impact/
false_positive_risk on top, never changes what the alert says happened or
its severity/status.

Same Claude-failure fallback convention as rule_explanation_agent.py: an
alert must never fail to be created just because Bedrock is unreachable,
so any explain_alert_with_claude() failure falls back to a deterministic
templated sentence instead of blocking alert creation.
"""

from __future__ import annotations

from typing import Any

from tools.claude_tools import explain_alert_with_claude


def _fallback_explanation(
    rule: dict[str, Any], execution_result: dict[str, Any]
) -> dict[str, str]:
    location = f"{rule.get('database_name')}.{rule.get('schema_name')}.{rule.get('table_name')}"
    if rule.get("column_name"):
        location += f".{rule['column_name']}"
    failed = execution_result.get("failed_count")
    total = execution_result.get("total_count")
    pct = execution_result.get("failure_percentage")
    return {
        "business_explanation": (
            f"The {rule.get('rule_type', 'data quality')} rule on {location} failed: "
            f"{failed} of {total} rows ({pct}%) violated the check."
        ),
        "business_impact": (
            f"Anything downstream that relies on {location} may be working with "
            "incomplete or incorrect data until this is resolved."
        ),
        "false_positive_risk": (
            "Not yet assessed by Claude -- review the failure count and percentage "
            "manually before treating this as a confirmed data issue."
        ),
    }


def run_alert_explanation_agent(
    rule: dict[str, Any], execution_result: dict[str, Any]
) -> dict[str, str]:
    """Return {business_explanation, business_impact, false_positive_risk}
    for one alert-triggering execution result. Never raises -- falls back
    to _fallback_explanation() on any Claude/Bedrock failure.
    """
    try:
        return explain_alert_with_claude(rule, execution_result)
    except Exception as exc:  # noqa: BLE001 -- see module docstring
        print(f"[alert_explanation_agent] Claude call failed, using fallback text: {exc}")
        return _fallback_explanation(rule, execution_result)
