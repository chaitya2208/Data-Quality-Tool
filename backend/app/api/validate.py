"""
Phase 4 — Shift-Left DDL Validation (Gate 1)

POST /api/v1/validate/ddl
  Accepts a CREATE TABLE SQL statement, runs the metadata/convention checks
  against the *parsed* DDL, and returns pass/fail + findings. No findings or
  scans are persisted. (The checks do call storage.ensure_definition to resolve
  each rule's library definition — a read against the server's warm Snowflake
  session, and idempotent since the 5 definitions are seeded at startup. The
  pre-commit hook itself stays offline: it only speaks HTTP to this endpoint.)

Why this bypasses the normal findings pipeline
-----------------------------------------------
The live pipeline (run_dynamic_checks) only emits a finding when the table has
an *approved per-table rule instance* — findings without one are dropped
(dynamic_rules.py). A brand-new table in a pre-commit hook has no instances
yet, so routing DDL through run_dynamic_checks would always return zero.

Instead we call the pure pattern-check functions directly. They are
self-contained (name + type heuristics), read the parsed column metadata from
`raw_metadata`, and — because we pass `live_metadata={}` — fall back to that
parsed metadata rather than querying the (nonexistent) live table. This is the
"convention floor":
the rules that hold for *every* table, independent of its data. Data-level /
sql_template rules are intentionally NOT run here — they need a live table and
are the job of the scheduled workflow (Gate 2).
"""
from fastapi import APIRouter, HTTPException
from typing import List, Optional
from pydantic import BaseModel
import logging

from app.services.ddl_parser import parse_create_table, DDLParseError
from app.services import dynamic_rules as dr

router = APIRouter()
logger = logging.getLogger(__name__)

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}

# The metadata/convention checks that can run against parsed DDL alone.
# These mirror the 5 python_handler definitions kept in the live library
# (rule_engine.initialize_default_rules) — no library pollution, no reviving
# trimmed rules. Table checks take (table_asset, column_names, scan_id);
# column checks take (col_asset, scan_id[, live_metadata]).
_TABLE_CHECKS = [dr.check_no_primary_key]
_COLUMN_CHECKS = [
    dr.check_pii_column,               # (col, scan_id)
    dr.check_nullable_id_column,       # (col, scan_id, live_metadata)
    dr.check_date_stored_as_varchar,   # (col, scan_id, live_metadata)
    dr.check_boolean_stored_as_varchar,# (col, scan_id, live_metadata)
]
# Column checks that take the extra live_metadata arg. Passing {} forces the
# fallback to each column asset's parsed raw_metadata (see _column_type_info).
_NEEDS_LIVE_METADATA = {
    dr.check_nullable_id_column,
    dr.check_date_stored_as_varchar,
    dr.check_boolean_stored_as_varchar,
}


class DDLValidateRequest(BaseModel):
    sql: str
    fail_on: List[str] = ["critical"]   # severities that block the build


class DDLFinding(BaseModel):
    rule_code:   str
    rule_name:   str
    severity:    str
    title:       str
    description: str
    column_name: Optional[str] = None


class DDLValidateResponse(BaseModel):
    passed:          bool
    table_name:      str
    columns_parsed:  int
    rules_checked:   int
    findings_count:  int
    blocked_by:      int           # findings whose severity is in fail_on
    fail_on:         List[str]
    findings:        List[DDLFinding]


@router.post("/ddl", response_model=DDLValidateResponse)
def validate_ddl(request: DDLValidateRequest):
    """
    Validate a CREATE TABLE statement against the metadata/convention rules.
    Returns pass/fail + full findings list. Nothing is written to storage.
    """
    if not request.sql.strip():
        raise HTTPException(status_code=400, detail="SQL cannot be empty")

    fail_on = [s.lower() for s in request.fail_on]

    # ── Parse DDL → in-memory assets ─────────────────────────────────────────
    try:
        table_asset, column_assets = parse_create_table(request.sql)
    except DDLParseError as e:
        raise HTTPException(status_code=400, detail=f"DDL parse error: {str(e)}")
    except Exception as e:
        logger.error(f"[ValidateDDL] Unexpected parse error: {e}")
        raise HTTPException(status_code=400, detail=f"Could not parse SQL: {str(e)}")

    # ── Run the convention checks directly on parsed assets ──────────────────
    scan_id = "__ddl_validate__"
    column_names = [c.column_name for c in column_assets if c.column_name]
    raw_findings: List[dict] = []

    for check in _TABLE_CHECKS:
        try:
            result = check(table_asset, column_names, scan_id)
            if result:
                raw_findings.append(result)
        except Exception as e:
            logger.error(f"[ValidateDDL] Table check {check.__name__} failed: {e}")

    for col_asset in column_assets:
        for check in _COLUMN_CHECKS:
            try:
                if check in _NEEDS_LIVE_METADATA:
                    result = check(col_asset, scan_id, {})   # {} → use parsed metadata
                else:
                    result = check(col_asset, scan_id)
                if result:
                    raw_findings.append(result)
            except Exception as e:
                logger.error(
                    f"[ValidateDDL] Column check {check.__name__} on "
                    f"{col_asset.column_name} failed: {e}"
                )

    # ── Shape findings for the response ──────────────────────────────────────
    findings: List[DDLFinding] = []
    for fd in raw_findings:
        ctx = fd.get("context") or {}
        severity = fd.get("severity", "info")
        if hasattr(severity, "value"):
            severity = severity.value
        findings.append(DDLFinding(
            rule_code=ctx.get("rule_code", ""),
            rule_name=ctx.get("rule_code", ""),
            severity=severity,
            title=fd.get("title", ""),
            description=fd.get("description", ""),
            column_name=ctx.get("column_name") or None,
        ))

    findings.sort(key=lambda f: SEVERITY_ORDER.get(f.severity, 5))

    blocked_by = sum(1 for f in findings if f.severity in fail_on)
    passed = blocked_by == 0
    rules_checked = len(_TABLE_CHECKS) + len(_COLUMN_CHECKS) * len(column_assets)

    logger.info(
        f"[ValidateDDL] {table_asset.table_name}: "
        f"{'PASSED' if passed else 'FAILED'} — "
        f"{len(findings)} finding(s), {blocked_by} blocking"
    )

    return DDLValidateResponse(
        passed=passed,
        table_name=table_asset.table_name,
        columns_parsed=len(column_assets),
        rules_checked=rules_checked,
        findings_count=len(findings),
        blocked_by=blocked_by,
        fail_on=fail_on,
        findings=findings,
    )
