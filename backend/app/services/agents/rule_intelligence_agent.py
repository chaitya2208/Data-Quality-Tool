"""
Rule Intelligence Agent — the brain of the pipeline.

Single Claude call that does three things in one shot:
1. Reviews the definition library and decides which definitions apply to
   this table (with reasons), for definitions that already have an active
   or pending instance here — Claude is told about deactivation/skip only,
   not re-invention.
2. Proposes NEW instances — either of an existing definition applied to a
   new target, or of a genuinely new check concept (a new definition).
3. For each new instance, immediately checks if the current schema
   violates it.

This is where the app's memory gap gets fixed: every candidate instance is
fingerprinted (definition + scope + target + threshold) and deduped against
RULE_INSTANCES before it's ever shown to a human — instead of Claude
reinventing the same checks every scan and going active with only an
implicit review-list gate. Every AI-proposed instance always lands PENDING;
there is no more "included in the active list = approved."

Output stored in AgentTask.output for the UI:
  - table_type + confidence
  - definitions_evaluated: [{definition_id, name, run, reason}]
  - instances_proposed: [{instance dict, fingerprint_match}]
"""
import json
import logging
import re
from typing import List, Optional, Dict, Any, Set

from app.services import storage
from app.services.fingerprint import compute_fingerprint
from app.services.snowflake_session import session as sf_session
from app.services.claude_client import ask_claude, ask_claude_agentic
from app.services.sql_validation import validate_sql
from app.services.rule_sql_templates import TEMPLATE_SHAPES, render_template
from app.services.text_similarity import word_overlap_score, DEFAULT_SIMILARITY_THRESHOLD

logger = logging.getLogger(__name__)

# Server-side statement timeout (seconds) for running a candidate check's SQL
# at proposal time. Bounds the cost of an AI-authored draft_sql that passes
# static validation but is accidentally expensive — see _execute_check_sql.
_CHECK_SQL_TIMEOUT_SECONDS = 120

_TEMPLATE_SHAPES_DOC = "\n".join(
    f"  - {name} (scope={spec['scope']}, needs threshold_config keys: {spec['params'] or 'none'})"
    for name, spec in TEMPLATE_SHAPES.items()
)

# Tool available to Claude during the rule-intelligence agentic loop.
# Claude can call this when it needs more evidence before it's confident
# enough to propose (or skip) a check — e.g. to see the distribution of
# a sparse column, verify a suspected NULL pattern, or inspect specific rows.
_SAMPLE_TOOL_SCHEMA = [
    {
        "name": "get_sample_rows",
        "description": (
            "Fetch sample rows from the table being analysed. Use this when "
            "the column statistics and top_values you already have are not "
            "enough to decide whether a check is worth proposing — for "
            "example: a sparse column where top_values covers only nulls, a "
            "suspected format pattern you want to verify, or a multi-column "
            "relationship you need concrete row evidence for. "
            "Only query the table you were given — requests for other tables "
            "will be rejected. Column names must match the schema exactly. "
            "Results are capped at 20 rows."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "columns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Subset of columns to return. Use a focused list — "
                        "omit columns you already understand. Leave empty to "
                        "return all columns (capped at 10)."
                    ),
                },
                "where_clause": {
                    "type": "string",
                    "description": (
                        "Optional WHERE predicate (no WHERE keyword). "
                        "Example: \"STATUS IS NULL\" or \"AMOUNT < 0\". "
                        "Must be a pure filter — no subqueries, no JOINs, "
                        "no aggregates."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of rows to return (1-20, default 10).",
                    "minimum": 1,
                    "maximum": 20,
                },
                "reason": {
                    "type": "string",
                    "description": "Why you need these rows — logged for observability.",
                },
            },
            "required": [],
        },
    }
]

# Max columns shown when Claude doesn't specify a column list
_SAMPLE_DEFAULT_COLS = 10
# Hard cap regardless of what Claude requests
_SAMPLE_MAX_ROWS = 20

SYSTEM_PROMPT = f"""You are a senior Snowflake data quality architect.

Given a table schema, real column statistics computed from the live data,
and the library of check DEFINITIONS this system already knows about (plus
what is already running or pending on this exact table), you will:
1. Classify the table type (fact/dimension/staging/config/audit/reference)
2. For each existing definition that is already active or pending on THIS
   table, decide whether it should keep running (rarely — only if clearly
   irrelevant to this table's business purpose)
3. Propose NEW instances: either a new application of an EXISTING definition
   to a column/table on THIS table that doesn't have one yet, or — only if no
   existing definition covers the concept — a genuinely NEW check.
4. Every new check MUST be backed by a real, executable SQL check — never a
   one-time opinion. Prefer one of these known template shapes when it fits
   (Claude just names the shape + the target/params, no SQL to write):
{_TEMPLATE_SHAPES_DOC}
   If none of these shapes fit the concept, write draft_sql yourself: a
   single SELECT returning exactly two columns, FAILED_COUNT and
   TOTAL_COUNT, querying only the table given above. This draft_sql will be
   validated (SELECT-only, single statement, no forbidden keywords, only
   this table referenced) before it can ever run — if it fails validation
   the instance is discarded, so write it carefully and test it mentally
   against the column stats you were given.
5. Use the column statistics (null%, distinct count, min/max, top values)
   to decide whether a check is worth proposing and whether it's currently
   violated — you have real numbers, not just a 3-row guess.
6. You have access to a tool: get_sample_rows. Call it when the column
   statistics alone are not enough to be confident — for example: a column
   whose top_values are all NULL and you want to know if non-null values
   follow a pattern; a suspected multi-column constraint you want to verify
   with a filtered sample; or a column you want to see before proposing an
   accepted_values or regex check. Do NOT call it for columns you already
   have enough evidence for. You may call it up to 5 times total.

Respond with valid JSON only — no markdown, no prose outside the JSON.
Be thorough, but NEVER propose an instance that duplicates something already
active or pending on this table (shown to you explicitly below) — assume the
system will silently drop exact duplicates, so re-proposing them wastes a
slot a genuinely new check could use instead.
Focus new instances on: value constraints, referential patterns, naming
standards for this domain, null semantics, data freshness, business key
uniqueness patterns.

You will also be given DETERMINISTIC SIGNALS — objective facts already
computed from live data (e.g. "this PK-shaped column has duplicate values",
"this timestamp column's most recent value is N days old"). Every signal
listed MUST get an entry in "signals_evaluated" in your response — you may
decide a signal doesn't need a NEW instance and say so with a reason, but
you cannot silently omit a listed signal. Some signals (uniqueness
violations, confirmed cross-table orphan relationships) are marked
"already proposed this run, do not duplicate" — a candidate instance for
those already exists outside this conversation (verified deterministically,
no LLM judgment needed for the objective fact itself); do not add another
new_instances entry for them, just acknowledge them in signals_evaluated.
Freshness signals are NOT auto-proposed — a specific staleness threshold is
a business judgment call only you (or the human reviewer) can make, so you
decide whether and how to propose a freshness check for those.

You will also be given KNOWN CROSS-TABLE RELATIONSHIPS for this table (real,
already-verified against live data). If you propose a referential_integrity
check, ref_table/ref_column MUST come from that list — never invent a
relationship that wasn't given to you.

You will also be given LOW-CARDINALITY COLUMNS with their full observed
value sets. If you propose an accepted_values check on one of these columns,
every value in your accepted_values list MUST come from that observed set —
any value you list that wasn't actually observed will be silently removed
before the check can run, so there is no benefit to padding the list from
assumed/world knowledge."""

