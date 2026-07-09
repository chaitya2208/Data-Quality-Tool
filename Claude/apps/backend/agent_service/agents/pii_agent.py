"""PII / Sensitivity Classification Agent -- architecture.md §7's masking
floor's missing piece. `tools/claude_tools.py`'s `_mask_column_profile()`
has always existed and correctly strips PII values before the Claude call,
but had no real classifier upstream of it -- every column's `is_pii` was
permanently `False`. This agent is that classifier.

Two tiers, per architecture.md §7 ("Column -> PII detector (regex/
heuristics) + LLM assist for ambiguous cases"):
1. Deterministic: tools/pii_detection_tools.classify_column_deterministic()
   -- column-name patterns first, then a value-shape check against
   top_values. No LLM call, no network I/O.
2. LLM-assist: for columns tier 1 leaves ambiguous (returns None), one
   batched Claude/Bedrock call per table (not per column) via
   tools/claude_tools.classify_columns_with_claude().

Pure compute, same convention as profiling_agent.py / metadata_agent.py:
this agent enriches and returns the column_profiles list, storage is the
caller's job (via snowflake_profiling_tools.store_profile_result(), which
now reads is_pii/pii_type/sensitivity_level/llm_sharing_policy off each
column dict).

If the Claude call fails (network/auth/throttling), every still-ambiguous
column falls back to the safest classification (HIGH sensitivity /
ALLOW_STATS_ONLY) rather than defaulting to "not PII" -- an LLM failure
must not fail the scan (same convention as
agents/rule_recommendation_agent.py's Claude-call try/except), but it also
must never silently downgrade an unclassified column to "safe to share
raw" just because the classifier itself broke.
"""

from __future__ import annotations

from typing import Any

from tools.claude_tools import classify_columns_with_claude
from tools.pii_detection_tools import SENSITIVITY_TO_POLICY, classify_column_deterministic


def _fallback_classification() -> dict[str, Any]:
    return {
        "is_pii": True,
        "pii_type": None,
        "sensitivity_level": "HIGH",
        "llm_sharing_policy": "ALLOW_STATS_ONLY",
    }


def run_pii_agent(
    database_name: str,
    schema_name: str,
    table_name: str,
    column_profiles: list[dict[str, Any]],
) -> dict[str, Any]:
    """Classify every column's PII/sensitivity. Read-only, no storage.

    Output: {"column_profiles": [...]} -- same list profiling_agent.py
    produced, each column dict now also carrying is_pii/pii_type/
    sensitivity_level/llm_sharing_policy.
    """
    enriched = [dict(col) for col in column_profiles]
    ambiguous: list[dict[str, Any]] = []

    for col in enriched:
        classification = classify_column_deterministic(
            col["column_name"], col.get("top_values")
        )
        if classification is not None:
            col.update(classification)
        else:
            ambiguous.append(col)

    if ambiguous:
        table_fqn = f"{database_name}.{schema_name}.{table_name}"
        try:
            results = classify_columns_with_claude(table_fqn, ambiguous)
            by_name = {r["column_name"]: r for r in results}
        except Exception:  # noqa: BLE001 -- must not fail the scan
            by_name = {}

        for col in ambiguous:
            result = by_name.get(col["column_name"])
            if result is None:
                col.update(_fallback_classification())
                continue
            sensitivity_level = result.get("sensitivity_level", "HIGH")
            col.update(
                {
                    "is_pii": result.get("is_pii", True),
                    "pii_type": result.get("pii_type"),
                    "sensitivity_level": sensitivity_level,
                    "llm_sharing_policy": SENSITIVITY_TO_POLICY.get(
                        sensitivity_level, "ALLOW_STATS_ONLY"
                    ),
                }
            )

    return {"column_profiles": enriched}
