"""Uniqueness Skill -- suggests UNIQUE rules.

Logic (per spec): if column name ends with ID, distinct count is close to
row count, and null percentage is low, recommend a uniqueness rule.

Deviations, see module-level NOTE comments below for why.
"""

from __future__ import annotations

from typing import Any

from skills._shared import build_candidate, name_tokens
from tools.rule_template_tools import uniqueness_sql

# "Distinct close to row count" left undefined in the spec. distinct_count is
# compared against NON-NULL row count, not raw row count -- nulls naturally
# reduce distinct count and would unfairly penalize an otherwise-unique
# column. 98% chosen as "close": allows a small number of legitimate
# duplicates (e.g. retries, corrections) without losing the signal.
_UNIQUENESS_RATIO_THRESHOLD = 0.98

# Same "low null percentage" cutoff as completeness_skill, for consistency.
_LOW_NULL_PCT_THRESHOLD = 5.0


def suggest_uniqueness_rules(
    database_name: str,
    schema_name: str,
    table_name: str,
    row_count: int,
    column_profiles: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """One candidate per column ending in the ID token whose distinct count
    (among non-null values) is close to the non-null row count.
    """
    candidates = []

    for col in column_profiles:
        column_name = col["column_name"]
        null_pct = col["null_percentage"]
        null_count = col["null_count"]
        distinct_count = col["distinct_count"]

        tokens = name_tokens(column_name)
        if not tokens or tokens[-1] != "ID":
            continue
        if null_pct > _LOW_NULL_PCT_THRESHOLD:
            continue

        non_null_count = row_count - null_count
        if non_null_count <= 0:
            continue

        ratio = distinct_count / non_null_count
        if ratio < _UNIQUENESS_RATIO_THRESHOLD:
            continue

        confidence = 0.95 if ratio >= 0.999 else 0.75

        candidates.append(
            build_candidate(
                rule_name=f"{column_name} should be unique",
                rule_type="UNIQUENESS",
                database_name=database_name,
                schema_name=schema_name,
                table_name=table_name,
                column_name=column_name,
                description=f"{column_name} appears to be a unique identifier.",
                reason=(
                    f"Column name ends with ID; distinct count is "
                    f"{ratio:.1%} of non-null rows and null percentage "
                    f"({null_pct}%) is low."
                ),
                evidence=[
                    "Column name ends with ID",
                    f"Distinct count / non-null row count = {ratio:.4f}",
                    f"Null percentage: {null_pct}%",
                ],
                severity="CRITICAL",
                confidence=confidence,
                threshold_config={"min_distinct_ratio": _UNIQUENESS_RATIO_THRESHOLD},
                generated_sql=uniqueness_sql(
                    database_name, schema_name, table_name, column_name
                ),
            )
        )

    return candidates
