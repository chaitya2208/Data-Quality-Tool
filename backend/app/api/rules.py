from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import Optional
from collections import defaultdict
from datetime import datetime
from app.core.database import get_db
from app.models.rule import Rule, RuleCategory, RuleSeverity, RuleStatus
from app.schemas.rule import RuleCreate, RuleUpdate, RuleResponse, RuleListResponse
from app.services.rule_engine import initialize_default_rules
from pydantic import BaseModel

router = APIRouter()

MEANINGFUL_FIELDS = {"name", "description", "category", "severity",
                     "applies_to", "rule_config", "owner"}


# ── Stats ─────────────────────────────────────────────────────────────────────

@router.get("/stats")
def get_rule_stats(db: Session = Depends(get_db)):
    """Aggregate rule counts by category and severity."""
    rules = db.query(Rule).all()

    by_category: dict = defaultdict(int)
    by_severity: dict = defaultdict(int)
    by_status:   dict = defaultdict(int)
    active = 0

    for r in rules:
        by_category[r.category.value if hasattr(r.category, 'value') else str(r.category)] += 1
        by_severity[r.severity.value if hasattr(r.severity, 'value') else str(r.severity)] += 1
        by_status[r.status.value if hasattr(r.status, 'value') else str(r.status or 'active')] += 1
        if r.is_active:
            active += 1

    return {
        "total":       len(rules),
        "active":      active,
        "pending":     by_status.get("pending", 0),
        "by_category": dict(by_category),
        "by_severity": dict(by_severity),
        "by_status":   dict(by_status),
    }


# ── List ──────────────────────────────────────────────────────────────────────

@router.get("", response_model=RuleListResponse)
def list_rules(
    category:  Optional[RuleCategory] = None,
    severity:  Optional[RuleSeverity] = None,
    status:    Optional[RuleStatus]   = None,
    is_active: Optional[bool]         = None,
    skip: int = 0,
    limit: int = 500,
    db: Session = Depends(get_db),
):
    query = db.query(Rule)
    if category:  query = query.filter(Rule.category  == category)
    if severity:  query = query.filter(Rule.severity  == severity)
    if status:    query = query.filter(Rule.status    == status)
    if is_active is not None:
        query = query.filter(Rule.is_active == is_active)

    total = query.count()
    rules = query.offset(skip).limit(limit).all()
    return RuleListResponse(total=total, rules=rules)


# ── Get ───────────────────────────────────────────────────────────────────────

@router.get("/{rule_id}", response_model=RuleResponse)
def get_rule(rule_id: str, db: Session = Depends(get_db)):
    rule = db.query(Rule).filter(Rule.id == rule_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    return rule


# ── Create (new rules go PENDING) ────────────────────────────────────────────

@router.post("", response_model=RuleResponse, status_code=201)
def create_rule(rule_data: RuleCreate, db: Session = Depends(get_db)):
    existing = db.query(Rule).filter(Rule.code == rule_data.code).first()
    if existing:
        raise HTTPException(status_code=400,
                            detail=f"Rule with code '{rule_data.code}' already exists")

    data = rule_data.model_dump()
    # New user-created rules start PENDING and inactive until approved
    data["status"]    = RuleStatus.PENDING
    data["is_active"] = False
    data["version"]   = 1

    rule = Rule(**data)
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


# ── Update ────────────────────────────────────────────────────────────────────

@router.patch("/{rule_id}", response_model=RuleResponse)
def update_rule(rule_id: str, update_data: RuleUpdate, db: Session = Depends(get_db)):
    rule = db.query(Rule).filter(Rule.id == rule_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")

    update_dict = update_data.model_dump(exclude_unset=True)

    # Sync is_active with status if status is being changed
    if "status" in update_dict:
        new_status = update_dict["status"]
        if new_status == RuleStatus.ACTIVE:
            update_dict["is_active"] = True
        elif new_status in (RuleStatus.DISABLED, RuleStatus.REJECTED, RuleStatus.PENDING):
            update_dict["is_active"] = False

    # Plain is_active toggle (from Rules page toggle switch)
    if "is_active" in update_dict and "status" not in update_dict:
        update_dict["status"] = (
            RuleStatus.ACTIVE if update_dict["is_active"] else RuleStatus.DISABLED
        )

    # Bump version on meaningful changes
    if MEANINGFUL_FIELDS & update_dict.keys():
        update_dict["version"] = (rule.version or 1) + 1

    for field, value in update_dict.items():
        setattr(rule, field, value)

    db.commit()
    db.refresh(rule)
    return rule


# ── Approve ───────────────────────────────────────────────────────────────────

@router.post("/{rule_id}/approve", response_model=RuleResponse)
def approve_rule(rule_id: str, db: Session = Depends(get_db)):
    """Approve a pending rule — makes it active and starts running on scans."""
    rule = db.query(Rule).filter(Rule.id == rule_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    if rule.status != RuleStatus.PENDING:
        raise HTTPException(status_code=400,
                            detail=f"Only PENDING rules can be approved (current: {rule.status})")

    rule.status      = RuleStatus.ACTIVE
    rule.is_active   = True
    rule.approved_at = datetime.utcnow()
    db.commit()
    db.refresh(rule)
    return rule


# ── Reject ────────────────────────────────────────────────────────────────────

class RejectRequest(BaseModel):
    reason: str

@router.post("/{rule_id}/reject", response_model=RuleResponse)
def reject_rule(rule_id: str, body: RejectRequest, db: Session = Depends(get_db)):
    """Reject a pending rule with a reason."""
    rule = db.query(Rule).filter(Rule.id == rule_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    if rule.status != RuleStatus.PENDING:
        raise HTTPException(status_code=400,
                            detail=f"Only PENDING rules can be rejected (current: {rule.status})")

    rule.status           = RuleStatus.REJECTED
    rule.is_active        = False
    rule.rejection_reason = body.reason
    rule.rejected_at      = datetime.utcnow()
    db.commit()
    db.refresh(rule)
    return rule


# ── Initialize defaults ───────────────────────────────────────────────────────

@router.post("/initialize")
def initialize_rules(db: Session = Depends(get_db)):
    try:
        initialize_default_rules(db)
        return {"message": "Default rules initialized successfully"}
    except Exception as e:
        raise HTTPException(status_code=500,
                            detail=f"Failed to initialize rules: {str(e)}")
