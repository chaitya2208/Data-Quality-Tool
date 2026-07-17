from fastapi import APIRouter, HTTPException
from typing import Optional
from app.services import storage
from app.schemas.finding import FindingResponse, FindingListResponse, FindingUpdate
from datetime import datetime
from collections import defaultdict
import logging
import time

logger = logging.getLogger(__name__)

router = APIRouter()

# Simple TTL cache — avoids re-hitting Snowflake on every page visit
_findings_cache: dict = {}   # key → (expires_at, payload)
_CACHE_TTL = 30              # seconds


def _cache_key(asset_id, scan_id, status, severity, connection_id, skip, limit):
    return f"{asset_id}|{scan_id}|{status}|{severity}|{connection_id}|{skip}|{limit}"


def _invalidate_findings_cache():
    _findings_cache.clear()


@router.get("", response_model=FindingListResponse)
def list_findings(
    asset_id: Optional[str] = None,
    scan_id: Optional[str] = None,
    status: Optional[str] = None,
    severity: Optional[str] = None,
    connection_id: Optional[str] = None,
    skip: int = 0,
    limit: int = 5000,
):
    """List all findings with optional filters"""
    key = _cache_key(asset_id, scan_id, status, severity, connection_id, skip, limit)
    cached = _findings_cache.get(key)
    if cached and cached[0] > time.time():
        return cached[1]

    total, raw_findings = storage.list_findings(
        asset_id=asset_id, scan_id=scan_id, status=status, severity=severity,
        connection_id=connection_id, skip=skip, limit=limit,
    )
    findings = []
    for f in raw_findings:
        try:
            findings.append(FindingResponse.model_validate(f, from_attributes=True))
        except Exception as e:
            logger.warning(f"[findings] Skipping unserializable finding {getattr(f, 'id', '?')}: {e}")
    result = FindingListResponse(total=total, findings=findings)
    _findings_cache[key] = (time.time() + _CACHE_TTL, result)
    return result


@router.get("/{finding_id}", response_model=FindingResponse)
def get_finding(finding_id: str):
    """Get a specific finding by ID"""
    finding = storage.get_finding(finding_id)
    if not finding:
        raise HTTPException(status_code=404, detail="Finding not found")
    return finding


@router.patch("/{finding_id}", response_model=FindingResponse)
def update_finding(finding_id: str, update_data: FindingUpdate):
    """Update a finding (status, assignment, resolution notes)"""
    finding = storage.get_finding(finding_id)
    if not finding:
        raise HTTPException(status_code=404, detail="Finding not found")

    fields = {}
    if update_data.status is not None:
        # FindingStatus is a (str, Enum); the Snowflake connector rejects binding
        # the enum member itself ("Binding data in type (findingstatus) is not
        # supported"), so coerce to its plain string value.
        status_value = update_data.status.value if hasattr(update_data.status, "value") else str(update_data.status)
        fields["status"] = status_value
        if status_value == "resolved" and not finding.resolved_at:
            fields["resolved_at"] = datetime.utcnow()
        elif status_value == "closed" and not finding.closed_at:
            fields["closed_at"] = datetime.utcnow()

    if update_data.assigned_to is not None:
        fields["assigned_to"] = update_data.assigned_to

    if update_data.resolution_notes is not None:
        fields["resolution_notes"] = update_data.resolution_notes

    result = storage.update_finding(finding_id, **fields)
    _invalidate_findings_cache()

    # If this update resolved/closed the finding and it was the LAST open finding
    # for its run, kick an immediate verification so the workflow can complete
    # right away instead of waiting for the 5-min auto-verify cycle. Best-effort:
    # any failure here must never break the PATCH response.
    _CLOSED_STATUSES = {"resolved", "false_positive", "wont_fix", "closed"}
    if update_data.status is not None and fields.get("status") in _CLOSED_STATUSES:
        try:
            _maybe_trigger_verification(finding.scan_id)
        except Exception as e:
            logger.warning(f"[findings] post-resolve verify trigger failed: {e}")

    return result


def _maybe_trigger_verification(scan_id: Optional[str]) -> None:
    """When a run's findings are all resolved, verify now (delay=0) rather than
    waiting for the periodic auto-verify. Only acts on runs in awaiting_fixes."""
    if not scan_id:
        return
    run = storage.get_agent_run_by_scan(scan_id)
    if not run or run.status != "awaiting_fixes":
        return
    if storage.count_open_findings_for_scan(scan_id) > 0:
        return  # still open findings — nothing to complete yet
    from app.services.agents import auto_verify_scheduler
    auto_verify_scheduler.schedule(run.id, delay=0)
    logger.info(f"[findings] All findings resolved for run {run.id} — verifying now")


@router.get("/stats/by-database")
def get_findings_by_database(connection_id: Optional[str] = None):
    """
    Aggregate findings by database → schema → table.
    Joins findings with assets to get proper database/schema/table names.
    Used for the dashboard chart. Optionally scoped to one connection.
    """
    pairs = storage.findings_with_asset_not_closed(connection_id=connection_id)

    db_map: dict = defaultdict(lambda: defaultdict(lambda: {"total": 0, "by_severity": defaultdict(int)}))

    for finding, asset in pairs:
        db_name    = asset.database_name or "Unknown"
        table_name = asset.table_name or asset.fqn or "Unknown"
        db_map[db_name][table_name]["total"] += 1
        db_map[db_name][table_name]["by_severity"][finding.severity] += 1

    result = []
    for db_name, tables in db_map.items():
        table_list = []
        for table_name, counts in tables.items():
            table_list.append({
                "table_name": table_name,
                "total": counts["total"],
                "by_severity": dict(counts["by_severity"]),
            })
        table_list.sort(key=lambda t: t["total"], reverse=True)
        total = sum(t["total"] for t in table_list)
        result.append({
            "database": db_name,
            "total": total,
            "tables": table_list,
        })

    result.sort(key=lambda d: d["total"], reverse=True)
    return result


@router.get("/stats/summary")
def get_findings_summary(connection_id: Optional[str] = None):
    """Get summary statistics of findings, optionally scoped to one connection."""
    return storage.findings_summary(connection_id=connection_id)
