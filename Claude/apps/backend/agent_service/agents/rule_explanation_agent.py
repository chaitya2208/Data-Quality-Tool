"""Rule Explanation Agent -- "Add Better Claude Explanations" ask.

Golden rule (per the ask): Claude explains and recommends. Code validates
and executes. Human approves. This agent is purely additive to that
pipeline -- it runs after SQL generation/validation/test-execution have
already produced a rule's final generated_sql/validation_status/
test_status (all deterministic, all unchanged by this agent), and only
attaches three text fields for a human reviewer: business_explanation,
business_impact, false_positive_risk. It never touches rule_sql,
threshold_config, severity, or any status field.

If the Claude call fails (network, auth, Bedrock throttling -- same
failure modes rule_recommendation_agent.py already guards against), this
falls back to a deterministic templated sentence built from the rule's own
fields, rather than leaving the explanation blank or failing the scan.
This mirrors that agent's own "LLM failing must not fail the pipeline"
convention -- explanation is a nice-to-have on top of a rule that already
has real SQL and a real test result; it must never block approval.
"""

from __future__ import annotations

from typing import Any

from tools.claude_tools import explain_rule_with_claude


def _fallback_explanation(rule: dict[str, Any]) -> dict[str, str]:
    """A plain, deterministic sentence built from the rule's own fields --
    used only when the Claude call itself fails. Not a substitute for a
    real explanation; just enough that a human reviewer never sees a blank
    field because Bedrock was unreachable.
    """
    location = f"{rule.get('database_name')}.{rule.get('schema_name')}.{rule.get('table_name')}"
    if rule.get("column_name"):
        location += f".{rule['column_name']}"
    return {
        "business_explanation": (
            f"This is a {rule.get('rule_type', 'data quality')} check on {location}. "
            f"{rule.get('description') or ''}"
        ).strip(),
        "business_impact": (
            f"If this check keeps failing, data in {location} may be incomplete or "
            "incorrect, which can affect anything downstream that relies on it."
        ),
        "false_positive_risk": (
            "Not yet assessed by Claude -- review the rule's evidence and test "
            "result manually before treating a failure as confirmed."
        ),
    }


def run_rule_explanation_agent(rule: dict[str, Any]) -> dict[str, str]:
    """Return {business_explanation, business_impact, false_positive_risk}
    for one recommended rule. Never raises -- falls back to
    _fallback_explanation() on any Claude/Bedrock failure.
    """
    try:
        return explain_rule_with_claude(rule)
    except Exception as exc:  # noqa: BLE001 -- see module docstring
        print(f"[rule_explanation_agent] Claude call failed, using fallback text: {exc}")
        return _fallback_explanation(rule)
