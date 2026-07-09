"""Deterministic PII/sensitivity detection -- the first tier of
agents/pii_agent.py's two-tier classification, per architecture.md §7:
"Column -> PII detector (regex/heuristics for email, phone, PAN, Aadhaar,
names, addresses, financial ids) + LLM assist for ambiguous cases".

This module is regex/heuristics only -- no LLM call, no network I/O. It
looks at a column's name and (when available) its profiled top_values
sample -- data profiling already collected, no new query -- and returns a
confident classification, or None to signal "ambiguous, defer to Claude".

Column-name patterns are checked first (cheapest, most reliable signal in
practice: a column literally named EMAIL almost certainly holds emails).
Value-shape regexes are a secondary check against top_values for columns
whose name alone doesn't give it away but whose sample values do (e.g. a
column named FIELD_7 that's full of email-shaped strings).

Sensitivity/policy mapping follows architecture.md §7 exactly:
    LOW    -> ALLOW_RAW_SAMPLE
    MEDIUM -> ALLOW_MASKED_SAMPLE
    HIGH   -> ALLOW_STATS_ONLY
PII_TYPE values match 04_create_rule_tables.sql's DDL comment convention:
EMAIL / PHONE / PAN / AADHAAR / NAME / ADDRESS / FINANCIAL_ID.
"""

from __future__ import annotations

import re
from typing import Any

# Column-name patterns -> (pii_type, sensitivity_level). Checked as
# substring matches against the column name uppercased with underscores
# kept (Snowflake convention is UPPER_SNAKE_CASE column names) -- ordered
# so more specific patterns (PHONE before NAME, etc.) don't get shadowed by
# a looser one appearing earlier.
_NAME_PATTERNS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"E?MAIL"), "EMAIL", "HIGH"),
    (re.compile(r"PHONE|MOBILE|CELL_?NUM|TELEPHONE"), "PHONE", "HIGH"),
    (re.compile(r"\bPAN\b|PAN_?NUM|PAN_?CARD"), "PAN", "HIGH"),
    (re.compile(r"AADHAAR|AADHAR"), "AADHAAR", "HIGH"),
    (re.compile(r"SSN|SOCIAL_?SECURITY"), "FINANCIAL_ID", "HIGH"),
    (re.compile(r"CREDIT_?CARD|CARD_?NUM|ACCOUNT_?NUM|IBAN|ROUTING_?NUM"), "FINANCIAL_ID", "HIGH"),
    (re.compile(r"ADDRESS|STREET|POSTAL|ZIP_?CODE"), "ADDRESS", "MEDIUM"),
    (re.compile(r"FIRST_?NAME|LAST_?NAME|FULL_?NAME|CUSTOMER_?NAME|USER_?NAME|"
                 r"PATIENT_?NAME|EMPLOYEE_?NAME|CONTACT_?NAME"), "NAME", "MEDIUM"),
)

# Value-shape regexes, applied to top_values' sample strings -- a secondary
# check for columns whose name alone didn't match above. Only checked
# against string-shaped values; non-string top_values (numbers, dates)
# never match, since these detectors are text-pattern-based by design.
_VALUE_PATTERNS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$"), "EMAIL", "HIGH"),
    (re.compile(r"^\+?\d[\d\-\s()]{7,}\d$"), "PHONE", "HIGH"),
)

SENSITIVITY_TO_POLICY = {
    "LOW": "ALLOW_RAW_SAMPLE",
    "MEDIUM": "ALLOW_MASKED_SAMPLE",
    "HIGH": "ALLOW_STATS_ONLY",
}

# Column-name patterns confident enough to classify as definitely NOT PII --
# short-circuits obvious system/audit columns (ids, timestamps, counts) so
# they don't fall through to the ambiguous/LLM-assist tier for no reason.
_SAFE_NAME_PATTERNS = re.compile(
    r"^(ID|.*_ID|.*_AT|.*_TS|.*_TIME|.*_DATE|.*_COUNT|.*_STATUS|.*_TYPE|"
    r"SCAN_ID|RULE_ID|EXECUTION_ID|ALERT_ID|BATCH_ID|.*_FLAG|.*_PERCENTAGE|"
    r"CREATED_.*|UPDATED_.*|LOADED_.*|ROW_.*)$"
)


def _classification(pii_type: str, sensitivity_level: str, is_pii: bool = True) -> dict[str, Any]:
    return {
        "is_pii": is_pii,
        "pii_type": pii_type if is_pii else None,
        "sensitivity_level": sensitivity_level,
        "llm_sharing_policy": SENSITIVITY_TO_POLICY[sensitivity_level],
    }


_MASKED_PLACEHOLDER = "***MASKED***"


def mask_sample_rows(
    rows: list[dict[str, Any]], column_policies: dict[str, str | None]
) -> list[dict[str, Any]]:
    """Mask a list of raw Snowflake result rows (e.g. from a sample-failed-
    rows query) per each column's LLM_SHARING_POLICY, for display/storage --
    not for an LLM call (that's _mask_column_profile() in claude_tools.py,
    which masks column-level stats, not row-level values; this is the
    row-level counterpart the sample-failed-rows feature needs).

    rows: raw run_query() result dicts (uppercase column-name keys, matching
    Snowflake's own convention -- e.g. {"CUSTOMER_ID": 5, "EMAIL": "a@b.com"}).
    column_policies: {column_name: llm_sharing_policy}, keys expected in the
    same case as the row dict keys (both come from Snowflake identifiers, so
    this holds naturally without any case-normalization step).

    ALLOW_RAW_SAMPLE -> value passed through unchanged.
    ALLOW_MASKED_SAMPLE -> value replaced with a fixed placeholder.
    ALLOW_STATS_ONLY, or a column with no policy on record at all -> field
    dropped from the row entirely. Missing-policy defaults to dropped, not
    passed-through -- same "never silently downgrade to safe-to-share just
    because classification is missing" convention agents/pii_agent.py
    already applies to its own Claude-failure fallback.
    """
    masked_rows = []
    for row in rows:
        masked_row = {}
        for column_name, value in row.items():
            policy = column_policies.get(column_name)
            if policy == "ALLOW_RAW_SAMPLE":
                masked_row[column_name] = value
            elif policy == "ALLOW_MASKED_SAMPLE":
                masked_row[column_name] = _MASKED_PLACEHOLDER
            # ALLOW_STATS_ONLY or unknown/missing policy: field dropped.
        masked_rows.append(masked_row)
    return masked_rows


def classify_column_deterministic(
    column_name: str, top_values: list[dict[str, Any]] | None = None
) -> dict[str, Any] | None:
    """Classify one column using regex/heuristics only.

    Returns a classification dict ({is_pii, pii_type, sensitivity_level,
    llm_sharing_policy}) when confident (either "this is PII" or "this is
    clearly not PII"), or None when genuinely ambiguous -- callers
    (agents/pii_agent.py) route None results to the LLM-assist tier.
    """
    name = column_name.upper()

    for pattern, pii_type, sensitivity in _NAME_PATTERNS:
        if pattern.search(name):
            return _classification(pii_type, sensitivity)

    if _SAFE_NAME_PATTERNS.match(name):
        return _classification(pii_type="", sensitivity_level="LOW", is_pii=False)

    if top_values:
        sample_strings = [
            str(tv["value"]) for tv in top_values if isinstance(tv.get("value"), str)
        ]
        for value in sample_strings:
            for pattern, pii_type, sensitivity in _VALUE_PATTERNS:
                if pattern.match(value):
                    return _classification(pii_type, sensitivity)

    return None
