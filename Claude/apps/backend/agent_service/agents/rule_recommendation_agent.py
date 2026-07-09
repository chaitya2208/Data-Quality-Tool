"""Rule Recommendation Agent -- now hybrid: deterministic templates + Claude-
suggested business/domain rules, per the follow-up ask ("Add Claude to Rule
Recommendation... Your Rule Recommendation Agent should become hybrid").
The original v1 (template-only, run before any LLM existed in this codebase
per the earlier explicit "do not start with Claude first" instruction) is
the first half of run_rule_recommendation_agent() below; nothing about it
changed except that its output now feeds into dedup + Claude augmentation
rather than being returned directly.

Pipeline, matching the ask's diagram exactly:

    Template rules (5 skills)
         +
    Claude suggested rules (tools/claude_tools.recommend_rules_with_claude)
         v
    deduplicate  (_is_duplicate_of_template)
         v
    score        (skills._shared.compute_priority, same formula as templates)
         v
    apply feedback (_apply_feedback -- see "Add Feedback Loop" below)
         v
    return -- storage is the caller's job, not this agent's (see
              module-level note below on why store_recommended_rule() isn't
              called from here)

Claude is called with the profile + the template rules already found (see
claude_tools.build_claude_input()) specifically so it can propose what
templates miss, not restate them -- the system prompt in claude_tools.py
already instructs "don't repeat a rule already listed." This agent's own
_is_duplicate_of_template() is a second, code-level backstop on top of that
prompt instruction -- an LLM instruction is not a hard guarantee, and
mvp-scope.md's cross-cutting invariant #3 ("scans are idempotent... never
duplicate rules") is exactly the kind of thing that must hold regardless of
whether the LLM followed its own system prompt.

Claude is called once per table (not once per rule) -- see
claude_tools.py's docstring on why Sonnet, not Opus, and why this is a
single forced-tool-use call rather than an agentic loop.

Feedback Loop (per the "Add Feedback Loop" ask): before returning,
_apply_feedback() checks storage_tools.get_feedback_for_table() -- every
REJECT/EDIT/FALSE_POSITIVE ever recorded for this table (see main.py's
reject/edit routes and the false-positive route) -- and applies three
rules, matched per candidate on (rule_type, column_name):
    REJECT         -> drop the candidate entirely (never re-suggest the
                       same rule type on the same column after a human
                       explicitly said no).
    FALSE_POSITIVE -> halve priority (the rule concept had merit -- it got
                       approved and ran -- but this specific alert didn't;
                       lower it in the queue, don't block it outright).
    EDIT           -> seed threshold_config from the human's most recent
                       edited value instead of the skill's hardcoded
                       default, on the theory that an edited value is a
                       better starting point for the *next* candidate of
                       the same type/column than the generic one.
This runs on template rules and surviving (non-duplicate) Claude rules
alike -- feedback is a signal about the rule concept, not about which
source produced it.
"""

from __future__ import annotations

from typing import Any

from skills._shared import compute_priority
from skills.completeness_skill import suggest_completeness_rules
from skills.freshness_skill import suggest_freshness_rules
from skills.governance_skill import suggest_governance_rules
from skills.uniqueness_skill import suggest_uniqueness_rules
from skills.validity_skill import suggest_validity_rules
from skills.volume_skill import suggest_volume_rules
from tools.claude_tools import (
    build_claude_input,
    build_instance_claude_input,
    recommend_instances_with_claude,
    recommend_rules_with_claude,
)
from tools.rule_template_tools import render_sql_for_instance, render_sql_for_rule
from tools.storage_tools import (
    compute_rule_fingerprint,
    compute_target_key,
    create_rule_group,
    find_rule_group,
    get_active_instance_fingerprints,
    get_feedback_for_table,
    get_feedback_suppression_data,
    get_pending_instance_by_fingerprint,
    get_pending_rule_fingerprints,
    get_rejected_instance_fingerprints,
    list_rule_definitions,
    list_table_profile_history,
)


