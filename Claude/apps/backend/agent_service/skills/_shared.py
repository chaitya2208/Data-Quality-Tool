"""Shared helpers for rule skills -- not one of the 5 requested skills, added
to avoid repeating the same rule-candidate shape / priority math / column-name
tokenizing in every skill file. FQN-building lives in
tools/rule_template_tools.py now, since that's the only place it's still
needed -- skills call rule_template_tools' *_sql() functions instead of
building SQL (and FQNs) themselves.

Every skill returns a list of dicts in this shape, matching
storage_tools.store_recommended_rule()'s parameters (minus scan_id, which the
caller/orchestrator supplies once it has a scan in progress):

{
  "rule_name": str,
  "rule_type": str,          # COMPLETENESS / UNIQUENESS / VALIDITY / FRESHNESS / VOLUME
  "database_name": str, "schema_name": str, "table_name": str,
  "column_name": str | None, # None for table-level rules (volume)
  "description": str,
  "reason": str,
  "evidence": list[str],
  "severity": "CRITICAL" | "WARNING" | "INFO",
  "confidence": float,       # 0-1: how sure the rule is logically correct
  "priority": float,         # 0-1: confidence * severity weight
  "threshold_config": dict | None,
  "generated_sql": str,      # template SQL, always returns a FAILED_COUNT column
}

Skills are pure functions over already-computed profile dicts (as produced
by snowflake_profiling_tools.py) -- no SQL is executed here.
"""

from __future__ import annotations

from typing import Any

_SEVERITY_WEIGHT = {"CRITICAL": 1.0, "WARNING": 0.6, "INFO": 0.3}


def compute_priority(confidence: float, severity: str) -> float:
    """priority = confidence * severity_weight -- the same formula
    build_candidate() below applies to every template-sourced rule.

    Extracted out to a standalone function (not just inline in
    build_candidate()) so agents/rule_recommendation_agent.py's hybrid
    template+Claude path can apply the identical formula to Claude-sourced
    rules too, rather than trusting whatever `priority` number Claude itself
    returns. Per mvp-scope.md's "numbers from code, words from the LLM"
    invariant, priority is a score this codebase computes, not something an
    LLM call is trusted to have gotten right on its own -- unknown severity
    values default to the WARNING weight rather than raising, since Claude's
    rule_type/severity vocabulary isn't hand-checked against this table the
    way each skill's own severity choices are.
    """
    weight = _SEVERITY_WEIGHT.get(severity, _SEVERITY_WEIGHT["WARNING"])
    return round(confidence * weight, 4)


def name_tokens(column_name: str) -> list[str]:
    """Split a column name into uppercase tokens on '_'.

    Used instead of substring matching -- "CANDIDATE" contains the substring
    "ID" (c-a-n-d-ID-ate), and "PAID"/"VALID" do too. Token matching avoids
    those false positives: CUSTOMER_ID -> ["CUSTOMER", "ID"] matches on the
    "ID" token; CANDIDATE -> ["CANDIDATE"], no match.
    """
    return column_name.upper().split("_")


def build_candidate(
    *,
    rule_name: str,
    rule_type: str,
    database_name: str,
    schema_name: str,
    table_name: str,
    column_name: str | None,
    description: str,
    reason: str,
    evidence: list[str],
    severity: str,
    confidence: float,
    threshold_config: dict[str, Any] | None,
    generated_sql: str,
) -> dict[str, Any]:
    priority = compute_priority(confidence, severity)
    return {
        "rule_name": rule_name,
        "rule_type": rule_type,
        "database_name": database_name,
        "schema_name": schema_name,
        "table_name": table_name,
        "column_name": column_name,
        "description": description,
        "reason": reason,
        "evidence": evidence,
        "severity": severity,
        "confidence": round(confidence, 4),
        "priority": priority,
        "threshold_config": threshold_config,
        "generated_sql": generated_sql,
    }
