"""
Dynamic Rule Engine — Phase 1

Pattern-based data quality checks derived from column metadata.
No LLM required. Each check auto-registers its Rule row the first
time it fires, so new rules appear in the Rules page automatically.

Checks implemented:
  TABLE-LEVEL
    1. NO_PRIMARY_KEY_HINT         — no ID/PK column found
    2. MISSING_CREATED_AT          — no created timestamp column
    3. MISSING_UPDATED_AT          — no updated timestamp column
    4. TOO_MANY_COLUMNS            — column count > threshold
    5. INCONSISTENT_NAMING         — mixed case styles in column names

  COLUMN-LEVEL
    6. PII_COLUMN_NO_MASKING       — name suggests PII (email, ssn, phone…)
    7. GENERIC_COLUMN_NAME         — uninformative name (col1, data, value…)
    8. COLUMN_TYPE_MISMATCH        — name implies type X but actual type is Y
    9. FK_COLUMN_NO_CONSTRAINT     — ends in _ID but is not the table's own PK
   10. NULLABLE_ID_COLUMN          — a PK/ID column allows NULLs
   11. BOOLEAN_STORED_AS_VARCHAR   — flag/bool column stored as VARCHAR
   12. DATE_STORED_AS_VARCHAR      — date/timestamp column stored as VARCHAR
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from app.services import storage
from app.services.snowflake_session import session as sf_session

import logging

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Constants / pattern libraries
# ─────────────────────────────────────────────────────────────────────────────

MAX_COLUMNS = 50  # alert if a table has more columns than this

# Snowflake type groups (upper-cased, strip precision like NUMBER(38,0) → NUMBER)
NUMERIC_TYPES = {"NUMBER", "INTEGER", "INT", "BIGINT", "SMALLINT",
                 "TINYINT", "BYTEINT", "DECIMAL", "NUMERIC", "FLOAT",
                 "FLOAT4", "FLOAT8", "DOUBLE", "DOUBLE PRECISION", "FIXED"}
DATE_TYPES = {"DATE"}
TIMESTAMP_TYPES = {"TIMESTAMP_NTZ", "TIMESTAMP_LTZ", "TIMESTAMP_TZ",
                   "TIMESTAMP", "DATETIME"}
TEMPORAL_TYPES = DATE_TYPES | TIMESTAMP_TYPES
BOOLEAN_TYPES = {"BOOLEAN", "BOOL", "BIT"}
VARCHAR_TYPES = {"VARCHAR", "TEXT", "STRING", "CHAR", "CHARACTER",
                 "NCHAR", "NVARCHAR", "NVARCHAR2", "CHAR VARYING",
                 "CHARACTER VARYING"}

# Name → expected type group patterns: (regex, expected_types, rule_code_suffix, human_label)
NAME_TYPE_RULES: List[tuple] = [
    (re.compile(r"(_ID|_KEY|_FK|_PK|_SEQ|_NUM|_NO)$", re.I),
     NUMERIC_TYPES,
     "ID_WRONG_TYPE",
     "ID/key column should be numeric"),

    (re.compile(r"(_DATE|_DT|_DAY|_MONTH|_YEAR|_PERIOD)$", re.I),
     TEMPORAL_TYPES,
     "DATE_WRONG_TYPE",
     "Date column should be DATE or TIMESTAMP"),

    (re.compile(r"(_TS|_TIMESTAMP|_TIME|_AT|_ON)$", re.I),
     TEMPORAL_TYPES,
     "TIMESTAMP_WRONG_TYPE",
     "Timestamp column should be TIMESTAMP or DATE"),

    (re.compile(r"(_FL|_FLAG|_IND|_INDICATOR|^IS_|_IS$|_YN$|_BIT$)$", re.I),
     BOOLEAN_TYPES | NUMERIC_TYPES,
     "FLAG_WRONG_TYPE",
     "Boolean/flag column should be BOOLEAN or small integer"),

    (re.compile(r"(_AMT|_AMOUNT|_PRICE|_COST|_RATE|_VALUE|_TOTAL|_SUM|_QTY|_QUANTITY)$", re.I),
     NUMERIC_TYPES,
     "AMOUNT_WRONG_TYPE",
     "Amount/numeric column should use a numeric type"),
]

# Columns whose names imply PII
PII_KEYWORDS = {
    "SSN", "SOCIAL_SECURITY", "SOCIAL_SEC",
    "PASSPORT", "PASSPORT_NUM", "PASSPORT_NO",
    "EMAIL", "EMAIL_ADDR", "EMAIL_ADDRESS",
    "PHONE", "PHONE_NUM", "PHONE_NUMBER", "MOBILE", "MOBILE_NUM",
    "PASSWORD", "PASSWD", "PWD", "HASHED_PASSWORD",
    "CREDIT_CARD", "CARD_NUMBER", "CARD_NUM", "CVV", "CVC",
    "DOB", "BIRTH_DATE", "DATE_OF_BIRTH", "BIRTHDATE",
    "SALARY", "COMPENSATION", "INCOME", "WAGE",
    "ROUTING_NUMBER", "BANK_ACCOUNT", "ACCOUNT_NUMBER", "IBAN",
    "IP_ADDRESS", "IP_ADDR",
    "BIOMETRIC", "FINGERPRINT", "FACE_ID",
    "NATIONAL_ID", "TAX_ID", "NIN", "SIN",
    "MEDICAL_RECORD", "PATIENT_ID", "HEALTH_ID",
}

# Regex fragments to also catch partial-name matches (e.g. CUST_EMAIL_ADDR)
PII_PATTERNS = [
    re.compile(r"EMAIL", re.I),
    re.compile(r"PHONE|MOBILE", re.I),
    re.compile(r"SSN|SOCIAL.SEC", re.I),
    re.compile(r"PASSPORT", re.I),
    re.compile(r"PASSWORD|PASSWD|PWD", re.I),
    re.compile(r"CREDIT.?CARD|CARD.?NUM|CVV|CVC", re.I),
    re.compile(r"\bDOB\b|BIRTH.?DATE|DATE.?OF.?BIRTH", re.I),
    re.compile(r"\bSALARY\b|COMPENSATION|INCOME", re.I),
    re.compile(r"BANK.?ACCT|ACCOUNT.?NUM|ROUTING|IBAN", re.I),
    re.compile(r"\bIP.?ADDR", re.I),
    re.compile(r"NATIONAL.?ID|TAX.?ID|NIN\b|SIN\b", re.I),
]

# Generic/meaningless column names
GENERIC_NAMES = {
    "COL", "COL1", "COL2", "COL3", "COL4", "COL5",
    "COLUMN", "COLUMN1", "COLUMN2", "COLUMN3",
    "FIELD", "FIELD1", "FIELD2", "FIELD3",
    "DATA", "DATA1", "DATA2",
    "VALUE", "VAL", "VAL1",
    "INFO", "MISC", "OTHER", "EXTRA", "ATTR",
    "TEMP", "TMP", "TEST", "DUMMY",
    "A", "B", "C", "D", "E", "F", "X", "Y", "Z",
    "FLAG", "TYPE", "STATUS",  # only when used as a bare name
}

# Audit columns we expect on production tables
CREATED_AT_NAMES = {
    "CREATED_AT", "CREATE_DATE", "CREATED_DATE", "CREATED_DT",
    "CREATED_ON", "CREATE_DT", "INS_DATE", "INSERT_DATE",
    "INSERT_TS", "CREATED_TS", "CREATED_TIME",
}
UPDATED_AT_NAMES = {
    "UPDATED_AT", "UPDATE_DATE", "MODIFIED_DATE", "MODIFIED_DT",
    "LAST_MODIFIED", "MODIFIED_ON", "UPD_DATE", "LAST_UPDATED",
    "UPDATED_TS", "MODIFIED_TS", "LAST_MODIFIED_TS",
}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _normalise_type(raw_type: str) -> str:
    """Strip precision/scale from type string and upper-case it."""
    return (raw_type or "").split("(")[0].strip().upper()


def _fetch_live_column_metadata(table_asset: Any) -> Dict[str, Dict[str, Any]]:
    """Runs a real SELECT against INFORMATION_SCHEMA.COLUMNS for this table
    and returns {COLUMN_NAME: {"data_type": ..., "is_nullable": ..., "comment": ...}}.

    This is the SQL-generation replacement for the checks that used to trust
    the cached column-asset snapshot taken at scan time (MetadataAgent's
    earlier pass): type/nullability/comment can drift between that snapshot
    and when these checks actually run (e.g. an ALTER TABLE landed in
    between), so type/nullability/comment-dependent checks below re-fetch
    live, in ONE query per table rather than per-check, instead of trusting
    the snapshot. On query failure, returns {} — callers fall back to the
    column asset's cached raw_metadata for that column (see _column_type_info
    below), so one query hiccup doesn't take out every check on the table."""
    try:
        rows = sf_session.query(
            f"""
            SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COMMENT
            FROM {table_asset.database_name}.INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = '{table_asset.schema_name}'
              AND TABLE_NAME = '{table_asset.table_name}'
            """
        )
    except Exception as e:
        logger.warning(
            f"[DynamicRules] Live column metadata fetch failed for {table_asset.fqn}: {e}"
        )
        return {}
    return {
        r["COLUMN_NAME"]: {
            "data_type": r.get("DATA_TYPE") or "",
            "is_nullable": r.get("IS_NULLABLE") or "NO",
            "comment": r.get("COMMENT"),
        }
        for r in rows
    }


