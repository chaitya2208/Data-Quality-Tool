from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import Optional, List
from app.core.database import get_db
from app.models.finding import Finding, FindingStatus
from app.models.asset import Asset
from app.schemas.finding import FindingResponse, FindingListResponse, FindingUpdate
from datetime import datetime
from collections import defaultdict

router = APIRouter()


@router.get("", response_model=FindingListResponse)
def list_findings(
    asset_id: Optional[str] = None,
    scan_id: Optional[str] = None,
    status: Optional[FindingStatus] = None,
    severity: Optional[str] = None,
    skip: int = 0,
    limit: int = 5000,
    db: Session = Depends(get_db)
):
    """List all findings with optional filters"""
    query = db.query(Finding)

    if asset_id:
        query = query.filter(Finding.asset_id == asset_id)
    if scan_id:
        query = query.filter(Finding.scan_id == scan_id)
    if status:
        query = query.filter(Finding.status == status)
    if severity:
        query = query.filter(Finding.severity == severity)

    # Order by most recent first
    query = query.order_by(Finding.detected_at.desc())

    total = query.count()
    findings = query.offset(skip).limit(limit).all()

    return FindingListResponse(total=total, findings=findings)


@router.get("/{finding_id}", response_model=FindingResponse)
def get_finding(finding_id: str, db: Session = Depends(get_db)):
    """Get a specific finding by ID"""
    finding = db.query(Finding).filter(Finding.id == finding_id).first()
    if not finding:
        raise HTTPException(status_code=404, detail="Finding not found")
    return finding


@router.patch("/{finding_id}", response_model=FindingResponse)
def update_finding(
    finding_id: str,
    update_data: FindingUpdate,
    db: Session = Depends(get_db)
):
    """Update a finding (status, assignment, resolution notes)"""
    finding = db.query(Finding).filter(Finding.id == finding_id).first()
    if not finding:
        raise HTTPException(status_code=404, detail="Finding not found")

    # Update fields
    if update_data.status is not None:
        finding.status = update_data.status

        # Update timestamps based on status change
        if update_data.status == FindingStatus.VALIDATED and not finding.validated_at:
            finding.validated_at = datetime.utcnow()
        elif update_data.status == FindingStatus.RESOLVED and not finding.resolved_at:
            finding.resolved_at = datetime.utcnow()
        elif update_data.status == FindingStatus.CLOSED and not finding.closed_at:
            finding.closed_at = datetime.utcnow()

    if update_data.assigned_to is not None:
        finding.assigned_to = update_data.assigned_to

    if update_data.resolution_notes is not None:
        finding.resolution_notes = update_data.resolution_notes

    finding.updated_at = datetime.utcnow()

    db.commit()
    db.refresh(finding)

    return finding


@router.get("/stats/by-database")
def get_findings_by_database(db: Session = Depends(get_db)):
    """
    Aggregate findings by database → schema → table.
    Joins findings with assets to get proper database/schema/table names.
    Used for the dashboard chart.
    """
    # Join findings with their assets to get database/schema/table
    rows = (
        db.query(Finding, Asset)
        .join(Asset, Finding.asset_id == Asset.id)
        .filter(Finding.status.notin_([FindingStatus.RESOLVED, FindingStatus.CLOSED]))
        .all()
    )

    # Build nested structure: database → tables with counts
    db_map: dict = defaultdict(lambda: defaultdict(lambda: {"total": 0, "by_severity": defaultdict(int)}))

    for finding, asset in rows:
        db_name    = asset.database_name or "Unknown"
        table_name = asset.table_name or asset.fqn or "Unknown"
        db_map[db_name][table_name]["total"] += 1
        db_map[db_name][table_name]["by_severity"][finding.severity] += 1

    # Shape into list sorted by total descending
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
def get_findings_summary(db: Session = Depends(get_db)):
    """Get summary statistics of findings"""
    total = db.query(Finding).count()

    by_status = {}
    for status in FindingStatus:
        count = db.query(Finding).filter(Finding.status == status).count()
        by_status[status.value] = count

    by_severity = {}
    for severity in ["critical", "high", "medium", "low", "info"]:
        count = db.query(Finding).filter(Finding.severity == severity).count()
        by_severity[severity] = count

    return {
        "total": total,
        "by_status": by_status,
        "by_severity": by_severity,
    }
