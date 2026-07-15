from fastapi import APIRouter, HTTPException
from typing import Optional, List, Dict, Any
from collections import defaultdict
from datetime import datetime
import json, re, logging
from app.services import storage
from app.schemas.rule import (
    RuleCreate, RuleUpdate, RuleResponse, RuleListResponse,
    RuleDefinitionResponse, RuleDefinitionListResponse,
    RuleInstanceResponse, RuleInstanceListResponse,
    RuleExecutionResponse, RuleExecutionListResponse,
)
from app.services.rule_engine import initialize_default_rules
from app.services.snowflake_session import session as sf_session
from app.services.claude_client import ask_claude
from app.services.text_similarity import word_overlap_score, DEFAULT_SIMILARITY_THRESHOLD
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter()

MEANINGFUL_FIELDS = {"name", "description", "category", "severity",
                     "applies_to", "rule_config", "owner"}


# ── Stats ─────────────────────────────────────────────────────────────────────

@router.get("/stats")
def get_rule_stats():
    """Aggregate rule counts by category and severity (view over RULE_INSTANCES)."""
    _, rules = storage.list_rules_view(limit=5000)

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
        "pending":     by_status.get("pending", 0),
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
    total, rules = storage.list_rules_view(
        category=category, severity=severity, status=status,
        is_active=is_active, skip=skip, limit=limit,
    )
    return RuleListResponse(total=total, rules=rules)


# ── Rule library: definitions / instances / executions ──────────────────────
# Registered before /{rule_id} so "/definitions" isn't swallowed as a rule_id.

@router.get("/definitions", response_model=RuleDefinitionListResponse)
def list_rule_definitions(
    status: Optional[str] = None,
    category: Optional[str] = None,
    check_kind: Optional[str] = None,
    skip: int = 0,
    limit: int = 500,
):
    total, definitions = storage.list_definitions(
        status=status, category=category, skip=0, limit=5000,
    )
    if check_kind:
        definitions = [d for d in definitions if d.check_kind == check_kind]
        total = len(definitions)

    # RULE_DEFINITIONS.INSTANCE_COUNT only increments, never decrements, so
    # it drifts from reality if a row is ever removed outside the normal
    # app flow — recompute live rather than show a stale number.
    real_counts = storage.get_real_instance_counts()
    for d in definitions:
        d.instance_count = real_counts.get(d.id, 0)
    definitions.sort(key=lambda d: d.instance_count, reverse=True)

    return RuleDefinitionListResponse(total=total, definitions=definitions[skip:skip + limit])


@router.get("/definitions/{definition_id}", response_model=RuleDefinitionResponse)
def get_rule_definition(definition_id: str):
    definition = storage.get_definition(definition_id)
    if not definition:
        raise HTTPException(status_code=404, detail="Rule definition not found")
    definition.instance_count = storage.get_real_instance_counts().get(definition_id, 0)
    return definition


class ToggleDefinitionRequest(BaseModel):
    is_active: bool


@router.patch("/definitions/{definition_id}", response_model=RuleDefinitionResponse)
def toggle_rule_definition(definition_id: str, body: ToggleDefinitionRequest):
    """Enable/disable a check concept — gates every instance under this
    definition at execution time (see RuleEngine.get_active_instances),
    not just this one row. Only active/disabled definitions are toggleable;
    a proposed definition must be approved via Agent Workflow review first."""
    definition = storage.get_definition(definition_id)
    if not definition:
        raise HTTPException(status_code=404, detail="Rule definition not found")
    if definition.status not in ("active", "disabled"):
        raise HTTPException(status_code=400,
                            detail=f"Only active/disabled definitions can be toggled (current: {definition.status})")
    new_status = "active" if body.is_active else "disabled"
    storage.update_definition(definition_id, status=new_status)
    definition = storage.get_definition(definition_id)
    definition.instance_count = storage.get_real_instance_counts().get(definition_id, 0)
    return definition


@router.get("/definitions/{definition_id}/instances", response_model=RuleInstanceListResponse)
def list_definition_instances(
    definition_id: str,
    status: Optional[str] = None,
    skip: int = 0,
    limit: int = 500,
):
    total, instances = storage.list_instances(
        definition_id=definition_id, status=status, skip=skip, limit=limit,
    )
    return RuleInstanceListResponse(total=total, instances=instances)


@router.get("/instances/{instance_id}/executions", response_model=RuleExecutionListResponse)
def list_instance_executions(instance_id: str, limit: int = 20):
    executions = storage.list_executions_for_instance(instance_id, limit=limit)
    return RuleExecutionListResponse(total=len(executions), executions=executions)


# ── Get ───────────────────────────────────────────────────────────────────────

