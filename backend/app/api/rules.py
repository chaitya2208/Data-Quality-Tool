from fastapi import APIRouter, HTTPException
from typing import Optional, List, Dict, Any
from collections import defaultdict
from datetime import datetime
import json, re, logging
from app.services import storage
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
def get_rule_stats():
    """Aggregate rule counts by category and severity."""
    rules = storage.list_all_rules()

    by_category: dict = defaultdict(int)
    by_severity: dict = defaultdict(int)
    by_status:   dict = defaultdict(int)
    active = 0

    for r in rules:
        by_category[r.category] += 1
        by_severity[r.severity] += 1
        by_status[r.status or 'active'] += 1
        if r.is_active:
            active += 1

    return {
        "total":       len(rules),
        "active":      active,
        "pending":     by_status.get("pending", 0) + by_status.get("pending", 0),
        "by_category": dict(by_category),
        "by_severity": dict(by_severity),
        "by_status":   dict(by_status),
    }


# ── List ──────────────────────────────────────────────────────────────────────

@router.get("", response_model=RuleListResponse)
def list_rules(
    category:  Optional[str] = None,
    severity:  Optional[str] = None,
    status:    Optional[str] = None,
    is_active: Optional[bool] = None,
    skip: int = 0,
    limit: int = 500,
):
    total, rules = storage.list_rules(
        category=category, severity=severity, status=status,
        is_active=is_active, skip=skip, limit=limit,
    )
    return RuleListResponse(total=total, rules=rules)


# ── Get ───────────────────────────────────────────────────────────────────────