USER_PROMPT_TEMPLATE = """Table: {fqn}
Row count: {row_count}
Owner: {owner}

=== SCHEMA ===
{columns}

=== COLUMN STATISTICS (computed from the live data — use these, not guesses) ===
{column_stats}

=== DETERMINISTIC SIGNALS (objective facts computed from live data — every
signal below MUST get an entry in signals_evaluated; see system prompt) ===
{deterministic_signals}

=== KNOWN CROSS-TABLE RELATIONSHIPS (verified against live data — any
referential_integrity proposal's ref_table/ref_column MUST come from this
list; never invent one) ===
{relationship_catalog}

=== LOW-CARDINALITY COLUMNS — full observed value sets (ground any
accepted_values proposal in these; any value not in this set will be
silently removed before the check can run) ===
{closed_set_columns}

=== DEFINITION LIBRARY (active check concepts this system already knows) ===
{definitions}

=== ALREADY ACTIVE OR PENDING ON THIS TABLE (do NOT re-propose these) ===
{existing_instances}

=== PAST INTELLIGENCE — what was learned from similar tables in prior scans
(use this to avoid repeating mistakes, re-proposing previously rejected
patterns, and to recognise table types you have seen before) ===
{past_context}

Respond with this JSON:
{{
  "table_type": "fact|dimension|staging|config|audit|reference|unknown",
  "table_type_confidence": <0-100>,
  "table_type_reason": "one sentence",
  "definitions_evaluated": {{
    "<definition_id>": {{
      "keep_running": true/false,
      "severity_override": null or "critical|high|medium|low",
      "reason": "one sentence"
    }}
  }},
  "signals_evaluated": {{
    "<signal_id>": {{
      "propose_instance": true/false,
      "reason": "one sentence — why you did or didn't propose a new_instances entry for this signal"
    }}
  }},
  "new_instances": [
    {{
      "definition_id": "<existing definition id>" or null,
      "new_definition": null or {{
        "name": "Short descriptive name",
        "category": "data_quality|schema|naming|security|ownership",
        "description": "What this checks and why it matters"
      }},
      "scope": "table" or "column" or "multi_column" or "cross_table",
      "column_name": "COLUMN_NAME_IF_COLUMN_SCOPE or null",
      "columns": ["COL_A", "COL_B"] or null (for multi_column scope only),
      "template_shape": "one of the template shape names above, or null if none fit",
      "threshold_config": {{}} (params the chosen template_shape needs — e.g. {{"accepted_values": ["A","B"]}}, {{"pattern": "..."}}, {{"max_age_hours": 24}}),
      "cross_table_ref": null or {{"ref_database": "...", "ref_schema": "...", "ref_table": "...", "ref_column": "..."}},
      "draft_sql": "a single SELECT returning FAILED_COUNT, TOTAL_COUNT — ONLY if template_shape is null",
      "severity": "critical|high|medium|low",
      "violation_detected": true/false,
      "violation_evidence": "what specifically is wrong (cite the real stats above), or null if not violated",
      "rationale": "why this instance matters for this specific table"
    }}
  ]
}}

IMPORTANT REQUIREMENTS:
- Every definitions_evaluated entry MUST cover every definition listed as
  "ALREADY ACTIVE OR PENDING ON THIS TABLE" above — decide keep_running for
  each one.
- Every signals_evaluated entry MUST cover every signal_id listed under
  DETERMINISTIC SIGNALS above — decide propose_instance for each one.
- new_instances entries with definition_id set reuse an existing definition
  for a NEW target on this table. Entries with new_definition set propose a
  genuinely new concept — only use this when nothing in the library fits.
- Every new_instances entry MUST set exactly one of template_shape or
  draft_sql — never both null, never both set. A check with neither is
  useless: it can never actually run.
- Base violation_detected and violation_evidence on the COLUMN STATISTICS
  and SAMPLE DATA given above, not assumption.
- Generate 1-5 new_instances. Skip if this table is already well covered by
  what's active/pending — do not force new suggestions.
- Your ENTIRE response must be a single valid JSON object starting with {{ and ending with }}."""

VALID_CATEGORIES = {"naming", "documentation", "ownership", "schema", "data_quality", "security", "performance"}
VALID_SEVERITIES = {"critical", "high", "medium", "low", "info"}