@router.get("/{rule_id}", response_model=RuleResponse)
def get_rule(rule_id: str):
    rule = storage.get_rule_view(rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    return rule


# ── Create (new rules go PENDING) ────────────────────────────────────────────

@router.post("", response_model=RuleResponse, status_code=201)
def create_rule(rule_data: RuleCreate):
    existing = storage.get_definition_by_handler_key(rule_data.code.lower())
    if existing:
        raise HTTPException(status_code=400,
                            detail=f"Rule with code '{rule_data.code}' already exists")

    # RuleCategory/RuleSeverity are (str, Enum); the Snowflake connector rejects
    # binding the enum member itself ("Binding data in type (rulecategory) is not
    # supported"), so coerce to the plain string value before it reaches storage.
    category_value = rule_data.category.value if hasattr(rule_data.category, "value") else str(rule_data.category)
    severity_value = rule_data.severity.value if hasattr(rule_data.severity, "value") else str(rule_data.severity)

    definition = storage.create_definition(
        name=rule_data.name,
        category=category_value,
        description=rule_data.description,
        check_kind="python_handler",
        handler_key=rule_data.code.lower(),
        default_severity=severity_value,
        allowed_scopes=rule_data.applies_to,
        source="user",
        status="proposed",
        owner=rule_data.owner,
        created_by=rule_data.created_by,
    )
    scope = "table" if "table" in rule_data.applies_to else "column"
    fingerprint = _global_fingerprint(definition.id)
    instance = storage.create_instance(
        definition_id=definition.id,
        scope=scope,
        database_name="*",
        fingerprint=fingerprint,
        severity=severity_value,
        target_config={},
        status="pending",
        is_active=False,
        jira_ticket=rule_data.jira_ticket,
        owner=rule_data.owner,
        created_by=rule_data.created_by,
    )
    return storage.get_rule_view(instance.id)


def _global_fingerprint(definition_id: str) -> str:
    import hashlib
    return hashlib.sha256(f"{definition_id}|global".encode("utf-8")).hexdigest()


# ── Update ────────────────────────────────────────────────────────────────────

@router.patch("/{rule_id}", response_model=RuleResponse)
def update_rule(rule_id: str, update_data: RuleUpdate):
    instance = storage.get_instance(rule_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Rule not found")

    update_dict = update_data.model_dump(exclude_unset=True)

    definition_fields = {}
    for f in ("name", "description", "category"):
        if f in update_dict:
            definition_fields[f] = update_dict.pop(f)
    if "applies_to" in update_dict:
        update_dict.pop("applies_to")  # allowed_scopes lives on the definition, rarely edited post-creation
    if "rule_config" in update_dict:
        update_dict.pop("rule_config")  # derived field on the rule view, not stored directly on the instance

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

    if definition_fields:
        storage.update_definition(instance.definition_id, **definition_fields)

    if update_dict:
        # Bump version on meaningful changes
        if MEANINGFUL_FIELDS & (update_dict.keys() | definition_fields.keys()):
            update_dict["version"] = (instance.version or 1) + 1
        storage.update_instance(rule_id, **update_dict)
    elif definition_fields and (MEANINGFUL_FIELDS & definition_fields.keys()):
        storage.update_instance(rule_id, version=(instance.version or 1) + 1)

    return storage.get_rule_view(rule_id)


# ── Approve ───────────────────────────────────────────────────────────────────

@router.post("/{rule_id}/approve", response_model=RuleResponse)
def approve_rule(rule_id: str):
    """Approve a pending rule instance — makes it active and starts running on scans."""
    instance = storage.get_instance(rule_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Rule not found")
    if instance.status != "pending":
        raise HTTPException(status_code=400,
                            detail=f"Only PENDING rules can be approved (current: {instance.status})")

    approved_by = (sf_session.get_cached_context() or {}).get("user")
    storage.approve_instance(rule_id, approved_by=approved_by)
    definition = storage.get_definition(instance.definition_id)
    if definition and definition.status == "proposed":
        storage.update_definition(definition.id, status="active")
    return storage.get_rule_view(rule_id)


# ── Reject ────────────────────────────────────────────────────────────────────

class RejectRequest(BaseModel):
    reason: str

@router.post("/{rule_id}/reject", response_model=RuleResponse)
def reject_rule(rule_id: str, body: RejectRequest):
    """Reject a pending rule instance with a reason."""
    instance = storage.get_instance(rule_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Rule not found")
    if instance.status != "pending":
        raise HTTPException(status_code=400,
                            detail=f"Only PENDING rules can be rejected (current: {instance.status})")

    rejected_by = (sf_session.get_cached_context() or {}).get("user")
    storage.reject_instance(rule_id, body.reason, rejected_by=rejected_by)
    return storage.get_rule_view(rule_id)


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


def _find_similar_rule(name: str, description: str, threshold: float = DEFAULT_SIMILARITY_THRESHOLD):
    """
    Return the most similar existing rule if overlap score ≥ threshold.
    Checks against all existing rule names and descriptions. Uses the shared
    word_overlap_score (app.services.text_similarity) so this duplicate-catch
    gate and the Rule Intelligence Agent's definition-dedup gate stay in sync.
    """
    combined = f"{name} {description}"
    best_score = 0.0
    best_rule = None
    _, rules = storage.list_rules_view(limit=5000)
    for rule in rules:
        rule_text = f"{rule.name} {rule.description or ''}"
        score = word_overlap_score(combined, rule_text)
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
    _, existing_rules = storage.list_rules_view(limit=5000)
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

    # 1. Exact code match (code == HANDLER_KEY upper-cased)
    exact_def = storage.get_definition_by_handler_key(parsed["code"].lower())
    if exact_def:
        parsed["duplicate_of"] = {"code": parsed["code"], "name": exact_def.name}
        logger.info(f"[RuleGenerate] Exact code duplicate detected: {parsed['code']}")
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