@router.get("/{rule_id}", response_model=RuleResponse)
def get_rule(rule_id: str):
    rule = storage.get_rule(rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    return rule


# ── Create (new rules go PENDING) ────────────────────────────────────────────

@router.post("", response_model=RuleResponse, status_code=201)
def create_rule(rule_data: RuleCreate):
    existing = storage.get_rule_by_code(rule_data.code)
    if existing:
        raise HTTPException(status_code=400,
                            detail=f"Rule with code '{rule_data.code}' already exists")

    data = rule_data.model_dump()
    # New user-created rules start PENDING and inactive until approved
    data["status"]    = "pending"
    data["is_active"] = False
    data["version"]   = 1

    return storage.create_rule(**data)


# ── Update ────────────────────────────────────────────────────────────────────

@router.patch("/{rule_id}", response_model=RuleResponse)
def update_rule(rule_id: str, update_data: RuleUpdate):
    rule = storage.get_rule(rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")

    update_dict = update_data.model_dump(exclude_unset=True)

    # Sync is_active with status if status is being changed
    if "status" in update_dict:
        new_status = update_dict["status"]
        if new_status == "active":
            update_dict["is_active"] = True
        elif new_status in ("disabled", "rejected", "pending"):
            update_dict["is_active"] = False

    # Plain is_active toggle (from Rules page toggle switch)
    if "is_active" in update_dict and "status" not in update_dict:
        update_dict["status"] = "active" if update_dict["is_active"] else "disabled"

    # Bump version on meaningful changes
    if MEANINGFUL_FIELDS & update_dict.keys():
        update_dict["version"] = (rule.version or 1) + 1

    return storage.update_rule(rule_id, **update_dict)


# ── Approve ───────────────────────────────────────────────────────────────────

@router.post("/{rule_id}/approve", response_model=RuleResponse)
def approve_rule(rule_id: str):
    """Approve a pending rule — makes it active and starts running on scans."""
    rule = storage.get_rule(rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    if rule.status != "pending":
        raise HTTPException(status_code=400,
                            detail=f"Only PENDING rules can be approved (current: {rule.status})")

    return storage.update_rule(
        rule_id, status="active", is_active=True, approved_at=datetime.utcnow(),
    )


# ── Reject ────────────────────────────────────────────────────────────────────

class RejectRequest(BaseModel):
    reason: str

@router.post("/{rule_id}/reject", response_model=RuleResponse)
def reject_rule(rule_id: str, body: RejectRequest):
    """Reject a pending rule with a reason."""
    rule = storage.get_rule(rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    if rule.status != "pending":
        raise HTTPException(status_code=400,
                            detail=f"Only PENDING rules can be rejected (current: {rule.status})")

    return storage.update_rule(
        rule_id, status="rejected", is_active=False,
        rejection_reason=body.reason, rejected_at=datetime.utcnow(),
    )


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
    duplicate_of: Optional[Dict[str, str]] = None  # {code, name} if similar rule detected


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
- The existing_rules list below shows rules that ALREADY EXIST — do NOT generate a rule
  that checks the same thing even if described differently or in another language
"""


def _word_overlap_score(text1: str, text2: str) -> float:
    """
    Simple word-overlap similarity between two strings (0.0 – 1.0).
    Used to catch semantic duplicates regardless of language/phrasing.
    """
    stop = {"a","an","the","is","are","was","were","be","been","being","have",
            "has","had","do","does","did","will","would","could","should","may",
            "might","must","shall","can","need","dare","ought","used","to","of",
            "in","for","on","with","at","by","from","as","into","through","this",
            "that","these","those","it","its","and","or","but","if","than","when",
            "where","which","who","how","all","each","every","both","rule","check",
            "column","table","snowflake","data","quality","should","not","no","any"}
    def words(t: str) -> set:
        return {w.lower() for w in re.findall(r"\w+", t) if w.lower() not in stop and len(w) > 2}
    w1, w2 = words(text1), words(text2)
    if not w1 or not w2:
        return 0.0
    return len(w1 & w2) / min(len(w1), len(w2))


def _find_similar_rule(name: str, description: str, threshold: float = 0.55):
    """
    Return the most similar existing rule if overlap score ≥ threshold.
    Checks against all existing rule names and descriptions.
    """
    combined = f"{name} {description}"
    best_score = 0.0
    best_rule = None
    for rule in storage.list_all_rules():
        rule_text = f"{rule.name} {rule.description or ''}"
        score = _word_overlap_score(combined, rule_text)
        if score > best_score:
            best_score = score
            best_rule = rule
    return best_rule if best_score >= threshold else None


@router.post("/generate", response_model=GeneratedRule)
def generate_rule_from_prompt(request: GenerateRuleRequest):
    """
    Convert a plain-English requirement into a structured rule definition using Claude.
    Returns duplicate_of if a semantically similar rule already exists.
    The returned rule is NOT saved — client edits it and calls POST / to create.
    """
    if not request.prompt.strip():
        raise HTTPException(status_code=400, detail="Prompt cannot be empty")

    # Give Claude the full name + description of all existing rules so it can
    # detect semantic duplicates regardless of language/phrasing
    existing_rules = storage.list_all_rules()
    existing_details = "\n".join(
        f"  - {r.code}: {r.name} — {(r.description or '')[:100]}"
        for r in existing_rules
    )

    user_prompt = f"""User requirement: {request.prompt}

EXISTING RULES (already implemented — do NOT duplicate these):
{existing_details or '  (none yet)'}

{f'Owner: {request.owner}' if request.owner else ''}

The user may have described this requirement in any language. Carefully check whether
the requirement is already covered by any existing rule above before generating a new one.

Convert this requirement into a data quality rule. Respond with JSON only."""

    try:
        raw = ask_claude(user_prompt, system=_GENERATE_SYSTEM, max_tokens=1024)
    except Exception as e:
        logger.error(f"[RuleGenerate] Claude call failed: {e}")
        raise HTTPException(status_code=503, detail=f"AI generation failed: {str(e)}")

    # Extract JSON
    parsed = None
    for pattern in [r"```(?:json)?\s*(\{.*\})\s*```", r"\{.*\}"]:
        match = re.search(pattern, raw, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(1) if "```" in pattern else match.group(0))
                if parsed.get("code") or parsed.get("name"):
                    break
            except Exception:
                continue

    if not parsed or not parsed.get("code") or not parsed.get("name"):
        logger.error(f"[RuleGenerate] Bad response from Claude: {raw[:300]}")
        raise HTTPException(status_code=500, detail="AI returned an invalid rule structure. Try rephrasing your requirement.")

    # Normalise
    parsed["code"] = re.sub(r"[^A-Z0-9_]", "_", parsed.get("code", "").upper().strip())[:50]
    valid_cats = {"naming", "documentation", "ownership", "schema", "data_quality", "security", "performance"}
    valid_sevs = {"critical", "high", "medium", "low", "info"}
    if parsed.get("category") not in valid_cats:
        parsed["category"] = "data_quality"
    if parsed.get("severity") not in valid_sevs:
        parsed["severity"] = "medium"
    if not isinstance(parsed.get("applies_to"), list):
        parsed["applies_to"] = ["table"]

    # ── Similarity check: catch semantic duplicates Claude may have missed ────
    parsed["duplicate_of"] = None

    # 1. Exact code match
    exact = storage.get_rule_by_code(parsed["code"])
    if exact:
        parsed["duplicate_of"] = {"code": exact.code, "name": exact.name}
        logger.info(f"[RuleGenerate] Exact code duplicate detected: {exact.code}")
    else:
        # 2. Word-overlap similarity check
        similar = _find_similar_rule(parsed["name"], parsed["description"])
        if similar:
            parsed["duplicate_of"] = {"code": similar.code, "name": similar.name}
            logger.info(
                f"[RuleGenerate] Semantic duplicate detected: '{parsed['name']}' "
                f"≈ existing '{similar.name}' ({similar.code})"
            )

    return GeneratedRule(**parsed)


# ── Initialize defaults ───────────────────────────────────────────────────────

@router.post("/initialize")
def initialize_rules():
    try:
        initialize_default_rules()
        return {"message": "Default rules initialized successfully"}
    except Exception as e:
        raise HTTPException(status_code=500,
                            detail=f"Failed to initialize rules: {str(e)}")