def _is_duplicate_of_template(
    claude_rule: dict[str, Any], template_rules: list[dict[str, Any]]
) -> bool:
    """A Claude-sourced candidate is a duplicate if it targets the same
    column and the same rule_type as an existing template rule.

    Matching on (column_name, rule_type) rather than rule_name -- Claude
    phrases rule names differently from the skills ("STATUS should not be
    RECONCILED if row counts do not balance" vs. a template's "STATUS should
    be one of the observed values"), so string-matching names would miss
    real duplicates. (column_name, rule_type) is coarser -- it would also
    reject a second, more specific COMPLETENESS rule Claude proposed for a
    column a template already covered for COMPLETENESS -- but that's the
    right tradeoff here: Claude's system prompt is already instructed to
    propose what templates *miss*, so a same-column-same-type Claude
    "addition" is far more likely a restatement than a genuinely distinct
    rule of that same type.
    """
    for template_rule in template_rules:
        if (
            claude_rule.get("column_name") == template_rule.get("column_name")
            and claude_rule.get("rule_type") == template_rule.get("rule_type")
        ):
            return True
    return False


# Halving priority on a false-positive signal (rather than, say, zeroing it
# or subtracting a flat amount) keeps the rule in the running -- it still
# sorts below an otherwise-equal candidate with no negative history, but a
# CRITICAL-severity rule with one false positive doesn't collapse to the
# same priority as an INFO-severity rule with none. A REJECT is the "block
# it outright" signal; FALSE_POSITIVE is deliberately softer.
_FALSE_POSITIVE_PRIORITY_MULTIPLIER = 0.5


def _apply_feedback(
    candidates: list[dict[str, Any]],
    database_name: str,
    schema_name: str,
    table_name: str,
) -> list[dict[str, Any]]:
    """Filter/adjust candidates using every REJECT/EDIT/FALSE_POSITIVE ever
    recorded for this table (storage_tools.get_feedback_for_table()) --
    see this module's docstring for the three rules applied. Matches on
    (rule_type, column_name) per candidate, same coarse-but-right-tradeoff
    key _is_duplicate_of_template() already uses for the same reason: a
    human's rejection of "a COMPLETENESS rule on CUSTOMER_ID" is a
    rejection of that concept, not of one specific worded rule_name.

    Returns a new list -- candidates rejected outright are dropped;
    surviving candidates get priority/threshold_config adjusted in place
    (as new dicts, not mutated) where feedback applies, others pass
    through unchanged.
    """
    feedback = get_feedback_for_table(database_name, schema_name, table_name)
    if not feedback:
        return candidates

    rejected_keys = {
        (f["rule_type"], f["column_name"])
        for f in feedback
        if f["feedback_type"] == "REJECT"
    }
    false_positive_keys = {
        (f["rule_type"], f["column_name"])
        for f in feedback
        if f["feedback_type"] == "FALSE_POSITIVE"
    }
    # Most recent EDIT wins per (rule_type, column_name) -- feedback is
    # ordered newest-first by get_feedback_for_table(), so the first EDIT
    # match encountered per key is already the latest one; a dict comprehension
    # iterating in that order and only inserting on first-seen achieves this
    # without a separate sort/groupby step.
    latest_edit_threshold: dict[tuple[str, str | None], dict] = {}
    for f in feedback:
        if f["feedback_type"] != "EDIT" or f["threshold_config"] is None:
            continue
        key = (f["rule_type"], f["column_name"])
        if key not in latest_edit_threshold:
            latest_edit_threshold[key] = f["threshold_config"]

    survivors = []
    for candidate in candidates:
        key = (candidate.get("rule_type"), candidate.get("column_name"))
        if key in rejected_keys:
            continue

        updated = candidate
        if key in false_positive_keys:
            updated = {
                **updated,
                "priority": round(
                    updated["priority"] * _FALSE_POSITIVE_PRIORITY_MULTIPLIER, 4
                ),
            }
        if key in latest_edit_threshold:
            updated = {**updated, "threshold_config": latest_edit_threshold[key]}
            # A changed threshold_config invalidates any already-rendered
            # generated_sql (a template rendered against the *old* default,
            # e.g. max_age_hours=24, would silently disagree with the
            # threshold now shown to the reviewer) -- re-render through the
            # same template dispatcher sql_generation_agent.py uses. Only
            # template-sourced rules have a dispatchable rule_type here;
            # a Claude-sourced rule_type render_sql_for_rule() doesn't
            # recognize raises ValueError, same as sql_generation_agent.py
            # already handles -- falls back to None (SQL Validation Agent
            # correctly marks it INVALID, same "recommended, not yet
            # executable" treatment as any other Claude rule_type gap).
            try:
                updated = {**updated, "generated_sql": render_sql_for_rule(updated)}
            except ValueError:
                updated = {**updated, "generated_sql": None}

        survivors.append(updated)

    return survivors