def _column_type_info(col_asset: Any, live_metadata: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Live INFORMATION_SCHEMA row for this column if the fetch succeeded,
    else the column asset's cached raw_metadata as a fallback."""
    live = live_metadata.get(col_asset.column_name)
    if live is not None:
        return live
    return col_asset.raw_metadata or {}


def _ensure_rule(
    code: str,
    name: str,
    description: str,
    category: str,
    severity: str,
    applies_to: List[str],
) -> Any:
    """Return a rule-shape object for the python_handler definition keyed by
    `code` (used as HANDLER_KEY, lowercased). Auto-creates the definition if
    missing. Callers use the returned object like the old Rule object
    (.severity, .code via .handler_key, .definition).

    NOTE 2026-07-15: this used to also create a DATABASE_NAME='*' global
    instance and return it. Globals are gone — a metadata handler now runs on
    a table only when RuleIntelligence proposed it (with human review) as a
    per-table instance. FindingsAgent gates run_dynamic_checks by
    allowed_rule_codes = {handler_keys of approved per-table instances}, so a
    dynamic check whose definition isn't proposed simply doesn't fire. The
    returned object is a lightweight shell used only for the finding's
    severity/code fields; no instance row is required to build a finding
    dict (create_findings_bulk fills instance_id from the FindingsAgent side
    when it wires the finding back to its approved instance).
    """
    from types import SimpleNamespace
    handler_key = code.lower()
    definition = storage.ensure_definition(handler_key, name, description, category, severity, applies_to)
    return SimpleNamespace(
        code=code,
        handler_key=handler_key,
        severity=severity,
        definition=definition,
    )


def _hash(definition_id: str) -> str:
    import hashlib
    return hashlib.sha256(f"{definition_id}|global".encode("utf-8")).hexdigest()


def _finding(
    asset_id: str,
    scan_id: str,
    rule: Any,
    title: str,
    description: str,
    context: Dict[str, Any],
    evidence: Dict[str, Any],
) -> Dict[str, Any]:
    """Build a finding dict from a rule-shape object. instance_id is left as
    None here — the caller (run_dynamic_checks) fills it from the
    handler_key → approved-instance-id map so the finding wires back to a
    per-table approved instance, not to a (no-longer-existing) global one.

    Normalizes evidence to the standard contract — {fail_count, total_count,
    sample_rows} — so the incident lifecycle can rely on those keys.
    Dynamic checks are metadata-shape audits (no failing rows), so counts
    default to 1/1 and sample_rows to []."""
    ev = dict(evidence or {})
    ev.setdefault("fail_count", 1)
    ev.setdefault("total_count", 1)
    ev.setdefault("sample_rows", [])
    return {
        "asset_id": asset_id,
        "scan_id": scan_id,
        "instance_id": getattr(rule, "id", None),
        "title": title,
        "description": description,
        "severity": rule.severity,
        "status": "open",
        "context": context,
        "evidence": ev,
    }


def _base_ctx(asset: Any) -> Dict[str, Any]:
    return {
        "database_name": asset.database_name,
        "schema_name": asset.schema_name,
        "table_name": asset.table_name,
        "fqn": asset.fqn,
    }


# ─────────────────────────────────────────────────────────────────────────────
# TABLE-LEVEL checks
# ─────────────────────────────────────────────────────────────────────────────

def check_no_primary_key(
    table_asset: Any, column_names: List[str], scan_id: str
) -> Optional[Dict]:
    """Flag tables that have no obvious primary key column."""
    upper_cols = {c.upper() for c in column_names}

    # Any column that looks like a surrogate PK
    pk_hint = any(
        re.search(r"(^ID$|_ID$|^PK_|_PK$|_KEY$|_SEQ$|_SURROGATE)", c)
        for c in upper_cols
    )
    if pk_hint:
        return None

    rule = _ensure_rule(
        code="NO_PRIMARY_KEY_HINT",
        name="Table May Be Missing a Primary Key",
        description=(
            "No column matching common primary-key naming patterns (ID, *_ID, PK_*, "
            "*_PK, *_KEY, *_SEQ) was found. Tables without a primary key risk duplicate "
            "rows and make joins, deduplication, and CDC harder."
        ),
        category='schema',
        severity='medium',
        applies_to=["table"],
    )
    ctx = {**_base_ctx(table_asset), "rule_code": rule.code}
    return _finding(
        table_asset.id, scan_id, rule,
        title=f"Table {table_asset.table_name} has no identifiable primary key",
        description=(
            f"{table_asset.fqn} has no column suggesting a primary key. "
            "Consider adding a surrogate key (e.g. TABLE_ID) for data integrity."
        ),
        context=ctx,
        evidence={"column_count": len(column_names),
                  "sample_columns": column_names[:10]},
    )


def check_missing_created_at(
    table_asset: Any, column_names: List[str], scan_id: str
) -> Optional[Dict]:
    """Flag tables missing a row-creation timestamp column."""
    upper_cols = {c.upper() for c in column_names}
    if upper_cols & CREATED_AT_NAMES:
        return None

    rule = _ensure_rule(
        code="MISSING_CREATED_AT",
        name="Missing Row Creation Timestamp",
        description=(
            "Production tables should track when rows were inserted via a column "
            "such as CREATED_AT, CREATE_DATE, or INSERT_TS. This enables auditing, "
            "incremental loads, and change tracking."
        ),
        category='schema',
        severity='medium',
        applies_to=["table"],
    )
    ctx = {**_base_ctx(table_asset), "rule_code": rule.code}
    return _finding(
        table_asset.id, scan_id, rule,
        title=f"Table {table_asset.table_name} is missing a created-at timestamp",
        description=(
            f"{table_asset.fqn} has no CREATED_AT or equivalent column. "
            "Add one (e.g. CREATED_AT TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()) "
            "to track row insertion time."
        ),
        context=ctx,
        evidence={"expected_names": list(CREATED_AT_NAMES)[:6]},
    )


def check_missing_updated_at(
    table_asset: Any, column_names: List[str], scan_id: str
) -> Optional[Dict]:
    """Flag tables missing a last-updated timestamp column."""
    upper_cols = {c.upper() for c in column_names}
    if upper_cols & UPDATED_AT_NAMES:
        return None

    rule = _ensure_rule(
        code="MISSING_UPDATED_AT",
        name="Missing Row Updated Timestamp",
        description=(
            "Mutable tables should track the last modification time via UPDATED_AT, "
            "MODIFIED_DATE, or equivalent. Required for CDC, incremental ETL, and auditing."
        ),
        category='schema',
        severity='low',
        applies_to=["table"],
    )
    ctx = {**_base_ctx(table_asset), "rule_code": rule.code}
    return _finding(
        table_asset.id, scan_id, rule,
        title=f"Table {table_asset.table_name} is missing an updated-at timestamp",
        description=(
            f"{table_asset.fqn} has no UPDATED_AT or equivalent column. "
            "Add one to support change data capture and incremental loads."
        ),
        context=ctx,
        evidence={"expected_names": list(UPDATED_AT_NAMES)[:6]},
    )


def check_too_many_columns(
    table_asset: Any, column_names: List[str], scan_id: str
) -> Optional[Dict]:
    """Flag tables that exceed the maximum column count."""
    count = len(column_names)
    if count <= MAX_COLUMNS:
        return None

    rule = _ensure_rule(
        code="TOO_MANY_COLUMNS",
        name="Table Has Too Many Columns",
        description=(
            f"Tables with more than {MAX_COLUMNS} columns often indicate poor "
            "normalisation, merged business entities, or accumulated technical debt. "
            "Consider decomposing into focused, related tables."
        ),
        category='schema',
        severity='low',
        applies_to=["table"],
    )
    ctx = {**_base_ctx(table_asset), "rule_code": rule.code}
    return _finding(
        table_asset.id, scan_id, rule,
        title=f"Table {table_asset.table_name} has {count} columns (threshold: {MAX_COLUMNS})",
        description=(
            f"{table_asset.fqn} has {count} columns, exceeding the recommended "
            f"maximum of {MAX_COLUMNS}. Review whether it should be split into "
            "smaller, more cohesive tables."
        ),
        context=ctx,
        evidence={"column_count": count, "threshold": MAX_COLUMNS},
    )


def check_inconsistent_naming(
    table_asset: Any, column_names: List[str], scan_id: str
) -> Optional[Dict]:
    """Flag tables where column names mix multiple casing styles."""
    if len(column_names) < 3:
        return None

    upper = sum(1 for c in column_names if c == c.upper() and "_" in c)   # SNAKE_UPPER
    lower = sum(1 for c in column_names if c == c.lower() and "_" in c)   # snake_lower
    camel = sum(1 for c in column_names
                if re.search(r"[a-z][A-Z]", c) and "_" not in c)          # camelCase

    styles_present = sum(1 for s in [upper, lower, camel] if s > 1)
    if styles_present < 2:
        return None

    rule = _ensure_rule(
        code="INCONSISTENT_COLUMN_NAMING",
        name="Inconsistent Column Naming Style",
        description=(
            "Column names should follow a single naming convention throughout a table "
            "(e.g. all UPPER_SNAKE_CASE). Mixing styles makes queries harder to write "
            "and datasets harder to join."
        ),
        category='naming',
        severity='low',
        applies_to=["table"],
    )
    ctx = {**_base_ctx(table_asset), "rule_code": rule.code}
    return _finding(
        table_asset.id, scan_id, rule,
        title=f"Table {table_asset.table_name} has mixed column naming styles",
        description=(
            f"{table_asset.fqn} uses multiple naming conventions across its columns "
            f"(UPPER_SNAKE: {upper}, lower_snake: {lower}, camelCase: {camel}). "
            "Standardise to a single convention."
        ),
        context=ctx,
        evidence={"upper_snake": upper, "lower_snake": lower, "camel_case": camel,
                  "sample_columns": column_names[:10]},
    )


# ─────────────────────────────────────────────────────────────────────────────
# COLUMN-LEVEL checks
# ─────────────────────────────────────────────────────────────────────────────

def check_pii_column(
    col_asset: Any, scan_id: str
) -> Optional[Dict]:
    """Flag columns whose names suggest PII without a masking indicator."""
    col_upper = (col_asset.column_name or "").upper()

    # Exact keyword match
    keyword = col_upper if col_upper in PII_KEYWORDS else None

    # Regex partial match
    if not keyword:
        for pattern in PII_PATTERNS:
            if pattern.search(col_upper):
                keyword = pattern.pattern
                break

    if not keyword:
        return None

    rule = _ensure_rule(
        code="PII_COLUMN_NO_MASKING",
        name="Potential PII Column Without Masking Policy",
        description=(
            "Columns whose names suggest personally identifiable information "
            "(e.g. EMAIL, SSN, PHONE, PASSWORD, DOB, SALARY) should have a "
            "Snowflake Dynamic Data Masking policy applied and a PII tag attached."
        ),
        category='security',
        severity='high',
        applies_to=["column"],
    )
    ctx = {
        **_base_ctx(col_asset),
        "column_name": col_asset.column_name,
        "rule_code": rule.code,
    }
    return _finding(
        col_asset.id, scan_id, rule,
        title=f"Column {col_asset.column_name} may contain PII without masking",
        description=(
            f"Column '{col_asset.column_name}' in {col_asset.fqn} appears to store "
            f"sensitive/PII data (matched: '{keyword}'). Ensure a Snowflake masking "
            "policy is applied and the column is tagged with a PII classification."
        ),
        context=ctx,
        evidence={"matched_pattern": keyword,
                  "data_type": (col_asset.raw_metadata or {}).get("data_type", "unknown")},
    )


def check_generic_column_name(
    col_asset: Any, scan_id: str
) -> Optional[Dict]:
    """Flag columns with uninformative generic names."""
    col_upper = (col_asset.column_name or "").upper()
    if col_upper not in GENERIC_NAMES:
        return None

    rule = _ensure_rule(
        code="GENERIC_COLUMN_NAME",
        name="Generic / Uninformative Column Name",
        description=(
            "Column names like COL1, DATA, VALUE, FIELD, or MISC provide no semantic "
            "context. Rename them to describe what they actually store."
        ),
        category='naming',
        severity='low',
        applies_to=["column"],
    )
    ctx = {
        **_base_ctx(col_asset),
        "column_name": col_asset.column_name,
        "rule_code": rule.code,
    }
    return _finding(
        col_asset.id, scan_id, rule,
        title=f"Column {col_asset.column_name} has a generic, uninformative name",
        description=(
            f"Column '{col_asset.column_name}' in {col_asset.fqn} has a generic name "
            "that gives no information about what it stores. Rename it to be descriptive."
        ),
        context=ctx,
        evidence={"column_name": col_asset.column_name},
    )


def check_column_type_mismatch(
    col_asset: Any, scan_id: str, live_metadata: Dict[str, Dict[str, Any]],
) -> Optional[Dict]:
    """Flag columns whose name implies one type but are stored as another."""
    col_upper = (col_asset.column_name or "").upper()
    raw_type = _column_type_info(col_asset, live_metadata).get("data_type", "") or ""
    actual_type = _normalise_type(raw_type)

    for pattern, expected_types, suffix, label in NAME_TYPE_RULES:
        if not pattern.search(col_upper):
            continue
        if actual_type in expected_types:
            return None  # type matches expectation

        rule_code = f"COLUMN_{suffix}"
        rule = _ensure_rule(
                code=rule_code,
            name=f"Column Type Mismatch — {label}",
            description=(
                f"Column names matching '{pattern.pattern}' should use types such as "
                f"{', '.join(sorted(expected_types)[:4])}. "
                "Storing them as other types causes implicit conversions, silent bugs, "
                "and join failures."
            ),
            category='schema',
            severity='medium',
            applies_to=["column"],
        )
        ctx = {
            **_base_ctx(col_asset),
            "column_name": col_asset.column_name,
            "rule_code": rule_code,
        }
        return _finding(
            col_asset.id, scan_id, rule,
            title=f"Column {col_asset.column_name} has unexpected type {raw_type or 'UNKNOWN'}",
            description=(
                f"Column '{col_asset.column_name}' in {col_asset.fqn} appears to be "
                f"a {label.lower()} but is defined as {raw_type or 'UNKNOWN'}. "
                f"Expected one of: {', '.join(sorted(expected_types)[:5])}."
            ),
            context=ctx,
            evidence={"actual_type": raw_type,
                      "expected_types": list(sorted(expected_types)[:5]),
                      "name_pattern": pattern.pattern},
        )
    return None


def check_fk_without_constraint(
    col_asset: Any, table_name: str, scan_id: str
) -> Optional[Dict]:
    """Flag _ID columns that look like FK references but have no constraint."""
    col_upper = (col_asset.column_name or "").upper()

    # Must end in _ID but not be the table's own surrogate PK
    if not re.search(r"_ID$", col_upper):
        return None

    table_upper = table_name.upper().rstrip("S")  # rough singularisation
    own_pk = {f"{table_upper}_ID", "SURROGATE_KEY", "ROW_ID", "RECORD_ID"}
    if col_upper in own_pk:
        return None

    rule = _ensure_rule(
        code="FK_COLUMN_NO_CONSTRAINT",
        name="Foreign Key Column Without FK Constraint",
        description=(
            "Columns ending in '_ID' typically reference another table. "
            "Snowflake does not enforce FK constraints by default — add an unenforced "
            "REFERENCES clause for documentation and data lineage tools."
        ),
        category='schema',
        severity='low',
        applies_to=["column"],
    )
    ctx = {
        **_base_ctx(col_asset),
        "column_name": col_asset.column_name,
        "rule_code": rule.code,
    }
    return _finding(
        col_asset.id, scan_id, rule,
        title=f"Column {col_asset.column_name} looks like a FK but has no constraint",
        description=(
            f"Column '{col_asset.column_name}' in {col_asset.fqn} appears to be a "
            "foreign key reference but no FK constraint is defined. Add a REFERENCES "
            "constraint (even unenforced) for lineage documentation."
        ),
        context=ctx,
        evidence={"column_name": col_asset.column_name},
    )


def check_nullable_id_column(
    col_asset: Any, scan_id: str, live_metadata: Dict[str, Dict[str, Any]],
) -> Optional[Dict]:
    """Flag ID/PK columns that allow NULLs."""
    col_upper = (col_asset.column_name or "").upper()
    is_id = re.search(r"(^ID$|_ID$|^PK_|_PK$|_KEY$)", col_upper)
    if not is_id:
        return None

    is_nullable = _column_type_info(col_asset, live_metadata).get("is_nullable", "NO")
    if str(is_nullable).upper() not in ("YES", "Y", "TRUE", "1"):
        return None

    rule = _ensure_rule(
        code="NULLABLE_ID_COLUMN",
        name="Nullable ID / Primary Key Column",
        description=(
            "Primary key and identifier columns should never be NULL. "
            "A nullable PK column breaks referential integrity and causes "
            "unexpected results in GROUP BY, JOIN, and deduplication operations."
        ),
        category='schema',
        severity='high',
        applies_to=["column"],
    )
    ctx = {
        **_base_ctx(col_asset),
        "column_name": col_asset.column_name,
        "rule_code": rule.code,
    }
    return _finding(
        col_asset.id, scan_id, rule,
        title=f"ID column {col_asset.column_name} allows NULL values",
        description=(
            f"Column '{col_asset.column_name}' in {col_asset.fqn} appears to be an "
            "identifier/primary key but is defined as NULLABLE. "
            "Add a NOT NULL constraint to ensure data integrity."
        ),
        context=ctx,
        evidence={"is_nullable": is_nullable},
    )


def check_date_stored_as_varchar(
    col_asset: Any, scan_id: str, live_metadata: Dict[str, Dict[str, Any]],
) -> Optional[Dict]:
    """Flag date/timestamp columns stored as VARCHAR/TEXT."""
    col_upper = (col_asset.column_name or "").upper()
    raw_type = _column_type_info(col_asset, live_metadata).get("data_type", "") or ""
    actual_type = _normalise_type(raw_type)

    date_name = re.search(
        r"(_DATE|_DT|_DAY|_AT|_ON|_TS|_TIME|_TIMESTAMP)$", col_upper
    )
    if not date_name:
        return None
    if actual_type not in VARCHAR_TYPES:
        return None

    rule = _ensure_rule(
        code="DATE_STORED_AS_VARCHAR",
        name="Date/Timestamp Column Stored as VARCHAR",
        description=(
            "Columns whose names suggest a date or timestamp are stored as VARCHAR. "
            "This prevents date arithmetic, sorting, filtering, and indexing from "
            "working correctly. Cast or convert to DATE or TIMESTAMP."
        ),
        category='data_quality',
        severity='high',
        applies_to=["column"],
    )
    ctx = {
        **_base_ctx(col_asset),
        "column_name": col_asset.column_name,
        "rule_code": rule.code,
    }
    return _finding(
        col_asset.id, scan_id, rule,
        title=f"Column {col_asset.column_name} stores a date/time value as {raw_type}",
        description=(
            f"Column '{col_asset.column_name}' in {col_asset.fqn} appears to store a "
            f"date or timestamp value but is defined as {raw_type}. "
            "Convert to DATE, TIMESTAMP_NTZ, or TIMESTAMP_LTZ to enable proper "
            "date arithmetic and partitioning."
        ),
        context=ctx,
        evidence={"actual_type": raw_type},
    )


def check_boolean_stored_as_varchar(
    col_asset: Any, scan_id: str, live_metadata: Dict[str, Dict[str, Any]],
) -> Optional[Dict]:
    """Flag boolean/flag columns stored as VARCHAR instead of BOOLEAN or a numeric type."""
    col_upper = (col_asset.column_name or "").upper()
    raw_type = _column_type_info(col_asset, live_metadata).get("data_type", "") or ""
    actual_type = _normalise_type(raw_type)

    # Column name must look like a boolean/flag field
    bool_name = re.search(
        r"(_FL$|_FLAG$|_IND$|_INDICATOR$|^IS_|_IS$|_YN$|_BIT$)", col_upper
    )
    if not bool_name:
        return None

    # Only flag if it is stored as a text type
    if actual_type not in VARCHAR_TYPES:
        return None

    rule = _ensure_rule(
        code="BOOLEAN_STORED_AS_VARCHAR",
        name="Boolean/Flag Column Stored as VARCHAR",
        description=(
            "Columns whose names suggest a boolean or flag value (_FL, _FLAG, _IND, "
            "IS_, _YN) are stored as VARCHAR. This allows invalid values (e.g. 'maybe', "
            "'3') and prevents efficient filtering. Use BOOLEAN or a small integer type."
        ),
        category='data_quality',
        severity='medium',
        applies_to=["column"],
    )
    ctx = {
        **_base_ctx(col_asset),
        "column_name": col_asset.column_name,
        "rule_code": rule.code,
    }
    return _finding(
        col_asset.id, scan_id, rule,
        title=f"Boolean column {col_asset.column_name} is stored as {raw_type}",
        description=(
            f"Column '{col_asset.column_name}' in {col_asset.fqn} appears to be a "
            f"boolean/flag column but is defined as {raw_type}. "
            "Convert to BOOLEAN or NUMBER(1) to enforce valid values and improve query performance."
        ),
        context=ctx,
        evidence={"actual_type": raw_type, "name_pattern": bool_name.group()},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point called by RuleEngine
# ─────────────────────────────────────────────────────────────────────────────

# Mapping of dynamic check function → rule code it produces.
# Trimmed 2026-07-15: only the 5 kept metadata handlers dispatch here. The
# other check_* functions further down in this file (check_missing_created_at,
# check_too_many_columns, check_inconsistent_naming, check_generic_column_name,
# check_column_type_mismatch, check_fk_without_constraint,
# check_missing_updated_at) are intentionally NOT wired up — their definitions
# are gone from the library, so allowed_rule_codes would never include them.
# Kept as dead code rather than deleted to preserve blame history; revive by
# adding back to these dicts and re-seeding the definition.
_TABLE_CHECK_CODES = {
    check_no_primary_key:       "NO_PRIMARY_KEY_HINT",
}

_COLUMN_CHECK_CODES = {
    check_pii_column:                "PII_COLUMN_NO_MASKING",
    check_nullable_id_column:        "NULLABLE_ID_COLUMN",
    check_date_stored_as_varchar:    "DATE_STORED_AS_VARCHAR",
    check_boolean_stored_as_varchar: "BOOLEAN_STORED_AS_VARCHAR",
}

# Every python_handler HANDLER_KEY actually served by this module (lower-cased,
# matching RULE_DEFINITIONS.HANDLER_KEY) — table + column codes, plus the
# per-subtype codes check_column_type_mismatch can emit (COLUMN_ID_WRONG_TYPE,
# COLUMN_DATE_WRONG_TYPE, etc., seeded individually in rule_engine.py's
# initialize_default_rules but all produced by the one _NAME_TYPE_RULES loop
# above). RuleEngine imports this so execute_rules() can skip these instances
# instead of attempting (and warning on) a Python-object dispatch that only
# ever covers 3 keys — these are executed via run_dynamic_checks() instead.
DYNAMIC_RULE_HANDLER_KEYS = frozenset(
    code.lower() for code in {**_TABLE_CHECK_CODES, **_COLUMN_CHECK_CODES}.values()
)


def run_dynamic_checks(
    table_asset: Any,
    column_assets: List[Any],
    scan_id: str,
    allowed_rule_codes=None,  # Optional[Set[str]] — from RuleClassifierAgent
    instance_id_by_handler_key: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """
    Run dynamic checks for a table and its columns.
    If allowed_rule_codes is given, only runs checks whose rule code is in that set.
    Returns finding dicts (not yet persisted) and commits any newly registered Rule rows.

    instance_id_by_handler_key: {handler_key_lower: instance_id} for the
    per-table approved instances of these dynamic checks. Findings emitted
    here are stamped with the matching instance_id so they wire up to the
    approved instance in RULE_INSTANCES (and thus into RULE_EXECUTIONS via
    FindingsAgent._log_executions). Without this map, findings would have
    instance_id=None and the execution log wouldn't tie them back to the
    approved instance. See FindingsAgent for how the map is built.
    """
    findings: List[Dict[str, Any]] = []
    col_names = [a.column_name for a in column_assets if a.column_name]
    instance_id_by_handler_key = instance_id_by_handler_key or {}

    # Live INFORMATION_SCHEMA.COLUMNS read for this table — ONE query, reused
    # by every type/nullability-dependent check below, instead of trusting
    # each column asset's cached raw_metadata from MetadataAgent's earlier
    # scan pass (which can drift if an ALTER TABLE landed in between).
    live_metadata = _fetch_live_column_metadata(table_asset)

    _NEEDS_LIVE_METADATA = {
        check_column_type_mismatch, check_nullable_id_column,
        check_date_stored_as_varchar, check_boolean_stored_as_varchar,
    }

    def _allowed(code: str) -> bool:
        if allowed_rule_codes is None:
            return True
        # COLUMN_TYPE_MISMATCH spawns multiple sub-codes — allow if any match
        if code == "COLUMN_TYPE_MISMATCH":
            return any(
                c.startswith("COLUMN_") and c.endswith("_WRONG_TYPE")
                for c in allowed_rule_codes
            ) or "COLUMN_TYPE_MISMATCH" in allowed_rule_codes
        return code in allowed_rule_codes

    # ── Table-level ──────────────────────────────────────────────────────────
    for fn, rule_code in _TABLE_CHECK_CODES.items():
        if not _allowed(rule_code):
            logger.debug(f"[DynamicRules] Skipping {rule_code} (classifier decision)")
            continue
        result = fn(table_asset, col_names, scan_id)
        if result:
            findings.append(result)

    # ── Column-level ─────────────────────────────────────────────────────────
    for col_asset in column_assets:
        for fn, rule_code in _COLUMN_CHECK_CODES.items():
            if not _allowed(rule_code):
                continue
            if fn is check_fk_without_constraint:
                result = fn(col_asset, table_asset.table_name or "", scan_id)
            elif fn in _NEEDS_LIVE_METADATA:
                result = fn(col_asset, scan_id, live_metadata)
            else:
                result = fn(col_asset, scan_id)
            if result:
                findings.append(result)

    # Wire each finding to its approved per-table instance. context.rule_code
    # holds the upper-cased HANDLER_KEY (see _base_ctx callers), which we
    # lower-case to look up in the caller-supplied map. Findings whose
    # handler has no approved instance for this table are dropped — globals
    # are gone, so a dynamic finding without a matching approved instance is
    # orphaned and would show up with instance_id=None (unjoinable to any
    # rule) if we let it through.
    #
    # Runs unconditionally now (not gated on `if instance_id_by_handler_key`):
    # if no handlers were approved, the map is empty and every finding gets
    # dropped, which is correct. The old gate skipped this pass entirely on
    # an empty map, letting orphan findings survive.
    for f in findings:
        ctx = f.get("context") or {}
        handler_key = (ctx.get("rule_code") or "").lower()
        iid = instance_id_by_handler_key.get(handler_key)
        if iid:
            f["instance_id"] = iid
        elif not f.get("instance_id"):
            logger.debug(
                f"[DynamicRules] Finding for handler_key={handler_key!r} has no matching "
                "approved instance — dropping (not in allowed_instance_ids)."
            )
    findings = [f for f in findings if f.get("instance_id")]

    logger.info(
        f"[DynamicRules] {table_asset.fqn}: {len(findings)} dynamic findings"
        + (f" (filtered by classifier)" if allowed_rule_codes else "")
    )
    return findings
