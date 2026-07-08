"""
Phase 4 — Shift-Left DDL Validation

POST /api/v1/validate/ddl
  Accepts a CREATE TABLE SQL statement, runs all active data quality rules
  against it, and returns pass/fail + findings — no Snowflake connection needed,
  no DB writes.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel

from app.core.database import get_db
from app.services.ddl_parser import parse_create_table, DDLParseError
from app.services.rule_engine import RuleEngine
import logging

router = APIRouter()
logger = logging.getLogger(__name__)

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


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
def validate_ddl(
    request: DDLValidateRequest,
    db: Session = Depends(get_db),
):
    """
    Validate a CREATE TABLE statement against all active data quality rules.
    Returns pass/fail + full findings list. Nothing is written to the database.
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

    # ── Run all active rules — sentinel scan_id prevents any DB writes ────────
    rule_engine = RuleEngine(db)
    try:
        findings_data = rule_engine.execute_all_rules(
            table_asset, column_assets, scan_id="__ddl_validate__"
        )
    except Exception as e:
        logger.error(f"[ValidateDDL] Rule engine error: {e}")
        raise HTTPException(status_code=500, detail=f"Rule engine failed: {str(e)}")

    # ── Resolve rule names from DB ────────────────────────────────────────────
    from app.models.rule import Rule
    rule_name_map: dict = {}
    for fd in findings_data:
        rule_id = fd.get("rule_id")
        if rule_id and rule_id not in rule_name_map:
            rule = db.query(Rule).filter(Rule.id == rule_id).first()
            rule_name_map[rule_id] = rule.name if rule else ""

    # ── Build response findings ───────────────────────────────────────────────
    findings: List[DDLFinding] = []
    for fd in findings_data:
        ctx      = fd.get("context") or {}
        rule_code = ctx.get("rule_code", "")
        rule_id   = fd.get("rule_id", "")
        severity  = fd.get("severity", "info")
        if hasattr(severity, "value"):
            severity = severity.value

        findings.append(DDLFinding(
            rule_code=rule_code,
            rule_name=rule_name_map.get(rule_id, rule_code),
            severity=severity,
            title=fd.get("title", ""),
            description=fd.get("description", ""),
            column_name=ctx.get("column_name") or None,
        ))

    # Sort by severity
    findings.sort(key=lambda f: SEVERITY_ORDER.get(f.severity, 5))

    blocked_by = sum(1 for f in findings if f.severity in fail_on)
    passed = blocked_by == 0

    # Count rules checked
    active_table_rules  = len(rule_engine.get_active_rules("table"))
    active_column_rules = len(rule_engine.get_active_rules("column"))
    rules_checked = active_table_rules + active_column_rules * len(column_assets)

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