def run_rule_recommendation_agent(
    database_name: str,
    schema_name: str,
    table_name: str,
    row_count: int,
    column_profiles: list[dict[str, Any]],
) -> dict[str, Any]:
    """Run all 5 deterministic skills, then augment with Claude-suggested
    business/domain rules for the same table, deduplicated and scored on
    the same scale.

    Output: {"recommended_rules": [...]} -- same shape as before
    (rule_name, rule_type, database_name/schema_name/table_name/column_name,
    description, reason, evidence, severity, confidence, priority,
    threshold_config, generated_sql), template rules first then Claude
    rules. Claude-sourced rules have generated_sql=None here -- SQL
    Generation Agent (sql_generation_agent.py) fills in what it can from a
    template and leaves the rest None; see that module's docstring for why
    a Claude rule with no matching template isn't a pipeline error.

    If the Claude call fails (network, auth, Bedrock throttling), the whole
    scan should not fail with it -- template rules are the reliable
    baseline, so any exception from recommend_rules_with_claude() is caught
    and logged to stderr, falling back to template-only output. This
    mirrors this codebase's existing per-node error handling in
    graphs/dq_workflow_graph.py (a failing step is recorded, not fatal).
    """
    template_rules: list[dict[str, Any]] = []
    template_rules += suggest_completeness_rules(
        database_name, schema_name, table_name, column_profiles
    )
    template_rules += suggest_uniqueness_rules(
        database_name, schema_name, table_name, row_count, column_profiles
    )
    template_rules += suggest_validity_rules(
        database_name, schema_name, table_name, row_count, column_profiles
    )
    template_rules += suggest_freshness_rules(
        database_name, schema_name, table_name, column_profiles
    )

    # Historical-average volume comparison needs prior scans' row counts --
    # fetched here (the agent), not inside the skill itself, so
    # suggest_volume_rules() stays a pure function per skills/_shared.py's
    # convention. A lookup failure must not fail the whole recommendation,
    # same as the Claude call / feedback lookup below -- falls back to the
    # skill's own <3-history static-rule behavior.
    try:
        row_count_history = [
            r["row_count"]
            for r in list_table_profile_history(database_name, schema_name, table_name)
        ]
    except Exception as exc:  # noqa: BLE001
        print(f"[rule_recommendation_agent] Profile history lookup failed, using static volume rule: {exc}")
        row_count_history = []

    template_rules += suggest_volume_rules(
        database_name, schema_name, table_name, row_count, row_count_history
    )

    template_rules += suggest_governance_rules(
        database_name, schema_name, table_name, column_profiles
    )

    # Tag every template rule so the UI can distinguish source later.
    # Governance rules set their own fingerprint ("source:governance") inside
    # the skill -- only apply the template fallback to rules that don't already
    # have one (i.e. the 5 data-quality skills above).
    for r in template_rules:
        if r.get("rule_fingerprint") is None:
            r["rule_fingerprint"] = "source:template"

    # Existing pending rules for this table -- passed to Claude so it
    # doesn't re-propose what's already waiting for human review, and used
    # as a second dedup layer below alongside template rules.
    try:
        existing_pending_fingerprints = get_pending_rule_fingerprints(
            database_name, schema_name, table_name
        )
    except Exception:  # noqa: BLE001
        existing_pending_fingerprints = set()

    # Build a lightweight list for Claude's context (rule_name/type/column/description)
    # from the fingerprint set -- enough for Claude to avoid re-suggesting them.
    existing_pending_for_claude = [
        {"rule_name": None, "rule_type": rt, "column_name": col, "description": None}
        for (_, col, rt) in existing_pending_fingerprints
    ]

    claude_rules: list[dict[str, Any]] = []
    claude_error: str | None = None
    try:
        claude_input = build_claude_input(
            database_name,
            schema_name,
            table_name,
            column_profiles,
            template_rules,
            row_count=row_count,
            existing_pending_rules=existing_pending_for_claude,
        )
        claude_result = recommend_rules_with_claude(claude_input)
        table_classification = claude_result.get("table_classification")

        for candidate in claude_result.get("rules", []):
            if _is_duplicate_of_template(candidate, template_rules):
                continue
            # Also skip if already pending in storage
            fp = (candidate.get("table_name", table_name), candidate.get("column_name"), candidate.get("rule_type"))
            if fp in existing_pending_fingerprints:
                continue

            confidence = float(candidate.get("confidence", 0.5))
            severity = candidate.get("severity", "WARNING")
            claude_rules.append(
                {
                    "rule_name": candidate.get("rule_name"),
                    "rule_type": candidate.get("rule_type"),
                    "database_name": database_name,
                    "schema_name": schema_name,
                    "table_name": table_name,
                    "column_name": candidate.get("column_name"),
                    "description": candidate.get("description"),
                    "reason": candidate.get("reason"),
                    "evidence": candidate.get("evidence", []),
                    "severity": severity,
                    "confidence": round(confidence, 4),
                    # Re-derived from code, not trusted from Claude's own
                    # "priority" field -- see compute_priority()'s docstring
                    # and mvp-scope.md's "numbers from code" invariant.
                    "priority": compute_priority(confidence, severity),
                    "threshold_config": candidate.get("threshold_config"),
                    "generated_sql": candidate.get("generated_sql"),
                    "rule_fingerprint": "source:claude",
                }
            )
    except Exception as exc:  # noqa: BLE001 -- see docstring: don't fail the scan over this
        claude_error = str(exc)
        table_classification = None
        print(f"[rule_recommendation_agent] Claude call failed, using template rules only: {exc}")

    all_candidates = template_rules + claude_rules

    # Feedback Loop -- drop rejected rule/column concepts, halve priority on
    # a false-positive history, seed threshold_config from the latest human
    # edit. See _apply_feedback()'s docstring and this module's own for the
    # three rules applied. A feedback-lookup failure (storage/network) must
    # not fail the whole recommendation the same way a Claude failure
    # doesn't -- feedback is a refinement on top of an already-valid
    # candidate set, not something the scan depends on to produce rules.
    try:
        all_candidates = _apply_feedback(all_candidates, database_name, schema_name, table_name)
    except Exception as exc:  # noqa: BLE001
        print(f"[rule_recommendation_agent] Feedback lookup failed, skipping: {exc}")

    return {"recommended_rules": all_candidates, "claude_error": claude_error, "table_classification": table_classification}


