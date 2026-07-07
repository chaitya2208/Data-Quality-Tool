from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import Optional, List, Dict, Any
from collections import defaultdict
from datetime import datetime
import json, re, logging
from app.core.database import get_db
from app.models.rule import Rule, RuleCategory, RuleSeverity, RuleStatus
from app.schemas.rule import RuleCreate, RuleUpdate, RuleResponse, RuleListResponse
from app.services.rule_engine import initialize_default_rules
from app.services.claude_client import ask_claude
from pydantic import BaseModel

logger = logging.getLogger(__name__)

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


# ── Generate rule from natural language prompt ───────────────────────────────

class GenerateRuleRequest(BaseModel):
    prompt: str
    owner: str = ""


class GeneratedRule(BaseModel):
    code: str
    name: str
    description: str
    category: str
    severity: str
    applies_to: List[str]
    rationale: str


_GENERATE_SYSTEM = """You are a Snowflake data quality expert.
Convert a user's plain-English requirement into a precisely structured data quality rule.

Respond with valid JSON only — no markdown, no prose outside the JSON.
Use this exact schema:
{
  "code": "UPPER_SNAKE_CASE unique identifier (max 50 chars)",
  "name": "Short human-readable name (max 60 chars)",
  "description": "Clear description of what this rule checks and why it matters (1-3 sentences)",
  "category": "one of: data_quality | schema | naming | security | ownership | documentation | performance",
  "severity": "one of: critical | high | medium | low | info",
  "applies_to": ["table"] or ["column"] or ["table", "column"],
  "rationale": "Why this rule matters for data quality (1-2 sentences)"
}

Rules for code generation:
- Use UPPER_SNAKE_CASE (e.g. NO_NULL_CUSTOMER_ID, STATUS_VALID_VALUES)
- Make it specific and descriptive, not generic
- Avoid duplicating common rules like MISSING_TABLE_COMMENT, MISSING_TABLE_OWNER
"""


@router.post("/generate", response_model=GeneratedRule)
def generate_rule_from_prompt(
    request: GenerateRuleRequest,
    db: Session = Depends(get_db),
):
    """
    Convert a plain-English requirement into a structured rule definition using Claude.
    The returned rule is NOT saved — client edits it and calls POST / to create.
    """
    if not request.prompt.strip():
        raise HTTPException(status_code=400, detail="Prompt cannot be empty")

    # Include a sample of existing rule codes so Claude avoids duplicates
    existing_codes = [
        r.code for r in db.query(Rule.code).limit(100).all()
    ]
    existing_sample = ", ".join(existing_codes[:30])

    user_prompt = f"""User requirement: {request.prompt}

Existing rule codes to avoid duplicating: {existing_sample}
{f'Owner: {request.owner}' if request.owner else ''}

Convert this requirement into a data quality rule. Respond with JSON only."""

    try:
        raw = ask_claude(user_prompt, system=_GENERATE_SYSTEM, max_tokens=1024)
    except Exception as e:
        logger.error(f"[RuleGenerate] Claude call failed: {e}")
        raise HTTPException(status_code=503, detail=f"AI generation failed: {str(e)}")

    # Extract JSON
    parsed = None
    for pattern in [r"```(?:json)?\s*(\{.*?\})\s*```", r"\{.*\}"]:
        match = re.search(pattern, raw, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(1) if "```" in pattern else match.group(0))
                break
            except Exception:
                continue

    if not parsed or not parsed.get("code") or not parsed.get("name"):
        logger.error(f"[RuleGenerate] Bad response from Claude: {raw[:300]}")
        raise HTTPException(status_code=500, detail="AI returned an invalid rule structure. Try rephrasing your requirement.")

    # Normalise
    parsed["code"] = re.sub(r"[^A-Z0-9_]", "_", parsed.get("code", "").upper().strip())[:50]
    valid_cats = {c.value for c in RuleCategory}
    valid_sevs = {s.value for s in RuleSeverity}
    if parsed.get("category") not in valid_cats:
        parsed["category"] = "data_quality"
    if parsed.get("severity") not in valid_sevs:
        parsed["severity"] = "medium"
    if not isinstance(parsed.get("applies_to"), list):
        parsed["applies_to"] = ["table"]

    return GeneratedRule(**parsed)


# ── Initialize defaults ───────────────────────────────────────────────────────

@router.post("/initialize")
def initialize_rules(db: Session = Depends(get_db)):
    try:
        initialize_default_rules(db)
        return {"message": "Default rules initialized successfully"}
    except Exception as e:
        raise HTTPException(status_code=500,
                            detail=f"Failed to initialize rules: {str(e)}")
