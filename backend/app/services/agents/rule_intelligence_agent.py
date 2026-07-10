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
from app.services.claude_client import ask_claude
from app.services.sql_validation import validate_sql
from app.services.rule_sql_templates import TEMPLATE_SHAPES, render_template

logger = logging.getLogger(__name__)

_TEMPLATE_SHAPES_DOC = "\n".join(
    f"  - {name} (scope={spec['scope']}, needs threshold_config keys: {spec['params'] or 'none'})"
    for name, spec in TEMPLATE_SHAPES.items()
)

SYSTEM_PROMPT = f"""You are a senior Snowflake data quality architect.

Given a table schema, real column statistics computed from the live data,
sample rows, and the library of check DEFINITIONS this system already knows
about (plus what is already running or pending on this exact table), you
will:
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

Respond with valid JSON only — no markdown, no prose outside the JSON.
Be thorough, but NEVER propose an instance that duplicates something already
active or pending on this table (shown to you explicitly below) — assume the
system will silently drop exact duplicates, so re-proposing them wastes a
slot a genuinely new check could use instead.
Focus new instances on: value constraints, referential patterns, naming
standards for this domain, null semantics, data freshness, business key
uniqueness patterns."""

USER_PROMPT_TEMPLATE = """Table: {fqn}
Row count: {row_count}
Owner: {owner}

=== SCHEMA ===
{columns}

=== COLUMN STATISTICS (computed from the live data — use these, not guesses) ===
{column_stats}

=== SAMPLE DATA (first 5 rows) ===
{sample_data}

=== DEFINITION LIBRARY (active check concepts this system already knows) ===
{definitions}

=== ALREADY ACTIVE OR PENDING ON THIS TABLE (do NOT re-propose these) ===
{existing_instances}

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
WORD_STOP = {"a", "an", "the", "is", "are", "was", "were", "be", "been", "being", "have",
             "has", "had", "do", "does", "did", "will", "would", "could", "should", "may",
             "might", "must", "shall", "can", "need", "dare", "ought", "used", "to", "of",
             "in", "for", "on", "with", "at", "by", "from", "as", "into", "through", "this",
             "that", "these", "those", "it", "its", "and", "or", "but", "if", "than", "when",
             "where", "which", "who", "how", "all", "each", "every", "both", "rule", "check",
             "column", "table", "snowflake", "data", "quality", "should", "not", "no", "any"}


def _word_overlap_score(text1: str, text2: str) -> float:
    def words(t: str) -> set:
        return {w.lower() for w in re.findall(r"\w+", t) if w.lower() not in WORD_STOP and len(w) > 2}
    w1, w2 = words(text1), words(text2)
    if not w1 or not w2:
        return 0.0
    return len(w1 & w2) / min(len(w1), len(w2))


class RuleIntelligenceAgent:
    """
    Library-aware classifier + suggester. Single Claude call, results
    fingerprint-deduped against RULE_INSTANCES before ever reaching a human.
    """

    def __init__(self):
        self._severity_backup: Dict[str, str] = {}

    def run(
        self,
        table_asset: Any,
        column_assets: List[Any],
        existing_definitions: List[Any],
        run_id: str,
        profile: Optional[Dict[str, Any]] = None,
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
          - suppressed: list of dicts describing candidates dropped due to
              fingerprint match against active/rejected instances — logged,
              never shown to the human as new.

        When `profile` (from ProfilingAgent) is provided it is accepted for
        interface compatibility; harsh's architecture derives its own column
        statistics directly from the live data (see _fetch_column_stats).
        """
        logger.info(f"[RuleIntelligence] Analyzing {table_asset.fqn}")

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

        sample_data = self._fetch_sample(table_asset)
        column_stats_text = self._fetch_column_stats(table_asset, column_assets)
        columns_text = self._format_columns(column_assets)
        definitions_text = self._format_definitions(existing_definitions)
        existing_text = self._format_existing_instances(existing_instances, all_known_definitions)

        prompt = USER_PROMPT_TEMPLATE.format(
            fqn=table_asset.fqn,
            row_count=table_asset.row_count or "unknown",
            owner=table_asset.owner or "unknown",
            columns=columns_text,
            column_stats=column_stats_text,
            sample_data=sample_data,
            definitions=definitions_text,
            existing_instances=existing_text,
        )

        raw = self._call_model(prompt)
        parsed = self._extract_json(raw)

        # parse_warning surfaces to the UI (via the coordinator) so a truncated /
        # unparseable response is visible instead of silently degrading to
        # "0 AI rules, all rules kept active".
        parse_warning = None
        if not parsed:
            parse_warning = (
                f"LLM response unparseable ({len(raw or '')} chars) — kept all rules "
                f"active, generated no AI rules. Likely a truncated or refused response."
            )
            logger.error(f"[RuleIntelligence] {parse_warning}")
            parsed = {}

        classification = {
            "table_type":            parsed.get("table_type", "unknown"),
            "table_type_confidence": parsed.get("table_type_confidence", 50),
            "table_type_reason":     parsed.get("table_type_reason", ""),
            "definitions_evaluated": parsed.get("definitions_evaluated", {}),
            "parse_warning":         parse_warning,
        }

        # Normalize: fill missing decisions with default (keep_running=True)
        for inst in existing_instances:
            def_id = inst.definition_id
            if def_id not in classification["definitions_evaluated"]:
                classification["definitions_evaluated"][def_id] = {
                    "keep_running": True, "severity_override": None,
                    "reason": "Default — not explicitly re-evaluated",
                }

        proposed_instances = []
        suppressed = []
        for candidate in parsed.get("new_instances", []):
            result = self._process_candidate(candidate, table_asset, run_id, all_known_definitions)
            if result is None:
                continue
            if result.get("suppressed"):
                suppressed.append(result)
            else:
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
        can never actually run, so it's worse than not proposing it at all."""
        threshold_config = candidate.get("threshold_config") or {}
        template_shape = candidate.get("template_shape")
        allowed_tables = [
            f"{table_asset.database_name}.{table_asset.schema_name}.{table_asset.table_name}".upper()
        ]

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
            if not result.is_valid:
                logger.warning(f"[RuleIntelligence] Claude draft_sql failed validation: {result.errors}")
                return None, None
            return draft_sql, threshold_config

        return None, None

    def _execute_check_sql(self, rule_sql: str) -> Optional[tuple[int, int]]:
        """Run a validated check SQL now and return (failed_count,
        total_count), or None if execution errors (e.g. TRY_CAST issue not
        caught by static validation). Only ever called with SELECT-only,
        single-statement, table-scoped SQL that already passed validate_sql()."""
        try:
            rows = sf_session.query(rule_sql)
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

    def _process_candidate(
        self,
        candidate: dict,
        table_asset: Any,
        run_id: str,
        existing_definitions: List[Any],
    ) -> Optional[dict]:
        scope = (candidate.get("scope") or "table").lower()
        column_name = candidate.get("column_name")
        target_config = self._build_target_config(candidate, scope)

        definition_id = candidate.get("definition_id")
        new_definition_data = candidate.get("new_definition")

        if definition_id:
            definition = storage.get_definition(definition_id)
            if not definition:
                logger.warning(f"[RuleIntelligence] Claude referenced unknown definition_id={definition_id}")
                return None
            is_new_definition = False
        elif new_definition_data:
            # Check if this "new" concept actually matches an existing one by
            # name/description similarity — Claude sometimes re-describes a
            # concept that already has a definition under a different id.
            matched = self._find_similar_definition(new_definition_data, existing_definitions)
            if matched:
                definition = matched
                is_new_definition = False
            else:
                definition = None  # not persisted yet — staged below
                is_new_definition = True
        else:
            return None

        severity = candidate.get("severity") or (definition.default_severity if definition else "medium")
        if severity not in VALID_SEVERITIES:
            severity = "medium"

        # A genuinely new concept must resolve to real, executable SQL —
        # this is the fix for AI rules being a one-time opinion with no
        # handler behind them. Reusing an existing definition needs no new
        # SQL: its check_kind/handler_key or rule_sql was already settled
        # when it was first approved.
        rule_sql = None
        violated_override: Optional[bool] = None
        evidence_override: Optional[str] = None
        if is_new_definition:
            rule_sql, threshold_config = self._build_rule_sql(candidate, table_asset, target_config)
            if not rule_sql:
                logger.info(
                    f"[RuleIntelligence] Discarding candidate '{new_definition_data.get('name', '?')}' — "
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
                    f"[RuleIntelligence] Discarding candidate '{new_definition_data.get('name', '?')}' — "
                    "rule_sql failed to execute against Snowflake"
                )
                return None
            failed_count, total_count = executed
            violated_override = failed_count > 0
            evidence_override = f"{failed_count} of {total_count} rows fail this check"
        else:
            threshold_config = candidate.get("threshold_config") or {}

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
                if new_evidence and _word_overlap_score(new_evidence, old_reason) < 0.3:
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
            "fingerprint": fingerprint,
            "definition": definition,  # None if is_new_definition
            "new_definition_data": new_definition_data if is_new_definition else None,
            "scope": scope,
            "target_config": target_config,
            "threshold_config": threshold_config,
            "rule_sql": rule_sql,  # only set for is_new_definition; reuse needs none
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
            score = _word_overlap_score(combined, f"{d.name} {d.description or ''}")
            if score > best_score:
                best_score, best = score, d
        return best if best_score >= 0.55 else None

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

    # ── Formatting helpers ────────────────────────────────────────────────

    def _fetch_sample(self, table_asset: Any) -> str:
        try:
            fqn = table_asset.fqn
            rows = sf_session.query(f"SELECT * FROM {fqn} LIMIT 5")
            if not rows:
                return "(no data)"
            headers = list(rows[0].keys())[:10]
            lines = [" | ".join(headers), "-" * 60]
            for row in rows:
                vals = [str(row.get(h, ""))[:20] for h in headers]
                lines.append(" | ".join(vals))
            return "\n".join(lines)
        except Exception as e:
            logger.warning(f"[RuleIntelligence] Could not fetch sample data: {e}")
        return "(sample data unavailable)"

    def _fetch_column_stats(self, table_asset: Any, column_assets: List[Any]) -> str:
        """Real per-column stats (null%, distinct count, min/max, top values)
        computed from the live table — this is what grounds a proposed
        check's violation_evidence in actual data instead of a 3-row guess.
        One query per column, capped at 20 columns to bound cost on wide
        tables; skips columns that error (e.g. unsupported type for MIN/MAX)
        rather than failing the whole scan."""
        fqn = table_asset.fqn
        lines = []
        for col in column_assets[:20]:
            name = col.column_name
            if not name:
                continue
            try:
                rows = sf_session.query(
                    f"""
                    SELECT
                        COUNT(*) AS TOTAL,
                        COUNT_IF({name} IS NULL) AS NULLS,
                        COUNT(DISTINCT {name}) AS DISTINCT_COUNT
                    FROM {fqn}
                    """
                )
                stat = rows[0] if rows else {}
                total = stat.get("TOTAL", 0) or 0
                nulls = stat.get("NULLS", 0) or 0
                distinct = stat.get("DISTINCT_COUNT", 0) or 0
                null_pct = round((nulls / total * 100), 1) if total else 0.0

                top_rows = sf_session.query(
                    f"""
                    SELECT {name} AS VAL, COUNT(*) AS CNT
                    FROM {fqn}
                    WHERE {name} IS NOT NULL
                    GROUP BY {name}
                    ORDER BY CNT DESC
                    LIMIT 5
                    """
                )
                top_values = ", ".join(f"{r['VAL']!r}({r['CNT']})" for r in top_rows)

                lines.append(
                    f"  {name:<25} null%={null_pct:<6} distinct={distinct:<8} "
                    f"top_values=[{top_values}]"
                )
            except Exception as e:
                logger.debug(f"[RuleIntelligence] Skipping stats for column {name}: {e}")
                continue
        return "\n".join(lines) if lines else "  (stats unavailable)"

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

    def _call_model(self, prompt: str) -> str:
        # Call Bedrock (Opus 4.8) directly. We deliberately do NOT route through
        # Snowflake Cortex here: the 2-arg CORTEX.COMPLETE has a low, unraisable
        # output cap that truncates the large rule-classification response, and
        # its single-quote-only escaping breaks on the prompt's JSON braces.
        # ask_claude streams internally with a 32k ceiling, so the full response
        # comes back intact.
        return ask_claude(prompt, system=SYSTEM_PROMPT, max_tokens=32000)

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