# ---------------------------------------------------------------------------
# Library-aware instance recommendation (docs/rules-architecture.md §5.4)
#
# Additive alongside run_rule_recommendation_agent() above -- that function
# and its flat column_name/rule_type shape are untouched; graphs/
# dq_workflow_graph.py still calls it. This is the new scope/target_config/
# definition_id-aware pipeline a later phase wires into scan_operations.py.
# ---------------------------------------------------------------------------

# Maps a template skill's rule_type (+ a threshold_config disambiguator for
# the 3-way VALIDITY split and 2-way VOLUME/GOVERNANCE splits) to the exact
# NAME of the SYSTEM definition seeded by 14_seed_rule_definitions.sql. Kept
# as one lookup table rather than scattered if/elif so the mapping is
# auditable in one place against that seed file's 11 names.
_TEMPLATE_RULE_TYPE_TO_DEFINITION_NAME = {
    "COMPLETENESS": "Not Null Check",
    "UNIQUENESS": "Unique Values",
    "FRESHNESS": "Updated Within N Hours",
}


def _definition_name_for_template_candidate(candidate: dict[str, Any]) -> str | None:
    """Resolve which SYSTEM definition a template candidate maps to. VALIDITY/
    VOLUME/GOVERNANCE need threshold_config inspected (one rule_type covers
    multiple definitions); the rest are a straight 1:1 lookup. Returns None
    if nothing matches -- the caller skips the candidate with a warning
    rather than crashing (11 SYSTEM definitions are always seeded, so this
    is a defensive fallback, not an expected path).
    """
    rule_type = candidate.get("rule_type")
    threshold_config = candidate.get("threshold_config") or {}

    if rule_type == "VALIDITY":
        if "accepted_values" in threshold_config:
            return "Accepted Values"
        if "pattern" in threshold_config:
            return "Email Format"
        if "min_value" in threshold_config:
            return "Positive Amount"
        return None

    if rule_type == "VOLUME":
        if "historical_avg_row_count" in threshold_config:
            return "Row Count Within Historical Band"
        return "Row Count Above Zero"

    if rule_type == "GOVERNANCE":
        check = threshold_config.get("governance_check")
        return {
            "date_as_varchar": "Date Stored As Varchar",
            "boolean_as_varchar": "Boolean Stored As Varchar",
            "column_id_wrong_type": "Key Column Wrong Type",
        }.get(check)

    return _TEMPLATE_RULE_TYPE_TO_DEFINITION_NAME.get(rule_type)


