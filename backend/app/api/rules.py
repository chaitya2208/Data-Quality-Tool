from fastapi import APIRouter, HTTPException
from typing import Optional, List, Dict
from collections import defaultdict
import json, re, logging
from app.services import storage
from app.schemas.rule import (
    RuleCreate, RuleUpdate, RuleResponse, RuleListResponse,
    RuleDefinitionResponse, RuleDefinitionListResponse,
    RuleInstanceListResponse,
    RuleExecutionListResponse,
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


@router.get("/coverage")
def get_rule_coverage(connection_id: Optional[str] = None):
    """Per-instance pass/fail breakdown for active instances.

    An instance is 'failing' if it has at least one open finding
    (open / reopened). It is 'passing' otherwise —
    including instances that previously had findings but have since been
    resolved.
    """
    _total, instances = storage.list_instances(status="active", limit=5000)
    if connection_id and not storage._is_snowflake_connection(connection_id):
        instances = [i for i in instances if getattr(i, "connection_id", None) == connection_id]
    if not instances:
        return {"active": 0, "passing": 0, "failing": 0, "never_run": 0}

    instance_ids = [i.id for i in instances]
    # One query: open finding count per instance
    placeholders = ", ".join(f"%(id_{n})s" for n in range(len(instance_ids)))
    params = {f"id_{n}": iid for n, iid in enumerate(instance_ids)}
    rows = sf_session.query(
        f"""
        SELECT INSTANCE_ID, COUNT(*) AS cnt
        FROM FINDINGS
        WHERE INSTANCE_ID IN ({placeholders})
          AND STATUS IN ('open', 'reopened')
        GROUP BY INSTANCE_ID
        """,
        params,
    )
    failing_ids = {r["INSTANCE_ID"] for r in rows}

    active = len(instances)
    failing = len(failing_ids)
    passing = active - failing

    return {
        "active":  active,
        "passing": passing,
        "failing": failing,
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
    """Enable/disable a check concept. This CASCADES to every RULE_INSTANCES
    row under this definition — flipping their IS_ACTIVE + STATUS in one
    atomic pass so subsequent scans immediately stop (or start) firing them.
    Only active/disabled definitions are toggleable; a proposed definition
    must be approved via Agent Workflow review first."""
    definition = storage.get_definition(definition_id)
    if not definition:
        raise HTTPException(status_code=404, detail="Rule definition not found")
    if definition.status not in ("active", "disabled"):
        raise HTTPException(status_code=400,
                            detail=f"Only active/disabled definitions can be toggled (current: {definition.status})")
    storage.set_definition_active_state(definition_id, body.is_active)
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


# ── Rule chat (Copilot-style side panel) ─────────────────────────────────────

class ChatMessage(BaseModel):
    role: str    # "user" | "assistant"
    content: str

class RuleChatRequest(BaseModel):
    messages: List[ChatMessage]
    session_id: Optional[str] = None   # when set, auto-save after generating response

class RuleChatResponse(BaseModel):
    message: str
    proposed_rule: Optional[GeneratedRule] = None
    is_ready: bool = False
    referenced_rules: List[Dict] = []

class ReferencedRule(BaseModel):
    definition_id: str
    code: str
    name: str
    category: str

# Chat session CRUD models
class ChatSessionResponse(BaseModel):
    id: str
    title: Optional[str]
    messages: List[dict]
    created_by: Optional[str]
    created_at: Optional[str]
    updated_at: Optional[str]

class ChatSessionListResponse(BaseModel):
    sessions: List[ChatSessionResponse]

class CreateSessionRequest(BaseModel):
    title: Optional[str] = None

class UpdateSessionRequest(BaseModel):
    messages: List[dict]
    title: Optional[str] = None


def _session_to_response(s) -> ChatSessionResponse:
    return ChatSessionResponse(
        id=s.id,
        title=s.title,
        messages=s.messages,
        created_by=s.created_by,
        created_at=str(s.created_at) if s.created_at else None,
        updated_at=str(s.updated_at) if s.updated_at else None,
    )


@router.get("/chat/sessions", response_model=ChatSessionListResponse)
def list_chat_sessions():
    user = (sf_session.get_cached_context() or {}).get("user")
    sessions = storage.list_rule_chats(created_by=user)
    return ChatSessionListResponse(sessions=[_session_to_response(s) for s in sessions])


@router.post("/chat/sessions", response_model=ChatSessionResponse, status_code=201)
def create_chat_session(req: CreateSessionRequest):
    user = (sf_session.get_cached_context() or {}).get("user")
    session = storage.create_rule_chat(title=req.title, messages=[], created_by=user)
    return _session_to_response(session)


@router.get("/chat/sessions/{session_id}", response_model=ChatSessionResponse)
def get_chat_session(session_id: str):
    session = storage.get_rule_chat(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Chat session not found")
    return _session_to_response(session)


@router.put("/chat/sessions/{session_id}", response_model=ChatSessionResponse)
def update_chat_session(session_id: str, req: UpdateSessionRequest):
    session = storage.get_rule_chat(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Chat session not found")
    updated = storage.update_rule_chat(session_id, messages=req.messages, title=req.title)
    return _session_to_response(updated)


@router.delete("/chat/sessions/{session_id}", status_code=204)
def delete_chat_session(session_id: str):
    session = storage.get_rule_chat(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Chat session not found")
    storage.delete_rule_chat(session_id)
    return None


def _build_rule_context_for_ai() -> str:
    """Build a rich rule context string for the AI system prompt."""
    try:
        _, rules = storage.list_rules_view(limit=5000)
        findings_by_def = storage.get_findings_count_per_definition()
        top_assets = storage.get_top_assets_per_definition(top_n=3)
        # Build a map from handler_key.upper() → definition_id for ref resolution
        # (rules in list_rules_view carry rule_config.definition_id)
        lines = []
        seen_def_ids: set = set()
        for r in rules:
            def_id = (r.rule_config or {}).get("definition_id", "")
            if def_id in seen_def_ids:
                continue
            seen_def_ids.add(def_id)
            finding_cnt = findings_by_def.get(def_id, 0)
            assets = top_assets.get(def_id, [])
            asset_str = ", ".join(f"{fqn} ({cnt})" for fqn, cnt in assets) if assets else "none"
            lines.append(
                f"- {r.code} | {r.category} | severity:{r.severity} | {finding_cnt} findings"
                f"\n  Assets: {asset_str}"
                f"\n  Desc: {(r.description or '')[:120]}"
                f"\n  def_id: {def_id}"
            )
        return "\n".join(lines) or "  (none yet)"
    except Exception as e:
        logger.warning(f"[RuleChat] Could not build rule context: {e}")
        return "  (unavailable)"


def _extract_rule_refs(text: str, rules_by_code: dict) -> tuple:
    """
    Find all {\"ref\": \"CODE\", ...} blocks in text, resolve them to ReferencedRule objects,
    and return (clean_text, referenced_rules_list).
    """
    ref_pattern = re.compile(r'\{"ref"\s*:\s*"([^"]+)"[^}]*\}')
    refs = []
    seen_codes: set = set()
    for match in ref_pattern.finditer(text):
        code = match.group(1).upper()
        if code in seen_codes:
            continue
        seen_codes.add(code)
        rule = rules_by_code.get(code)
        if rule:
            def_id = (rule.rule_config or {}).get("definition_id", "")
            refs.append({
                "definition_id": def_id,
                "code": code,
                "name": rule.name,
                "category": rule.category,
            })
    clean = ref_pattern.sub("", text).strip()
    return clean, refs


_CHAT_SYSTEM = """You are a Snowflake data quality rule expert helping a user define a new data quality rule via conversation.

RESPONSE STYLE (strictly follow these):
• Use short bullet points (•) — NOT paragraphs. Max 3-4 bullets per turn.
• Ask exactly ONE clarifying question per turn, not multiple.
• Keep each message under 120 words total.
• Be direct and specific — no filler phrases.

When mentioning an existing rule, output a reference tag exactly like this (the UI renders it as a clickable card):
{"ref": "RULE_CODE", "name": "Rule Name", "definition_id": "..."}

ALWAYS return valid JSON with this exact shape (no markdown, no prose outside JSON):
{
  "message": "Your reply with bullet points and any ref tags inline",
  "is_ready": false,
  "proposed_rule": null
}

When you have enough information, set is_ready=true and populate proposed_rule:
{
  "message": "Here is the rule I've drafted:",
  "is_ready": true,
  "proposed_rule": {
    "code": "UPPER_SNAKE_CASE (max 50 chars)",
    "name": "Short name (max 60 chars)",
    "description": "1-3 sentence description",
    "category": "data_quality|schema|naming|security|ownership|documentation|performance",
    "severity": "critical|high|medium|low|info",
    "applies_to": ["table"] or ["column"] or ["table","column"],
    "rationale": "1-2 sentence rationale",
    "duplicate_of": null
  }
}

Rules for code: UPPER_SNAKE_CASE, max 50 chars, specific (e.g. NO_NULL_CUSTOMER_ID).
Do NOT propose a rule that duplicates an existing one — reference it with a ref tag instead.
"""


@router.post("/chat", response_model=RuleChatResponse)
def rule_chat(request: RuleChatRequest):
    """
    Conversational rule creation. Accepts a full message history, returns the next
    assistant turn. When Claude is confident it sets is_ready=True with proposed_rule.
    If session_id is provided, auto-saves the updated conversation to RULE_CHATS.
    """
    if not request.messages:
        raise HTTPException(status_code=400, detail="messages cannot be empty")

    rule_context = _build_rule_context_for_ai()
    system = _CHAT_SYSTEM + f"\n\nEXISTING RULES (with live findings data — do NOT duplicate):\n{rule_context}"

    _, existing_rules = storage.list_rules_view(limit=5000)
    rules_by_code = {r.code.upper(): r for r in existing_rules}

    messages_payload = [{"role": m.role, "content": m.content} for m in request.messages]
    transcript = "\n".join(
        f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['content']}"
        for m in messages_payload
    )
    prompt = f"Conversation so far:\n{transcript}\n\nNow respond as the assistant."

    try:
        raw = ask_claude(prompt, system=system, max_tokens=1024)
    except Exception as e:
        logger.error(f"[RuleChat] Claude call failed: {e}")
        raise HTTPException(status_code=503, detail=f"AI chat failed: {str(e)}")

    parsed = None
    for pattern in [r"```(?:json)?\s*(\{.*?\})\s*```", r"\{[\s\S]*\}"]:
        match = re.search(pattern, raw, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(1) if "```" in pattern else match.group(0))
                if "message" in parsed:
                    break
            except Exception:
                continue

    if not parsed or "message" not in parsed:
        logger.error(f"[RuleChat] Bad response: {raw[:300]}")
        return RuleChatResponse(message="I had trouble formatting my response. Could you rephrase?")

    clean_message, refs = _extract_rule_refs(parsed["message"], rules_by_code)

    proposed = None
    if parsed.get("is_ready") and parsed.get("proposed_rule"):
        pr = parsed["proposed_rule"]
        pr["code"] = re.sub(r"[^A-Z0-9_]", "_", (pr.get("code") or "").upper().strip())[:50]
        valid_cats = {"naming", "documentation", "ownership", "schema", "data_quality", "security", "performance"}
        valid_sevs = {"critical", "high", "medium", "low", "info"}
        if pr.get("category") not in valid_cats:
            pr["category"] = "data_quality"
        if pr.get("severity") not in valid_sevs:
            pr["severity"] = "medium"
        if not isinstance(pr.get("applies_to"), list):
            pr["applies_to"] = ["table"]
        pr.setdefault("rationale", "")
        pr["duplicate_of"] = None
        exact_def = storage.get_definition_by_handler_key(pr["code"].lower())
        if exact_def:
            pr["duplicate_of"] = {"code": pr["code"], "name": exact_def.name}
        else:
            similar = _find_similar_rule(pr.get("name", ""), pr.get("description", ""))
            if similar:
                pr["duplicate_of"] = {"code": similar.code, "name": similar.name}
        proposed = GeneratedRule(**pr)

    response = RuleChatResponse(
        message=clean_message,
        proposed_rule=proposed,
        is_ready=bool(parsed.get("is_ready")) and proposed is not None,
        referenced_rules=refs,
    )

    # Auto-save if a session_id was provided
    if request.session_id:
        try:
            session = storage.get_rule_chat(request.session_id)
            if session is not None:
                import json as _json
                existing_msgs = list(session.messages)
                # Append the new user message(s) not already in DB + the assistant reply
                # (client sends full history; just save it wholesale)
                new_msgs = [{"role": m.role, "content": m.content} for m in request.messages]
                new_msgs.append({
                    "role": "assistant",
                    "content": clean_message,
                    "referenced_rules": refs,
                    "proposed_rule": proposed.dict() if proposed else None,
                })
                title = session.title
                if not title and new_msgs:
                    first_user = next((m["content"] for m in new_msgs if m["role"] == "user"), None)
                    if first_user:
                        title = first_user[:60]
                storage.update_rule_chat(request.session_id, messages=new_msgs, title=title)
        except Exception as save_err:
            logger.warning(f"[RuleChat] Auto-save failed: {save_err}")

    return response


# ── Initialize defaults ───────────────────────────────────────────────────────

@router.post("/initialize")
def initialize_rules():
    try:
        initialize_default_rules()
        return {"message": "Default rules initialized successfully"}
    except Exception as e:
        raise HTTPException(status_code=500,
                            detail=f"Failed to initialize rules: {str(e)}")
