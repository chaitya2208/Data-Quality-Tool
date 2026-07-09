"""Validity Skill -- suggests range/format rules.

Logic (per spec):
- EMAIL-like column -> email format rule.
- AMOUNT/PRICE column -> value > 0 rule.
- STATUS column with few distinct values -> accepted-values rule.

Deviations, see module-level NOTE comments below for why.
"""

from __future__ import annotations

from typing import Any

from skills._shared import build_candidate, name_tokens
from tools.rule_template_tools import accepted_values_sql, email_format_sql, positive_amount_sql

_NUMERIC_TYPE_PREFIXES = ("NUMBER", "DECIMAL", "INT", "FLOAT", "DOUBLE")

# "STATUS has few values" left undefined in the spec. 10 chosen as the cutoff
# for "few": low enough that proposing an IN (...) accepted-values list is
# still readable/reviewable by a human; above this it's likely a
# free-text/high-cardinality column, not a fixed status set.
_FEW_VALUES_THRESHOLD = 10


def suggest_validity_rules(
    database_name: str,
    schema_name: str,
    table_name: str,
    row_count: int,
    column_profiles: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    candidates = []

    for col in column_profiles:
        column_name = col["column_name"]
        data_type = col["data_type"].split("(")[0].upper()
        tokens = set(name_tokens(column_name))

        # -- EMAIL-like column: format check --------------------------------
        if "EMAIL" in tokens:
            candidates.append(
                build_candidate(
                    rule_name=f"{column_name} should be a valid email format",
                    rule_type="VALIDITY",
                    database_name=database_name,
                    schema_name=schema_name,
                    table_name=table_name,
                    column_name=column_name,
                    description=f"{column_name} should contain a valid email address.",
                    reason="Column name indicates it stores email addresses.",
                    evidence=["Column name contains EMAIL token"],
                    severity="WARNING",
                    confidence=0.85,
                    threshold_config={"pattern": r"^[^@\s]+@[^@\s]+\.[^@\s]+$"},
                    generated_sql=email_format_sql(
                        database_name, schema_name, table_name, column_name
                    ),
                )
            )
            continue  # EMAIL doesn't also match AMOUNT/PRICE or STATUS

        # -- AMOUNT/PRICE column: must be numeric type to make sense --------
        # Spec matches on name alone; a text column named PRICE_TIER
        # ("premium"/"standard") would break "value > 0" SQL. Gated on the
        # column's actual data type, not just its name.
        if tokens & {"AMOUNT", "PRICE"} and data_type.startswith(_NUMERIC_TYPE_PREFIXES):
            candidates.append(
                build_candidate(
                    rule_name=f"{column_name} should be greater than 0",
                    rule_type="VALIDITY",
                    database_name=database_name,
                    schema_name=schema_name,
                    table_name=table_name,
                    column_name=column_name,
                    description=f"{column_name} should be a positive value.",
                    reason="Column name indicates a monetary amount and is numeric.",
                    evidence=[
                        "Column name contains AMOUNT/PRICE token",
                        f"Data type: {col['data_type']}",
                    ],
                    severity="WARNING",
                    confidence=0.8,
                    threshold_config={"min_value": 0, "exclusive": True},
                    generated_sql=positive_amount_sql(
                        database_name, schema_name, table_name, column_name
                    ),
                )
            )
            continue

        # -- STATUS column with few distinct values: accepted-values --------
        if "STATUS" in tokens and 0 < col["distinct_count"] <= _FEW_VALUES_THRESHOLD:
            accepted_values = [
                tv["value"] for tv in col.get("top_values", []) if tv["value"] is not None
            ]
            if not accepted_values:
                continue
            candidates.append(
                build_candidate(
                    rule_name=f"{column_name} should be one of the observed values",
                    rule_type="VALIDITY",
                    database_name=database_name,
                    schema_name=schema_name,
                    table_name=table_name,
                    column_name=column_name,
                    description=f"{column_name} should only contain a known set of status values.",
                    reason=(
                        f"Column name contains STATUS token and has only "
                        f"{col['distinct_count']} distinct value(s)."
                    ),
                    evidence=[
                        "Column name contains STATUS token",
                        f"Distinct count: {col['distinct_count']}",
                        f"Observed values: {accepted_values}",
                    ],
                    severity="WARNING",
                    confidence=0.7,
                    threshold_config={"accepted_values": accepted_values},
                    generated_sql=accepted_values_sql(
                        database_name, schema_name, table_name, column_name, accepted_values
                    ),
                )
            )

    return candidates