def _map_template_candidates_to_instances(
    template_rules: list[dict[str, Any]],
    definitions_by_name: dict[str, dict[str, Any]],
    database_name: str,
    schema_name: str,
    table_name: str,
) -> list[dict[str, Any]]:
    """Map every flat template candidate (column_name/rule_type shape, as
    produced by the 6 skills) into the scope/target_config/definition_id
    instance shape (§4.4/§4.5), attaching a real rule_fingerprint to each.

    This is pure deterministic code with no external dependency -- unlike
    the Claude call and storage lookups below, a bug here is allowed to
    raise rather than being swallowed, matching how template-rule production
    already behaves in run_rule_recommendation_agent() above.
    """
    instances: list[dict[str, Any]] = []
    for candidate in template_rules:
        definition_name = _definition_name_for_template_candidate(candidate)
        definition = definitions_by_name.get(definition_name) if definition_name else None
        if definition is None:
            print(
                f"[rule_recommendation_agent] No matching SYSTEM definition for template "
                f"candidate rule_type={candidate.get('rule_type')!r} "
                f"threshold_config={candidate.get('threshold_config')!r} -- skipping"
            )
            continue

        column_name = candidate.get("column_name")
        scope = "TABLE" if column_name is None else "COLUMN"
        target_config = {} if column_name is None else {"column": column_name}
        threshold_config = candidate.get("threshold_config")
        definition_id = definition["definition_id"]

        fingerprint = compute_rule_fingerprint(
            definition_id, scope, database_name, schema_name, table_name,
            target_config, threshold_config,
        )

        instances.append(
            {
                "rule_name": candidate.get("rule_name"),
                "rule_type": candidate.get("rule_type"),
                "database_name": database_name,
                "schema_name": schema_name,
                "table_name": table_name,
                "scope": scope,
                "target_config": target_config,
                "definition_id": definition_id,
                "is_new_definition": False,
                "proposed_definition": None,
                "description": candidate.get("description"),
                "reason": candidate.get("reason"),
                "evidence": candidate.get("evidence"),
                "severity": candidate.get("severity"),
                "confidence": candidate.get("confidence"),
                "priority": candidate.get("priority"),
                "threshold_config": threshold_config,
                "generated_sql": candidate.get("generated_sql"),
                "rule_fingerprint": fingerprint,
                "suggested_group_id": None,
            }
        )
    return instances


def _lightweight_instance_list(fingerprints: Any) -> list[dict[str, Any]]:
    """Build the small {definition_id, scope} placeholder list
    build_instance_claude_input() wants for existing_approved_instances /
    existing_pending_instances -- enough for Claude to see roughly what
    already exists without a second round-trip to fetch full instance rows.
    Fingerprints alone don't carry definition_id/scope, so this is
    deliberately a minimal, honest placeholder (empty dicts when there's
    nothing more specific to say) rather than a fabricated shape.
    """
    return [{} for _ in fingerprints]