class RuleIntelligenceAgent:
    """
    Library-aware classifier + suggester. Single Claude call, results
    fingerprint-deduped against RULE_INSTANCES before ever reaching a human.
    """

    def __init__(self):
        self._severity_backup: Dict[str, str] = {}
        self._closed_set_columns: Dict[str, dict] = {}

    def run(
        self,
        table_asset: Any,
        column_assets: List[Any],
        existing_definitions: List[Any],
        run_id: str,
        profiler_result: Optional[Dict[str, Any]] = None,
        relationship_catalog: Optional[List[Any]] = None,
    ) -> Dict[str, Any]:
        """
        Returns a result dict with:
          - classification: table type + per-definition keep/skip decisions
          - proposed_instances: list of dicts, each either
              {"kind": "reuse", "instance": SimpleNamespace, "violated": bool, "evidence": str}
            or
              {"kind": "new", "definition": dict (unsaved), "instance": dict (unsaved), ...}
              — "new" entries have NOT been persisted; the caller (coordinator)
              persists them as PROPOSED/pending only after building the
              review state, so nothing exists in storage until reviewed.
              Each entry also carries "source": "deterministic" | "llm" —
              "deterministic" entries bypassed the LLM entirely (uniqueness on
              a PK-shaped column, a confirmed cross-table orphan relationship)
              because they're objective facts verified against live data, not
              business judgment calls — they still go through the same
              fingerprint/execute/validate path and land pending like every
              other proposal, so the human-approval gate is unchanged.
          - suppressed: list of dicts describing candidates dropped due to
              fingerprint match against active/rejected instances — logged,
              never shown to the human as new.

        profiler_result (from DeterministicProfilerAgent) and
        relationship_catalog (from relationship_discovery.get_or_refresh_catalog)
        are optional so this agent still runs (with reduced signal) if either
        upstream step failed — see coordinator.py's degrade-gracefully pattern
        for the rest of this pipeline.
        """
        logger.info(f"[RuleIntelligence] Analyzing {table_asset.fqn}")

        profiler_result = profiler_result or {}
        relationship_catalog = relationship_catalog or []
        self._closed_set_columns = profiler_result.get("closed_set_columns") or {}

        existing_instances = self._existing_instances_for_table(table_asset)
        rejected_instances = self._rejected_instances_for_table(table_asset)

        # Definitions referenced by existing_instances may be PROPOSED (not
        # yet ACTIVE) — resolve those directly so Claude can see their real
        # name/description instead of a bare UUID, and so dedup-by-similarity
        # catches a re-proposal of an already-staged concept. Rejected
        # instances' definitions are included too (but not in the prompt's
        # "already active/pending" list) so a re-proposed rejected concept
        # gets matched by name/description and hits the rejection-suppression
        # path instead of silently becoming a fresh duplicate definition.
        by_id = {d.id: d for d in existing_definitions}
        for inst in existing_instances + rejected_instances:
            if inst.definition_id not in by_id:
                resolved = storage.get_definition(inst.definition_id)
                if resolved:
                    by_id[resolved.id] = resolved
        all_known_definitions = list(by_id.values())

        # ── Deterministic candidates first — objective facts verified
        # against live data, bypass the LLM entirely (see class docstring).
        # Run through the identical _process_candidate fingerprint/execute/
        # validate path as LLM candidates, just tagged source="deterministic".
        proposed_instances: List[dict] = []
        suppressed: List[dict] = []
        deterministic_fingerprints: Set[str] = set()

        for det_candidate in self._build_deterministic_candidates(
            table_asset, profiler_result, relationship_catalog, existing_instances,
        ):
            result = self._process_candidate(
                det_candidate, table_asset, run_id, all_known_definitions, source="deterministic",
            )
            if result is None:
                continue
            if result.get("suppressed"):
                suppressed.append(result)
            else:
                proposed_instances.append(result)
                deterministic_fingerprints.add(result["fingerprint"])

        column_stats_text = self._format_column_stats(profiler_result.get("column_stats") or {})
        columns_text = self._format_columns(column_assets)
        definitions_text = self._format_definitions(existing_definitions)
        existing_text = self._format_existing_instances(existing_instances, all_known_definitions)
        signals_text = self._format_signals(profiler_result, proposed_instances)
        relationships_text = self._format_relationships(table_asset, relationship_catalog)
        closed_sets_text = self._format_closed_sets(self._closed_set_columns)
        past_context_text = self._format_past_context(table_asset)

        prompt = USER_PROMPT_TEMPLATE.format(
            fqn=table_asset.fqn,
            row_count=table_asset.row_count or "unknown",
            owner=table_asset.owner or "unknown",
            columns=columns_text,
            column_stats=column_stats_text,
            deterministic_signals=signals_text,
            relationship_catalog=relationships_text,
            closed_set_columns=closed_sets_text,
            definitions=definitions_text,
            existing_instances=existing_text,
            past_context=past_context_text,
        )

        raw, thinking, tool_calls = self._call_model(prompt, table_asset)
        parsed = self._extract_json(raw)

        # A parse failure (malformed/truncated response) is NOT the same as a
        # model that ran fine and proposed nothing — but downstream both look
        # like "0 new instances". Retry once with an explicit repair
        # instruction before giving up, and flag the run when even the retry
        # fails so it surfaces to the reviewer instead of masquerading as a
        # clean, well-covered table.
        if tool_calls:
            logger.info(
                f"[RuleIntelligence] Claude used get_sample_rows {len(tool_calls)} time(s) — "
                + "; ".join((tc.get("input") or {}).get("reason", "no reason given") for tc in tool_calls)
            )

        parse_failed = False
        if not parsed:
            logger.warning("[RuleIntelligence] Could not parse model JSON — retrying once with repair prompt")
            repair_prompt = (
                prompt
                + "\n\nYour previous response could not be parsed as JSON. "
                "Respond again with ONLY a single valid JSON object matching the "
                "schema above — no markdown fences, no prose before or after."
            )
            raw, _retry_thinking, _retry_tools = self._call_model(repair_prompt, table_asset)
            parsed = self._extract_json(raw)

        if not parsed:
            logger.error(
                "[RuleIntelligence] Model JSON unparseable after retry — proceeding with "
                "defaults (0 proposals). Flagging run so this isn't mistaken for full coverage."
            )
            parse_failed = True
            parsed = {}

        # ── Self-critique pass ───────────────────────────────────────────────
        # After Claude has proposed its full list, a second lightweight call
        # reads those proposals back and drops any that score below threshold.
        # This actively shrinks reviewer queue size and teaches Claude that
        # weak proposals have a cost.
        if parsed.get("new_instances"):
            parsed["new_instances"] = self._self_critique_proposals(
                proposals=parsed["new_instances"],
                table_asset=table_asset,
                column_stats_text=column_stats_text,
            )

        classification = {
            "table_type":            parsed.get("table_type", "unknown"),
            "table_type_confidence": parsed.get("table_type_confidence", 50),
            "table_type_reason":     parsed.get("table_type_reason", ""),
            "definitions_evaluated": parsed.get("definitions_evaluated", {}),
            "signals_evaluated":     parsed.get("signals_evaluated", {}),
        }

        # Persist the intelligence log (Option B) — best-effort, never blocks
        # the pipeline if it fails. The log id is stored on the result so the
        # coordinator can back-fill approved/rejected counts after user review.
        intelligence_log_id = None
        try:
            signals_used = {
                "pk_shaped_candidates": profiler_result.get("pk_shaped_candidates", []),
                "freshness_signals": profiler_result.get("freshness_signals", []),
                "closed_set_columns": list((profiler_result.get("closed_set_columns") or {}).keys()),
                "relationships": len(relationship_catalog),
                "sample_tool_calls": [
                    {"reason": (tc.get("input") or {}).get("reason", ""), "where": (tc.get("input") or {}).get("where_clause", "")}
                    for tc in tool_calls
                ],
            }
            log = storage.create_intelligence_log(
                run_id=run_id,
                table_fqn=table_asset.fqn,
                table_type=classification["table_type"],
                table_type_confidence=classification["table_type_confidence"],
                thinking=thinking,
                signals_used=signals_used,
                proposals_count=len(parsed.get("new_instances", [])),
                suppressed_count=len(suppressed),
            )
            intelligence_log_id = log.id
            logger.info(f"[RuleIntelligence] Intelligence log saved: {intelligence_log_id}")
        except Exception as e:
            logger.warning(f"[RuleIntelligence] Could not save intelligence log: {e}")

        # Normalize: fill missing decisions with default (keep_running=True)
        for inst in existing_instances:
            def_id = inst.definition_id
            if def_id not in classification["definitions_evaluated"]:
                classification["definitions_evaluated"][def_id] = {
                    "keep_running": True, "severity_override": None,
                    "reason": "Default — not explicitly re-evaluated",
                }

        # Signals Claude never addressed at all — logged, not hard-failed
        # (this codebase's existing graceful-degradation philosophy; see
        # _extract_json falling back to {} rather than raising).
        expected_signal_ids = {s["signal_id"] for s in self._all_signal_ids(profiler_result)}
        signals_missed = sorted(expected_signal_ids - set(classification["signals_evaluated"].keys()))
        if signals_missed:
            logger.debug(f"[RuleIntelligence] Signals not addressed by Claude: {signals_missed}")

        for candidate in parsed.get("new_instances", []):
            result = self._process_candidate(
                candidate, table_asset, run_id, all_known_definitions, source="llm",
            )
            if result is None:
                continue
            if result.get("suppressed"):
                suppressed.append(result)
                continue
            if result["fingerprint"] in deterministic_fingerprints:
                # Claude independently proposed the same target a deterministic
                # candidate already covers this run — suppress, don't duplicate.
                suppressed.append({
                    "suppressed": True, "reason": "already_proposed_deterministically",
                    "fingerprint": result["fingerprint"], "candidate": candidate,
                })
                continue
            proposed_instances.append(result)

        logger.info(
            f"[RuleIntelligence] Table={classification['table_type']}, "
            f"existing_instances={len(existing_instances)}, "
            f"proposed={len(proposed_instances)}, suppressed_dupes={len(suppressed)}"
        )

        return {
            "classification": classification,
            "existing_instances": existing_instances,
            "proposed_instances": proposed_instances,
            "suppressed": suppressed,
            "signals_missed": signals_missed,
            "parse_failed": parse_failed,
            "intelligence_log_id": intelligence_log_id,
        }

    # ── Existing-state gathering ──────────────────────────────────────────

    def _existing_instances_for_table(self, table_asset: Any) -> List[Any]:
        """Active or pending instances scoped to this specific table, plus
        every global (DATABASE_NAME='*') instance — those apply everywhere."""
        _, table_scoped = storage.list_instances(
            database_name=table_asset.database_name,
            schema_name=table_asset.schema_name,
            table_name=table_asset.table_name,
            limit=1000,
        )
        table_scoped = [i for i in table_scoped if i.status in ("active", "pending")]

        _, globals_ = storage.list_instances(database_name="*", limit=1000)
        globals_ = [i for i in globals_ if i.status in ("active", "pending")]

        return table_scoped + globals_

    def _rejected_instances_for_table(self, table_asset: Any) -> List[Any]:
        """Rejected instances scoped to this table — used only to resolve
        their definitions for similarity-matching, never shown to Claude as
        'already active/pending' (that would misrepresent them as running)."""
        _, table_scoped = storage.list_instances(
            database_name=table_asset.database_name,
            schema_name=table_asset.schema_name,
            table_name=table_asset.table_name,
            status="rejected",
            limit=1000,
        )
        return table_scoped

    # ── Deterministic candidate generation ─────────────────────────────────

    def _all_signal_ids(self, profiler_result: Dict[str, Any]) -> List[dict]:
        """Every signal_id Claude is expected to address in signals_evaluated
        — PK-shaped-column candidates (regardless of whether they're already
        violated — even a currently-unique key is worth a forced check-in)
        and freshness signals. Used both to build the prompt section and to
        compute signals_missed after the response comes back."""
        signals = []
        for cand in profiler_result.get("pk_shaped_candidates", []):
            signals.append({"signal_id": f"uniqueness:{cand['column']}", **cand})
        for sig in profiler_result.get("freshness_signals", []):
            signals.append(sig)
        return signals

    def _build_deterministic_candidates(
        self,
        table_asset: Any,
        profiler_result: Dict[str, Any],
        relationship_catalog: List[Any],
        existing_instances: List[Any],
    ) -> List[dict]:
        """Objective facts verified against live data — uniqueness violations
        on PK-shaped columns, and confirmed cross-table orphan relationships
        — become candidate dicts in the SAME shape _process_candidate expects
        from an LLM response, so they run through the identical fingerprint/
        execute/validate/human-review path. Freshness is deliberately NOT
        included here (see class docstring / system prompt) — a staleness
        threshold is a business judgment call, not an objective fact."""
        candidates: List[dict] = []
        existing_columns_with_uniqueness = {
            inst.target_config.get("column")
            for inst in existing_instances
            if (inst.target_config or {}).get("column")
        }
        # A column matching the PK-shape regex is NOT a uniqueness candidate
        # if it's a confirmed foreign key on THIS table — a transactions
        # table having many rows per customer is normal, not a defect.
        # relationship_discovery's live orphan-rate verification is a fact,
        # not a guess, so it's the authority here whenever the referenced
        # table exists in-schema to be discovered against. A PK-shaped
        # column whose referenced table isn't in this schema (so no
        # relationship candidate could ever be built for it) still gets
        # proposed — a human reviews every deterministic proposal anyway,
        # so a rare false positive here is a one-click skip, not a silent
        # wrong action.
        confirmed_fk_columns = {
            rel.from_column for rel in relationship_catalog
            if rel.from_table == table_asset.table_name and rel.status == "confirmed"
        }

        for cand in profiler_result.get("pk_shaped_candidates", []):
            column = cand["column"]
            if cand.get("is_unique") is not False:
                continue  # only propose when live data actually shows duplicates
            if column in existing_columns_with_uniqueness:
                continue
            if column in confirmed_fk_columns:
                continue  # verified: this column references another table, not this table's own key
            candidates.append({
                "definition_id": None,
                "new_definition": None,
                "template_shape": "uniqueness",
                "scope": "column",
                "column_name": column,
                "threshold_config": {},
                "draft_sql": None,
                "severity": "high",
                "violation_detected": True,
                "violation_evidence": (
                    f"{cand['duplicate_rows']} duplicate value(s) among "
                    f"{cand['non_null_total']} non-null rows in a PK-shaped column "
                    f"({cand['distinct']} distinct values)."
                ),
                "rationale": (
                    f"{column} matches common primary-key naming conventions but its "
                    f"live data has {cand['duplicate_rows']} duplicated value(s) — a real "
                    "integrity risk for joins, GROUP BY, and deduplication."
                ),
            })

        for rel in relationship_catalog:
            if rel.status != "confirmed" or rel.confidence != "verified":
                continue
            if rel.from_table != table_asset.table_name:
                continue
            if not rel.orphan_rate:
                continue
            candidates.append({
                "definition_id": None,
                "new_definition": None,
                "template_shape": "referential_integrity",
                "scope": "cross_table",
                "column_name": rel.from_column,
                "cross_table_ref": {
                    "ref_database": table_asset.database_name,
                    "ref_schema": table_asset.schema_name,
                    "ref_table": rel.to_table,
                    "ref_column": rel.to_column,
                },
                "threshold_config": {},
                "draft_sql": None,
                "severity": "high",
                "violation_detected": True,
                "violation_evidence": (
                    f"{rel.sample_orphans} of {rel.sample_total} rows have a "
                    f"{rel.from_column} value with no matching row in "
                    f"{rel.to_table}.{rel.to_column} ({rel.orphan_rate:.1%} orphan rate)."
                ),
                "rationale": (
                    f"{rel.from_column} is a verified foreign key into {rel.to_table} — "
                    "live data shows orphaned references, breaking referential integrity."
                ),
            })

        return candidates

    # ── Candidate processing (fingerprint dedup) ──────────────────────────

    def _build_target_config(self, candidate: dict, scope: str) -> dict:
        if scope == "column":
            column_name = candidate.get("column_name")
            return {"column": column_name} if column_name else {}
        if scope == "multi_column":
            return {"columns": candidate.get("columns") or []}
        if scope == "cross_table":
            ref = candidate.get("cross_table_ref") or {}
            target = {"column": candidate.get("column_name")}
            target.update(ref)
            return target
        return {}

    def _build_rule_sql(
        self, candidate: dict, table_asset: Any, target_config: dict,
    ) -> tuple[Optional[str], Optional[dict]]:
        """Returns (rule_sql, threshold_config) if the candidate resolves to
        real, validated SQL — or (None, None) if it can't (unknown template
        shape, missing params, or validation failure). Callers must discard
        a candidate that gets (None, None) back — a check with no rule_sql
        can never actually run, so it's worse than not proposing it at all.

        For draft_sql (Claude-authored), one repair attempt is made when
        static validation fails: Claude is shown the exact error and asked to
        fix only the SQL, not re-derive the whole proposal.
        """
        threshold_config = candidate.get("threshold_config") or {}
        template_shape = candidate.get("template_shape")
        allowed_tables = [
            f"{table_asset.database_name}.{table_asset.schema_name}.{table_asset.table_name}".upper()
        ]
        # cross_table checks (referential_integrity) legitimately reference a
        # second table — the reference target — so it must be in the allow-
        # list too, or validate_sql rejects every such check regardless of
        # who proposed it (LLM or deterministic candidate).
        cross_table_ref = candidate.get("cross_table_ref") or {}
        if cross_table_ref.get("ref_database") and cross_table_ref.get("ref_schema") and cross_table_ref.get("ref_table"):
            allowed_tables.append(
                f"{cross_table_ref['ref_database']}.{cross_table_ref['ref_schema']}.{cross_table_ref['ref_table']}".upper()
            )

        if template_shape:
            try:
                sql = render_template(
                    template_shape,
                    table_asset.database_name, table_asset.schema_name, table_asset.table_name,
                    target_config, threshold_config,
                )
            except (ValueError, KeyError) as e:
                logger.warning(f"[RuleIntelligence] Template '{template_shape}' failed to render: {e}")
                return None, None
            result = validate_sql(sql, allowed_tables=allowed_tables)
            if not result.is_valid:
                logger.warning(f"[RuleIntelligence] Rendered template SQL failed validation: {result.errors}")
                return None, None
            return sql, threshold_config

        draft_sql = candidate.get("draft_sql")
        if draft_sql:
            result = validate_sql(draft_sql, allowed_tables=allowed_tables)
            if result.is_valid:
                return draft_sql, threshold_config

            # ── SQL repair loop ──────────────────────────────────────────────
            # Static validation failed. Ask Claude to fix the specific error
            # rather than discarding a proposal whose intent was correct.
            errors_text = "; ".join(result.errors)
            logger.info(f"[RuleIntelligence] draft_sql failed validation ({errors_text}) — attempting repair")
            repaired = self._repair_draft_sql(
                draft_sql=draft_sql,
                errors=errors_text,
                table_asset=table_asset,
                candidate=candidate,
                allowed_tables=allowed_tables,
            )
            if repaired:
                return repaired, threshold_config
            logger.warning(f"[RuleIntelligence] SQL repair failed — discarding candidate")
            return None, None

        return None, None

    def _repair_draft_sql(
        self,
        draft_sql: str,
        errors: str,
        table_asset: Any,
        candidate: dict,
        allowed_tables: list,
        max_attempts: int = 2,
    ) -> Optional[str]:
        """Ask Claude to fix a draft_sql that failed static validation.
        Returns the repaired SQL string if it passes, else None.
        Uses a lightweight non-thinking call — this is a targeted fix, not
        a full reasoning task."""
        fqn = table_asset.fqn
        check_name = (
            (candidate.get("new_definition") or {}).get("name")
            or candidate.get("rationale", "")[:60]
            or "unnamed check"
        )
        repair_system = (
            "You are a Snowflake SQL expert. Fix the SQL so it passes the "
            "stated validation rules. Return ONLY the corrected SQL — no "
            "explanation, no markdown fences, no prose."
        )
        for attempt in range(max_attempts):
            repair_prompt = (
                f"The following Snowflake SQL for check '{check_name}' on table {fqn} "
                f"failed validation with error(s): {errors}\n\n"
                f"Rules:\n"
                f"- Must be a single SELECT statement\n"
                f"- Must return exactly two columns named FAILED_COUNT and TOTAL_COUNT\n"
                f"- May only reference these tables: {', '.join(allowed_tables)}\n"
                f"- No DML, no CTEs that reference other tables, no LIMIT\n\n"
                f"Original SQL:\n{draft_sql}\n\n"
                f"Return only the corrected SQL."
            )
            try:
                fixed_sql = ask_claude(repair_prompt, system=repair_system, max_tokens=2000).strip()
                # Strip any accidental markdown fences the model added
                fixed_sql = re.sub(r"^```[a-z]*\n?", "", fixed_sql, flags=re.IGNORECASE)
                fixed_sql = re.sub(r"\n?```$", "", fixed_sql).strip()
                check = validate_sql(fixed_sql, allowed_tables=allowed_tables)
                if check.is_valid:
                    logger.info(f"[RuleIntelligence] SQL repair succeeded on attempt {attempt + 1}")
                    return fixed_sql
                errors = "; ".join(check.errors)
                draft_sql = fixed_sql  # feed repaired version into next attempt
                logger.debug(f"[RuleIntelligence] Repair attempt {attempt + 1} still invalid: {errors}")
            except Exception as e:
                logger.warning(f"[RuleIntelligence] SQL repair attempt {attempt + 1} exception: {e}")
                break
        return None

    def _execute_check_sql(self, rule_sql: str) -> Optional[tuple[int, int]]:
        """Run a validated check SQL now and return (failed_count,
        total_count), or None if execution errors (e.g. TRY_CAST issue not
        caught by static validation). Only ever called with SELECT-only,
        single-statement, table-scoped SQL that already passed validate_sql().

        Bounded by a server-side statement timeout: validate_sql() proves the
        SQL is SELECT-only and table-scoped, but NOT that it's cheap — an
        AI-authored draft_sql with an accidental cartesian join passes
        validation yet could scan billions of rows. The timeout caps that
        blast radius; a query that exceeds it is cancelled and the candidate
        discarded (execution returned None) rather than proposed."""
        try:
            rows = sf_session.query(rule_sql, timeout=_CHECK_SQL_TIMEOUT_SECONDS)
            if not rows:
                return None
            row = rows[0]
            failed = row.get("FAILED_COUNT")
            total = row.get("TOTAL_COUNT")
            if failed is None or total is None:
                logger.warning(f"[RuleIntelligence] Check SQL did not return FAILED_COUNT/TOTAL_COUNT: {row}")
                return None
            return int(failed), int(total)
        except Exception as e:
            logger.warning(f"[RuleIntelligence] Check SQL execution failed: {e}")
            return None

    def _ground_accepted_values(self, candidate: dict, target_config: dict) -> dict:
        """Mechanically trims any accepted_values list down to values this
        column's profiled data actually contains — a general-purpose
        validation gate on the LLM's own output (works identically for any
        low-cardinality column on any table), not a per-table patch. Values
        not observed are dropped and logged; if nothing survives, the render
        step below will reject the candidate outright (an accepted_values
        check with no accepted values left is not a real check)."""
        threshold_config = candidate.get("threshold_config") or {}
        accepted = threshold_config.get("accepted_values")
        column = target_config.get("column")
        if not accepted or not column:
            return candidate
        closed_set = self._closed_set_columns.get(column)
        if not closed_set:
            return candidate  # not profiled as low-cardinality — nothing to ground against
        observed = {str(v) for v in closed_set.get("values", [])}
        grounded = [v for v in accepted if str(v) in observed]
        dropped = [v for v in accepted if str(v) not in observed]
        if dropped:
            logger.debug(
                f"[RuleIntelligence] Trimmed accepted_values for {column}: "
                f"dropped {dropped} (not observed in live data), kept {grounded}"
            )
        new_candidate = dict(candidate)
        new_candidate["threshold_config"] = {**threshold_config, "accepted_values": grounded}
        return new_candidate

    def _process_candidate(
        self,
        candidate: dict,
        table_asset: Any,
        run_id: str,
        existing_definitions: List[Any],
        source: str = "llm",
    ) -> Optional[dict]:
        scope = (candidate.get("scope") or "table").lower()
        column_name = candidate.get("column_name")
        target_config = self._build_target_config(candidate, scope)
        candidate = self._ground_accepted_values(candidate, target_config)

        definition_id = candidate.get("definition_id")
        new_definition_data = candidate.get("new_definition")
        template_shape = candidate.get("template_shape")

        is_new_definition = False
        definition = None

        if definition_id:
            definition = storage.get_definition(definition_id)
            if not definition:
                logger.warning(f"[RuleIntelligence] Claude referenced unknown definition_id={definition_id}")
                return None
        elif template_shape and storage.get_definition_by_template_shape(template_shape):
            # Deterministic backstop — checked BEFORE the fuzzy-similarity
            # path so it works even for a candidate Claude never attaches a
            # definition_id to. A canonical definition for this shape already
            # exists system-wide; always reuse it instead of letting a new
            # per-table/per-column duplicate spawn. The candidate's own
            # name/description (if any) is discarded — its business-specific
            # rationale is preserved separately via RULE_INSTANCES.RATIONALE.
            definition = storage.get_definition_by_template_shape(template_shape)
        elif new_definition_data:
            # Check if this "new" concept actually matches an existing one by
            # name/description similarity — Claude sometimes re-describes a
            # concept that already has a definition under a different id.
            matched = self._find_similar_definition(new_definition_data, existing_definitions)
            if matched:
                definition = matched
            else:
                definition = None  # not persisted yet — staged below
                is_new_definition = True
        else:
            return None

        severity = candidate.get("severity") or (definition.default_severity if definition else "medium")
        if severity not in VALID_SEVERITIES:
            severity = "medium"

        # sql_template checks need rule_sql rendered/validated/executed for
        # THIS SPECIFIC target — whether the definition is brand new or a
        # reused canonical/matched one, because rule_sql/threshold_config are
        # per-instance, not per-definition (a shared "not_null" definition
        # still needs a fresh SELECT for each new column it's applied to).
        # python_handler reuse needs none: its dispatch is by handler_key
        # against already-fetched metadata, settled when the definition was
        # first approved, not per-target.
        needs_rule_sql = definition is None or definition.check_kind == "sql_template"
        effective_template_shape = template_shape or (definition.template_shape if definition else None)

        rule_sql = None
        violated_override: Optional[bool] = None
        evidence_override: Optional[str] = None
        threshold_config = candidate.get("threshold_config") or {}

        if needs_rule_sql:
            render_candidate = {**candidate, "template_shape": effective_template_shape}
            rule_sql, threshold_config = self._build_rule_sql(render_candidate, table_asset, target_config)
            label = new_definition_data.get("name", "?") if new_definition_data else (definition.name if definition else "?")
            if not rule_sql:
                logger.info(
                    f"[RuleIntelligence] Discarding candidate '{label}' — "
                    "no valid template_shape or draft_sql resolved to executable SQL"
                )
                return None
            # Actually run it now — real query result beats Claude's
            # self-reported violation_detected. If execution fails (bad
            # column ref, type mismatch not caught by validation, etc.),
            # discard rather than propose an unrunnable check.
            executed = self._execute_check_sql(rule_sql)
            if executed is None:
                logger.info(
                    f"[RuleIntelligence] Discarding candidate '{label}' — "
                    "rule_sql failed to execute against Snowflake"
                )
                return None
            failed_count, total_count = executed
            violated_override = failed_count > 0
            evidence_override = f"{failed_count} of {total_count} rows fail this check"

        # Fingerprint requires a definition_id — for a genuinely new
        # definition we don't have one yet, so fingerprint against a stable
        # placeholder derived from the proposed name instead.
        fp_definition_key = definition.id if definition else f"new:{new_definition_data.get('name', '')}"
        fingerprint = compute_fingerprint(
            definition_id=fp_definition_key,
            scope=scope,
            database_name=table_asset.database_name,
            schema_name=table_asset.schema_name,
            table_name=table_asset.table_name,
            target_config=target_config,
            threshold_config=threshold_config,
        )

        existing_match = storage.get_instance_by_fingerprint(fingerprint) if definition else None

        if existing_match:
            if existing_match.status == "active":
                return {"suppressed": True, "reason": "already_active", "fingerprint": fingerprint,
                        "candidate": candidate}
            if existing_match.status == "pending":
                return {"suppressed": True, "reason": "already_pending", "fingerprint": fingerprint,
                        "candidate": candidate, "existing_instance_id": existing_match.id}
            if existing_match.status == "rejected":
                new_evidence = candidate.get("violation_evidence") or ""
                old_reason = existing_match.rejection_reason or ""
                if new_evidence and word_overlap_score(new_evidence, old_reason) < 0.3:
                    logger.info(
                        f"[RuleIntelligence] Re-proposing previously rejected fingerprint "
                        f"{fingerprint[:12]} — new evidence differs from rejection reason"
                    )
                else:
                    return {"suppressed": True, "reason": "previously_rejected", "fingerprint": fingerprint,
                            "candidate": candidate}

        # Real execution result overrides Claude's self-reported claim for
        # newly-executed checks — for reused definitions there's no fresh
        # execution here (that happens through RuleEngine at findings time),
        # so trust Claude's read of whether the existing check applies.
        violated = violated_override if violated_override is not None else bool(candidate.get("violation_detected"))
        evidence_text = evidence_override or candidate.get("violation_evidence") or ""

        return {
            "suppressed": False,
            "kind": "new" if is_new_definition else "reuse",
            "source": source,
            "fingerprint": fingerprint,
            "definition": definition,  # None if is_new_definition
            "new_definition_data": new_definition_data if is_new_definition else None,
            "template_shape": effective_template_shape,
            "scope": scope,
            "target_config": target_config,
            "threshold_config": threshold_config,
            "rule_sql": rule_sql,  # set whenever needs_rule_sql was true
            "column_name": column_name,
            "severity": severity,
            "violated": violated,
            "evidence": evidence_text,
            "rationale": candidate.get("rationale", ""),
            "source_run_id": run_id,
        }

    def _find_similar_definition(self, new_def: dict, existing_definitions: List[Any]) -> Optional[Any]:
        name = new_def.get("name", "")
        desc = new_def.get("description", "")
        combined = f"{name} {desc}"
        best_score, best = 0.0, None
        for d in existing_definitions:
            score = word_overlap_score(combined, f"{d.name} {d.description or ''}")
            if score > best_score:
                best_score, best = score, d
        return best if best_score >= DEFAULT_SIMILARITY_THRESHOLD else None

    # ── Classification decision helpers (consumed by coordinator) ────────

    def get_keep_running_ids(self, classification: dict) -> Set[str]:
        return {
            def_id for def_id, d in classification.get("definitions_evaluated", {}).items()
            if d.get("keep_running", True)
        }

    def get_skip_ids(self, classification: dict) -> Set[str]:
        return {
            def_id for def_id, d in classification.get("definitions_evaluated", {}).items()
            if not d.get("keep_running", True)
        }

    def get_severity_override(self, classification: dict, definition_id: str) -> Optional[str]:
        d = classification.get("definitions_evaluated", {}).get(definition_id, {})
        return d.get("severity_override") or None

    # ── Self-critique pass ────────────────────────────────────────────────

    _CRITIQUE_SYSTEM = (
        "You are a ruthless data quality review lead. Your job is to cut weak "
        "rule proposals before they waste reviewer time. Score each proposal "
        "honestly and drop anything you would reject yourself."
    )

    def _self_critique_proposals(
        self,
        proposals: list,
        table_asset: Any,
        column_stats_text: str,
        min_score: int = 3,
    ) -> list:
        """Second-pass call: Claude reads its own proposals and scores each
        1-5 on three dimensions:
          evidence   — how well backed by real column stats / live data (not guesses)
          impact     — how much real data quality harm a violation would cause
          approval   — how likely a human reviewer who knows this table would approve

        Any proposal with mean score < min_score (default 3.0) is dropped here,
        before it enters _process_candidate and hits fingerprinting/SQL execution.
        Deterministic candidates never enter this path (they're already processed).

        Uses a fast non-thinking call — this is a scoring/filtering task, not
        an exploration task.
        """
        if not proposals:
            return proposals

        proposals_json = json.dumps(proposals, indent=2)
        critique_prompt = (
            f"Table: {table_asset.fqn}\n"
            f"Row count: {table_asset.row_count or 'unknown'}\n\n"
            f"Column statistics (from live data):\n{column_stats_text}\n\n"
            f"Proposed rule instances to score:\n{proposals_json}\n\n"
            "For each proposal (identified by its array index 0-based), score it on:\n"
            "  evidence: 1-5 — how well is it backed by the column stats above? "
            "(5 = directly contradicts a stat, 1 = pure guess with no stat support)\n"
            "  impact: 1-5 — how serious would a violation be for data consumers? "
            "(5 = critical join keys / PII / financial amounts, 1 = cosmetic)\n"
            "  approval: 1-5 — how likely would an experienced data engineer approve this? "
            "(5 = obvious and unambiguous, 1 = highly speculative or already implicit)\n\n"
            "Respond with JSON only:\n"
            '{"scores": [{"index": 0, "evidence": <1-5>, "impact": <1-5>, "approval": <1-5>, '
            '"drop_reason": null or "one sentence why this is weak"}, ...]}'
        )
        try:
            raw = ask_claude(critique_prompt, system=self._CRITIQUE_SYSTEM, max_tokens=4000)
            raw = raw.strip()
            raw = re.sub(r"^```[a-z]*\n?", "", raw, flags=re.IGNORECASE)
            raw = re.sub(r"\n?```$", "", raw).strip()
            critique = json.loads(raw)
            scores_by_index = {s["index"]: s for s in critique.get("scores", [])}
        except Exception as e:
            logger.warning(f"[RuleIntelligence] Self-critique parse failed ({e}) — keeping all proposals")
            return proposals

        kept = []
        for i, proposal in enumerate(proposals):
            score_entry = scores_by_index.get(i)
            if not score_entry:
                kept.append(proposal)
                continue
            mean = (score_entry.get("evidence", 3) + score_entry.get("impact", 3) + score_entry.get("approval", 3)) / 3
            if mean >= min_score:
                kept.append(proposal)
            else:
                reason = score_entry.get("drop_reason") or f"mean score {mean:.1f} < {min_score}"
                # new_definition is often explicitly null (see prompt schema), so
                # `.get('new_definition', {})` returns None, not {} — guard with `or {}`.
                label = (proposal.get("new_definition") or {}).get("name") or proposal.get("definition_id", "?")
                logger.info(
                    f"[RuleIntelligence] Self-critique dropped proposal[{i}] "
                    f"'{label}' "
                    f"— {reason} (evidence={score_entry.get('evidence')}, "
                    f"impact={score_entry.get('impact')}, approval={score_entry.get('approval')})"
                )

        dropped = len(proposals) - len(kept)
        if dropped:
            logger.info(f"[RuleIntelligence] Self-critique: kept {len(kept)}/{len(proposals)} proposals, dropped {dropped}")
        return kept

    # ── Formatting helpers ────────────────────────────────────────────────

    def _format_past_context(self, table_asset: Any) -> str:
        """Retrieve synthesised feedback memo + raw review lessons + past
        intelligence logs and format them as grounded guidance.

        Injection order (most actionable first):
          1. Synthesised feedback memo — cross-run patterns Claude distilled
             from many human decisions, e.g. "always approve non-negative on
             AMOUNT columns; always reject column-comment checks here."
          2. Raw approve/reject lessons — individual human decisions.
          3. Past thinking blobs from similar tables.
        """
        parts = []

        # ── Synthesised feedback memo (highest signal) ───────────────────
        try:
            bare_table = table_asset.fqn.upper().split(".")[-1]
            # We don't know table_type yet (that's Claude's output), so we try
            # common types; the memo for the right type will have higher
            # confidence and the others will be absent.
            memo = None
            for ttype in ["fact", "dimension", "staging", "audit", "reference", "config", "unknown"]:
                m = storage.get_feedback_memo(bare_table, ttype)
                if m and m.get("confidence", 0) >= 40:
                    memo = m
                    break
        except Exception:
            memo = None

        if memo:
            memo_lines = [f"  SYNTHESISED FEEDBACK MEMO (confidence={memo.get('confidence', '?')}%, based on {memo.get('_lesson_count', '?')} past reviews):"]
            for pat in memo.get("always_approve", [])[:6]:
                memo_lines.append(f"  ✓ ALWAYS APPROVE: {pat}")
            for pat in memo.get("always_reject", [])[:8]:
                memo_lines.append(f"  ✗ ALWAYS REJECT:  {pat}")
            for col, advice in list((memo.get("column_advice") or {}).items())[:6]:
                memo_lines.append(f"  COLUMN {col}: {advice}")
            if memo.get("table_type_notes"):
                memo_lines.append(f"  NOTE: {memo['table_type_notes'][:200]}")
            parts.append("\n".join(memo_lines))

        # ── Raw review lessons ───────────────────────────────────────────
        try:
            lessons = storage.get_review_lessons_for_table(table_asset.fqn, limit=20)
        except Exception:
            lessons = []

        if lessons:
            approved = [l for l in lessons if l["verdict"] == "approved"]
            rejected = [l for l in lessons if l["verdict"] == "rejected"]
            lesson_lines = ["  LESSONS FROM PRIOR HUMAN REVIEWS ON THIS TABLE:"]
            for l in approved[:5]:
                col = f" on column {l['column']}" if l.get("column") else ""
                lesson_lines.append(
                    f"  ✓ APPROVED: {l['check_concept']}{col} — \"{l['reason']}\""
                )
            for l in rejected[:10]:
                col = f" on column {l['column']}" if l.get("column") else ""
                lesson_lines.append(
                    f"  ✗ REJECTED: {l['check_concept']}{col} — \"{l['reason']}\""
                )
            parts.append("\n".join(lesson_lines))

        # ── Past thinking blobs from similar tables ──────────────────────
        try:
            past_logs = storage.search_similar_intelligence(table_asset.fqn, limit=3)
        except Exception:
            past_logs = []

        for log in past_logs:
            outcome = f"approved={log.approved_count}, rejected={log.rejected_count}"
            parts.append(
                f"  Table: {log.table_fqn}  type={log.table_type}  {outcome}\n"
                f"  Reasoning: {(log.thinking or '')[:400]}"
            )

        if not parts:
            return "  (no past intelligence available yet — this is the first scan of a similar table)"
        return "\n\n".join(parts)

    def _execute_sample_tool(self, table_asset: Any, inputs: dict) -> str:
        """Safe executor for the get_sample_rows tool call from Claude.

        Enforces:
        - Only the target table can be queried (rejects any other fqn)
        - WHERE clause is stripped of dangerous keywords
        - Row cap of _SAMPLE_MAX_ROWS
        - SELECT-only (no DML)
        """
        fqn = table_asset.fqn
        columns = inputs.get("columns") or []
        where_clause = (inputs.get("where_clause") or "").strip()
        limit = min(int(inputs.get("limit") or 10), _SAMPLE_MAX_ROWS)
        reason = inputs.get("reason", "")

        logger.info(f"[RuleIntelligence] sample tool: cols={columns} where={where_clause!r} limit={limit} reason={reason!r}")

        # Safety: reject anything that could turn the filter into a second
        # statement or exfiltration query.
        _FORBIDDEN_WORDS = {"insert", "update", "delete", "drop", "truncate", "create",
                            "alter", "merge", "execute", "exec", "call", "grant", "revoke", "select"}
        where_lower = where_clause.lower()
        # Semicolons are statement terminators — check with plain 'in', not \b
        if ";" in where_lower:
            return "Rejected: WHERE clause contains forbidden keyword ';'."
        for kw in _FORBIDDEN_WORDS:
            if re.search(r'\b' + kw + r'\b', where_lower):
                return f"Rejected: WHERE clause contains forbidden keyword '{kw}'."

        col_expr = ", ".join(columns[:_SAMPLE_DEFAULT_COLS]) if columns else "*"
        sql = f"SELECT {col_expr} FROM {fqn}"
        if where_clause:
            sql += f" WHERE {where_clause}"
        sql += f" LIMIT {limit}"

        try:
            rows = sf_session.query(sql)
            if not rows:
                return "(no rows matched)"
            headers = list(rows[0].keys())[:_SAMPLE_DEFAULT_COLS]
            lines = [" | ".join(headers), "-" * 60]
            for row in rows:
                vals = [str(row.get(h, ""))[:30] for h in headers]
                lines.append(" | ".join(vals))
            return "\n".join(lines)
        except Exception as e:
            logger.warning(f"[RuleIntelligence] sample tool query failed: {e}")
            return f"Query failed: {e}"

    def _format_column_stats(self, column_stats: Dict[str, dict]) -> str:
        """Formats DeterministicProfilerAgent's column_stats for the prompt —
        the querying itself now happens once, upstream in profiler_agent.py,
        reused for both this text and deterministic candidate generation."""
        if not column_stats:
            return "  (stats unavailable)"
        lines = []
        for name, stat in column_stats.items():
            top_values = ", ".join(f"{tv['value']!r}({tv['count']})" for tv in stat.get("top_values", []))
            lines.append(
                f"  {name:<25} null%={stat['null_pct']:<6} distinct={stat['distinct']:<8} "
                f"top_values=[{top_values}]"
            )
        return "\n".join(lines)

    def _format_signals(self, profiler_result: Dict[str, Any], deterministic_proposed: List[dict]) -> str:
        """Every signal Claude must acknowledge in signals_evaluated — see
        system prompt. Uniqueness/referential-integrity signals already
        covered by a deterministic candidate this run are marked so Claude
        doesn't duplicate them; freshness signals are always open for Claude
        to decide on, since a staleness threshold is a judgment call."""
        proposed_columns = {
            p["target_config"].get("column")
            for p in deterministic_proposed
            if p.get("target_config")
        }
        lines = []
        for cand in profiler_result.get("pk_shaped_candidates", []):
            column = cand["column"]
            status = "VIOLATED — duplicates found" if cand.get("is_unique") is False else "currently unique"
            already = " [already proposed this run, do not duplicate]" if column in proposed_columns else ""
            lines.append(
                f"  signal_id=uniqueness:{column}  column={column}  {status} "
                f"({cand['distinct']} distinct / {cand['non_null_total']} non-null){already}"
            )
        for sig in profiler_result.get("freshness_signals", []):
            lines.append(
                f"  signal_id={sig['signal_id']}  column={sig['column']} ({sig['data_type']})  "
                f"most recent value={sig['max_value']}  age={sig['age_days']} days"
            )
        return "\n".join(lines) if lines else "  (no deterministic signals for this table)"

    def _format_relationships(self, table_asset: Any, relationship_catalog: List[Any]) -> str:
        relevant = [
            r for r in relationship_catalog
            if r.from_table == table_asset.table_name and r.status == "confirmed"
        ]
        if not relevant:
            return "  (no verified cross-table relationships found for this table)"
        lines = []
        for r in relevant:
            orphan_note = f", orphan_rate={r.orphan_rate:.1%}" if r.orphan_rate else ""
            lines.append(
                f"  {r.from_column} -> {r.to_table}.{r.to_column}  "
                f"confidence={r.confidence}{orphan_note}"
            )
        return "\n".join(lines)

    def _format_closed_sets(self, closed_set_columns: Dict[str, dict]) -> str:
        if not closed_set_columns:
            return "  (no low-cardinality columns profiled for this table)"
        lines = []
        for name, info in closed_set_columns.items():
            values = ", ".join(repr(v) for v in info["values"][:50])
            lines.append(f"  {name} ({info['distinct_count']} distinct values): [{values}]")
        return "\n".join(lines)

    def _format_columns(self, column_assets: List[Any]) -> str:
        lines = []
        for col in column_assets:
            meta = col.raw_metadata or {}
            dtype    = meta.get("data_type", "UNKNOWN")
            nullable = meta.get("is_nullable", "Y")
            null_str = "NOT NULL" if str(nullable).upper() in ("N", "NO") else "nullable"
            comment  = col.comment or ""
            lines.append(
                f"  {col.column_name:<30} {dtype:<20} {null_str}"
                + (f'  -- "{comment}"' if comment else "")
            )
        return "\n".join(lines) if lines else "  (no columns)"

    def _format_definitions(self, definitions: List[Any]) -> str:
        if not definitions:
            return "  (library is empty)"
        lines = []
        for d in definitions:
            lines.append(
                f"  id={d.id} [{d.category}] {d.name} (approved {d.approval_count}x)"
                f"\n    {d.description[:120]}"
            )
        return "\n".join(lines)

    def _format_existing_instances(self, instances: List[Any], definitions: List[Any]) -> str:
        if not instances:
            return "  (none — this table has no active/pending checks yet)"
        by_id = {d.id: d for d in definitions}
        lines = []
        for inst in instances:
            d = by_id.get(inst.definition_id)
            name = d.name if d else inst.definition_id
            target = inst.target_config.get("column") if inst.target_config else None
            target_str = f"column={target}" if target else "table-level"
            lines.append(f"  definition_id={inst.definition_id} \"{name}\" [{inst.status}] {target_str}")
        return "\n".join(lines)

    def _call_model(self, prompt: str, table_asset: Any) -> tuple:
        """
        Returns (text, thinking, tool_calls).

        Primary path: agentic loop with adaptive thinking + get_sample_rows tool.
        Bedrock redacts thinking block content even when adaptive thinking is
        enabled, so we make a second call after the main one to reconstruct the
        reasoning explicitly — see _generate_thinking().
        Falls back to ask_claude (no tools) if the agentic call fails.
        """
        def tool_executor(name: str, inputs: dict) -> str:
            if name == "get_sample_rows":
                return self._execute_sample_tool(table_asset, inputs)
            return f"Unknown tool: {name}"

        tool_calls = []
        try:
            result = ask_claude_agentic(
                prompt,
                system=SYSTEM_PROMPT,
                tools=_SAMPLE_TOOL_SCHEMA,
                tool_executor=tool_executor,
                max_tokens=24000,
                effort="high",
                max_tool_rounds=5,
            )
            text = result["text"]
            tool_calls = result["tool_calls"]
        except Exception as e:
            logger.warning(f"[RuleIntelligence] Agentic call failed ({e}), falling back to standard Bedrock")
            try:
                text = ask_claude(prompt, system=SYSTEM_PROMPT, max_tokens=32000)
            except Exception as e2:
                logger.error(f"[RuleIntelligence] Bedrock fallback also failed: {e2}")
                raise

        thinking = self._generate_thinking(prompt, text)
        return text, thinking, tool_calls

    _THINKING_SYSTEM = (
        "You are a senior data quality architect. You have just completed a "
        "rule intelligence analysis for a Snowflake table. Write out your full "
        "reasoning process in depth — not a summary or bullet points, but your "
        "actual chain of thought in continuous prose: what signals you examined "
        "and what each told you, the trade-offs you weighed, why each rule "
        "proposal is justified by the specific data characteristics, what you "
        "considered but ruled out and why, and any caveats or uncertainties. "
        "Write entirely in first person. Be deeply specific to the actual "
        "statistics and signals in the analysis — avoid generic advice."
    )

    def _generate_thinking(self, original_prompt: str, model_output: str) -> str:
        """
        Ask Claude to write out its full reasoning chain for the analysis it
        just completed. Bedrock doesn't expose thinking block content, so we
        reconstruct it from the prompt + output as a second call.
        Returns "" on any failure so the pipeline is never blocked.
        """
        thinking_prompt = (
            f"You were given this data quality analysis task:\n\n"
            f"---\n{original_prompt}\n---\n\n"
            f"You produced this output:\n\n"
            f"---\n{model_output}\n---\n\n"
            f"Now write your complete, detailed reasoning process for how you "
            f"arrived at that output. You must cover every significant decision:\n"
            f"- How you classified the table type, which signals drove that "
            f"conclusion, and what alternative classifications you considered\n"
            f"- For each definition you evaluated: why it was or wasn't relevant "
            f"to this table's specific business purpose and data characteristics\n"
            f"- Which column statistics stood out and exactly how they shaped "
            f"your proposals (reference actual numbers from the stats)\n"
            f"- For every proposed rule instance: the precise reasoning — why "
            f"this column, why this threshold, why this severity, what failure "
            f"mode it guards against, and whether current data already violates it\n"
            f"- What you considered proposing but decided against, and why those "
            f"were weaker than what you kept\n"
            f"Write as continuous prose — no headers, no bullet points. "
            f"Be specific to this table, not generic."
        )
        try:
            thinking = ask_claude(
                thinking_prompt,
                system=self._THINKING_SYSTEM,
                max_tokens=4000,
            )
            logger.info(f"[RuleIntelligence] Generated thinking ({len(thinking)} chars)")
            return thinking
        except Exception as e:
            logger.warning(f"[RuleIntelligence] Thinking generation failed ({e}) — continuing without")
            return ""

    @staticmethod
    def _extract_json(text: str) -> dict:
        match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
        if match:
            try:
                result = json.loads(match.group(1))
                if "new_instances" in result or "table_type" in result:
                    return result
            except Exception:
                pass
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                result = json.loads(match.group(0))
                if "new_instances" in result or "table_type" in result:
                    return result
            except Exception:
                pass
        try:
            return json.loads(text.strip())
        except Exception:
            pass
        logger.warning(f"[RuleIntelligence] Could not extract JSON. Raw response (first 500 chars): {text[:500]}")
        return {}
