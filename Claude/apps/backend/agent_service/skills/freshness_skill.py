"""Freshness Skill -- suggests freshness rules.

Logic (per spec): if table has a date/timestamp column like CREATED_AT,
UPDATED_AT, LOAD_DATE, suggest a freshness check.

Deviations, see module-level NOTE comments below for why.
"""

from __future__ import annotations

from typing import Any

from skills._shared import build_candidate, name_tokens
from tools.rule_template_tools import freshness_sql

_FRESHNESS_TOKENS = {"CREATED", "UPDATED", "LOAD", "MODIFIED", "REFRESHED"}
_DATE_TIME_TYPE_PREFIXES = ("DATE", "TIME", "TIMESTAMP")

# "Freshness" needs a concrete threshold to be an executable rule, not
# specified in the source spec. 24 hours chosen as a generic MVP default --
# reasonable for most operational tables, but this should become
# user-configurable per table once the approval UI supports editing
# threshold_config (already the case: THRESHOLD_CONFIG is user-editable).
_DEFAULT_MAX_AGE_HOURS = 24


def suggest_freshness_rules(
    database_name: str,
    schema_name: str,
    table_name: str,
    column_profiles: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """One candidate per column that both names itself as a freshness marker
    AND is an actual date/time type -- name-only matching risks proposing a
    freshness check against a text column that happens to be named
    LOAD_DATE_LABEL or similar.
    """
    candidates = []

    for col in column_profiles:
        column_name = col["column_name"]
        data_type = col["data_type"].split("(")[0].upper()

        tokens = set(name_tokens(column_name))
        if not tokens & _FRESHNESS_TOKENS:
            continue
        if not data_type.startswith(_DATE_TIME_TYPE_PREFIXES):
            continue

        candidates.append(
            build_candidate(
                rule_name=f"{table_name} should be updated within {_DEFAULT_MAX_AGE_HOURS}h",
                rule_type="FRESHNESS",
                database_name=database_name,
                schema_name=schema_name,
                table_name=table_name,
                column_name=column_name,
                description=(
                    f"{column_name} should reflect activity within the last "
                    f"{_DEFAULT_MAX_AGE_HOURS} hours."
                ),
                reason=(
                    f"Column name matches a freshness marker pattern and is "
                    f"a {data_type} type."
                ),
                evidence=[
                    f"Column name token matches freshness pattern",
                    f"Data type: {col['data_type']}",
                ],
                severity="WARNING",
                confidence=0.7,
                threshold_config={"max_age_hours": _DEFAULT_MAX_AGE_HOURS},
                generated_sql=freshness_sql(
                    database_name,
                    schema_name,
                    table_name,
                    column_name,
                    _DEFAULT_MAX_AGE_HOURS,
                ),
            )
        )

    return candidates