def run_instance_recommendation_agent(
    database_name: str,
    schema_name: str,
    table_name: str,
    row_count: int,
    column_profiles: list[dict[str, Any]],
) -> dict[str, Any]:
    """Library-aware instance recommendation (docs/rules-architecture.md
    §5.4's full pipeline). Additive alongside run_rule_recommendation_agent()
    -- does not replace it.

    Output: {"recommended_instances": [...], "claude_error": str | None,
    "table_classification": dict | None, "new_definitions_staged": [...]}.
    Each recommended_instances entry: rule_name, rule_type, database_name,
    schema_name, table_name, scope, target_config, definition_id,
    is_new_definition, proposed_definition, description, reason, evidence,
    severity, confidence, priority, threshold_config, generated_sql,
    rule_fingerprint, suggested_group_id.

    Same error-tolerance tiering as run_rule_recommendation_agent(): a
    Claude failure, a storage lookup failure, or a grouping failure must
    never raise out of this function (falls back / skips and continues) --
    only step 1's deterministic template-mapping is allowed to raise on a
    genuine bug, since it has no external dependency.
    """
    # -- Step 1: deterministic skills, mapped into the instance shape -------
    template_rules: list[dict[str, Any]] = []
    template_rules += suggest_completeness_rules(database_name, schema_name, table_name, column_profiles)
    template_rules += suggest_uniqueness_rules(database_name, schema_name, table_name, row_count, column_profiles)
    template_rules += suggest_validity_rules(database_name, schema_name, table_name, row_count, column_profiles)
    template_rules += suggest_freshness_rules(database_name, schema_name, table_name, column_profiles)

    try:
        row_count_history = [
            r["row_count"] for r in list_table_profile_history(database_name, schema_name, table_name)
        ]
    except Exception as exc:  # noqa: BLE001
        print(f"[rule_recommendation_agent] Profile history lookup failed, using static volume rule: {exc}")
        row_count_history = []
    template_rules += suggest_volume_rules(database_name, schema_name, table_name, row_count, row_count_history)

    template_rules += suggest_governance_rules(database_name, schema_name, table_name, column_profiles)

    active_definitions = list_rule_definitions(status="ACTIVE")
    definitions_by_name = {d["name"]: d for d in active_definitions}

    mapped_template_instances = _map_template_candidates_to_instances(
        template_rules, definitions_by_name, database_name, schema_name, table_name
    )

    # -- Step 2: existing fingerprints (active / rejected / pending) --------
    try:
        existing_approved_fingerprints = get_active_instance_fingerprints(database_name, schema_name, table_name)
    except Exception:  # noqa: BLE001
        existing_approved_fingerprints = set()
    try:
        rejected_fingerprints_with_reasons = get_rejected_instance_fingerprints(database_name, schema_name, table_name)
    except Exception:  # noqa: BLE001
        rejected_fingerprints_with_reasons = {}
    try:
        pending_fingerprints = get_pending_instance_by_fingerprint(database_name, schema_name, table_name)
    except Exception:  # noqa: BLE001
        pending_fingerprints = {}

    # -- Step 3: feedback suppression data -----------------------------------
    try:
        feedback_data = get_feedback_suppression_data(database_name, schema_name, table_name)
    except Exception:  # noqa: BLE001
        feedback_data = {"suppressed": {}, "priority_halved": set(), "threshold_seeds": {}}

    # -- Step 4: call Claude (library-aware) ---------------------------------
    claude_new_definitions: list[dict[str, Any]] = []
    claude_instance_suggestions: list[dict[str, Any]] = []
    claude_error: str | None = None
    table_classification: dict[str, Any] | None = None
    try:
        claude_input = build_instance_claude_input(
            database_name,
            schema_name,
            table_name,
            column_profiles,
            row_count,
            rule_definition_library=active_definitions,
            existing_approved_instances=_lightweight_instance_list(existing_approved_fingerprints),
            existing_pending_instances=_lightweight_instance_list(pending_fingerprints),
            rejected_instances_with_reasons=[{"reason": r} for r in rejected_fingerprints_with_reasons.values()],
            # build_instance_claude_input() expects feedback_signals as a
            # JSON-safe list -- feedback_data's own shape (suppressed dict,
            # priority_halved *set*, threshold_seeds dict) is code-internal
            # for storage_tools.get_feedback_suppression_data()'s consumers,
            # not something to hand to json.dumps() directly.
            feedback_signals=[
                {"type": "REJECT", "target_key": k, "comment": v}
                for k, v in feedback_data.get("suppressed", {}).items()
            ] + [
                {"type": "FALSE_POSITIVE", "target_key": k}
                for k in feedback_data.get("priority_halved", set())
            ] + [
                {"type": "EDIT", "target_key": k, "threshold_config": v}
                for k, v in feedback_data.get("threshold_seeds", {}).items()
            ] or None,
        )
        claude_result = recommend_instances_with_claude(claude_input)
        table_classification = claude_result.get("table_classification")
        claude_new_definitions = claude_result.get("new_definitions", [])
        claude_instance_suggestions = claude_result.get("instance_suggestions", [])
    except Exception as exc:  # noqa: BLE001 -- must not fail the scan over this
        claude_error = str(exc)
        print(f"[rule_recommendation_agent] Instance Claude call failed, using template instances only: {exc}")

    # -- Step 5a: match/stage new_definitions --------------------------------
    # index -> either a real existing definition_id (str) or the staged
    # proposed_definition dict itself (not yet in RULE_DEFINITIONS -- §4.3.1).
    resolved_new_definitions: dict[int, Any] = {}
    new_definitions_staged: list[dict[str, Any]] = []
    for idx, new_def in enumerate(claude_new_definitions):
        category = new_def.get("category")
        name = (new_def.get("name") or "").strip().lower()
        matched_existing = None
        for existing in active_definitions:
            if existing.get("category") != category:
                continue
            existing_name = (existing.get("name") or "").strip().lower()
            if existing_name == name or name in existing_name or existing_name in name:
                matched_existing = existing
                break
        if matched_existing is not None:
            resolved_new_definitions[idx] = matched_existing["definition_id"]
        else:
            resolved_new_definitions[idx] = new_def
            new_definitions_staged.append(new_def)

    # -- Step 5b-5e: resolve, score, fingerprint, suppress, render Claude instances --
    resolved_claude_instances: list[dict[str, Any]] = []
    for suggestion in claude_instance_suggestions:
        definition_id = suggestion.get("definition_id")
        new_definition_index = suggestion.get("new_definition_index")
        proposed_definition = None
        is_new_definition = False

        if definition_id is None and new_definition_index is not None:
            resolved = resolved_new_definitions.get(new_definition_index)
            if isinstance(resolved, str):
                definition_id = resolved
            elif isinstance(resolved, dict):
                proposed_definition = resolved
                is_new_definition = True
            else:
                # new_definition_index pointed nowhere resolvable -- skip
                # this suggestion rather than guessing.
                continue
        elif definition_id is None:
            # Neither definition_id nor a resolvable new_definition_index --
            # malformed suggestion, skip it.
            continue

        confidence = float(suggestion.get("confidence", 0.5))
        severity = suggestion.get("severity", "WARNING")
        priority = compute_priority(confidence, severity)
        scope = suggestion.get("scope")
        target_config = suggestion.get("target_config") or {}
        threshold_config = suggestion.get("threshold_config")

        if is_new_definition:
            rule_fingerprint = None
        else:
            rule_fingerprint = compute_rule_fingerprint(
                definition_id, scope, database_name, schema_name, table_name,
                target_config, threshold_config,
            )
            # -- 5c: respect active/rejected fingerprint matches (simplified --
            # no "meaningfully new evidence" re-propose exception; see report).
            if rule_fingerprint in existing_approved_fingerprints:
                continue
            if rule_fingerprint in rejected_fingerprints_with_reasons:
                continue

        instance = {
            "rule_name": suggestion.get("reason") or f"Claude-suggested check ({scope})",
            "rule_type": (proposed_definition or {}).get("category") if is_new_definition else (
                next((d["category"] for d in active_definitions if d["definition_id"] == definition_id), None)
            ),
            "database_name": database_name,
            "schema_name": schema_name,
            "table_name": table_name,
            "scope": scope,
            "target_config": target_config,
            "definition_id": None if is_new_definition else definition_id,
            "is_new_definition": is_new_definition,
            "proposed_definition": proposed_definition,
            "description": suggestion.get("reason"),
            "reason": suggestion.get("reason"),
            "evidence": suggestion.get("evidence"),
            "severity": severity,
            "confidence": round(confidence, 4),
            "priority": priority,
            "threshold_config": threshold_config,
            "generated_sql": None,
            "rule_fingerprint": rule_fingerprint,
            "suggested_group_id": None,
            "_suggested_group_name": suggestion.get("suggested_group_name"),
            "_draft_generated_sql": suggestion.get("draft_generated_sql"),
        }

        # -- 5d: feedback suppression (only meaningful for real definitions) --
        if not is_new_definition:
            target_key = compute_target_key(definition_id, scope, target_config)
            if target_key in feedback_data.get("suppressed", {}):
                continue
            if target_key in feedback_data.get("priority_halved", set()):
                instance["priority"] = round(instance["priority"] * 0.5, 4)
            seeded_threshold = feedback_data.get("threshold_seeds", {}).get(target_key)
            if seeded_threshold is not None:
                instance["threshold_config"] = seeded_threshold
                instance["rule_fingerprint"] = compute_rule_fingerprint(
                    definition_id, scope, database_name, schema_name, table_name,
                    target_config, seeded_threshold,
                )

        # -- 5e: render SQL --------------------------------------------------
        if not is_new_definition:
            definition = next((d for d in active_definitions if d["definition_id"] == definition_id), None)
            if definition is not None and definition.get("sql_template"):
                try:
                    instance["generated_sql"] = render_sql_for_instance(
                        definition, scope, target_config, database_name, schema_name, table_name,
                        instance["threshold_config"],
                    )
                except Exception as exc:  # noqa: BLE001 -- must not crash the whole recommendation
                    print(f"[rule_recommendation_agent] render_sql_for_instance failed for {definition_id}: {exc}")
                    instance["generated_sql"] = None
            else:
                instance["generated_sql"] = instance.pop("_draft_generated_sql", None)
        else:
            instance["generated_sql"] = instance.pop("_draft_generated_sql", None)
        instance.pop("_draft_generated_sql", None)

        resolved_claude_instances.append(instance)

    # -- Step 5f: merge + dedupe on fingerprint -------------------------------
    merged: list[dict[str, Any]] = list(mapped_template_instances)
    seen_fingerprints = {i["rule_fingerprint"] for i in mapped_template_instances if i["rule_fingerprint"]}
    for instance in resolved_claude_instances:
        fp = instance["rule_fingerprint"]
        if fp is not None and fp in seen_fingerprints:
            continue
        if fp is not None:
            seen_fingerprints.add(fp)
        merged.append(instance)

    # -- Step 5g: grouping -----------------------------------------------------
    group_name_to_id: dict[str, str] = {}
    for instance in merged:
        group_name = instance.pop("_suggested_group_name", None)
        if not group_name:
            continue
        try:
            if group_name not in group_name_to_id:
                definition_id_for_group = instance.get("definition_id") or "PENDING_NEW_DEFINITION"
                existing_group = find_rule_group(
                    name=group_name, definition_id=definition_id_for_group,
                    scope_level="TABLE", schema_name=schema_name,
                )
                if existing_group is not None:
                    group_name_to_id[group_name] = existing_group["group_id"]
                else:
                    group_name_to_id[group_name] = create_rule_group(
                        name=group_name, definition_id=definition_id_for_group,
                        scope_level="TABLE", database_name=database_name,
                        schema_name=schema_name, table_name=table_name,
                    )
            instance["suggested_group_id"] = group_name_to_id[group_name]
        except Exception as exc:  # noqa: BLE001 -- grouping must not fail the whole recommendation
            print(f"[rule_recommendation_agent] Grouping failed for {group_name!r}: {exc}")

    # Strip any remaining internal-only keys before returning.
    for instance in merged:
        instance.pop("_suggested_group_name", None)
        instance.pop("_draft_generated_sql", None)

    return {
        "recommended_instances": merged,
        "claude_error": claude_error,
        "table_classification": table_classification,
        "new_definitions_staged": new_definitions_staged,
    }
