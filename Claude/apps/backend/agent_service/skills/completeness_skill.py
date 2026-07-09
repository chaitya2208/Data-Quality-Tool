"""Completeness Skill -- suggests NOT NULL rules.

Logic (per spec): if column name contains ID/DATE/AMOUNT/STATUS/CODE and null
percentage is low, recommend a completeness rule.

Deviations, see module-level NOTE comments below for why.
"""

from __future__ import annotations

from typing import Any

from skills._shared import build_candidate, name_tokens
from tools.rule_template_tools import completeness_sql

# "Contains ID/DATE/..." read literally as substring match would misfire on
# names like CANDIDATE (contains "ID"), PAID/VALID (contain "ID"). Matched as
# whole '_'-separated tokens instead -- see _shared.name_tokens().
_COMPLETENESS_TOKENS = {"ID", "DATE", "AMOUNT", "STATUS", "CODE"}

# "Null percentage is low" was left undefined in the spec. 5% chosen as the
# cutoff: a column already mostly non-null is a reasonable completeness
# candidate; above this, the column may be null by design and proposing a
# NOT NULL rule would likely just be rejected on review.
_LOW_NULL_PCT_THRESHOLD = 5.0

# ID-token columns are usually business/primary keys -- missing values there
# is more severe than a missing DATE/AMOUNT/STATUS/CODE, which is common
# WARNING-level DQ territory.
_SEVERITY_BY_TOKEN = {
    "ID": "CRITICAL",
    "DATE": "WARNING",
    "AMOUNT": "WARNING",
    "STATUS": "WARNING",
    "CODE": "WARNING",
}


def suggest_completeness_rules(
    database_name: str,
    schema_name: str,
    table_name: str,
    column_profiles: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """One candidate per column whose name matches a completeness-worthy
    token and whose null percentage is already low.
    """
    candidates = []

    for col in column_profiles:
        column_name = col["column_name"]
        null_pct = col["null_percentage"]

        tokens = set(name_tokens(column_name))
        matched_tokens = tokens & _COMPLETENESS_TOKENS
        if not matched_tokens or null_pct > _LOW_NULL_PCT_THRESHOLD:
            continue

        # Prefer the most severe matched token if more than one applies
        # (e.g. STATUS_CODE matches both STATUS and CODE).
        severity = "CRITICAL" if "ID" in matched_tokens else "WARNING"

        confidence = 0.95 if null_pct == 0 else 0.8

        candidates.append(
            build_candidate(
                rule_name=f"{column_name} should not be null",
                rule_type="COMPLETENESS",
                database_name=database_name,
                schema_name=schema_name,
                table_name=table_name,
                column_name=column_name,
                description=f"{column_name} is expected to always be populated.",
                reason=(
                    f"Column name matches completeness-relevant pattern "
                    f"({', '.join(sorted(matched_tokens))}) and current null "
                    f"percentage ({null_pct}%) is already low."
                ),
                evidence=[
                    f"Column name token(s): {', '.join(sorted(matched_tokens))}",
                    f"Current null percentage: {null_pct}%",
                ],
                severity=severity,
                confidence=confidence,
                threshold_config={"max_null_percentage": 0.0},
                generated_sql=completeness_sql(
                    database_name, schema_name, table_name, column_name
                ),
            )
        )

    return candidates
