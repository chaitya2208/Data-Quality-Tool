"""
Rule Intelligence Agent — the brain of the pipeline.

Single Claude call that does three things in one shot:
1. Reviews all existing rules and decides which apply to this table (with reasons)
2. Generates NEW AI rules specific to this table's business logic
3. For each AI rule, immediately checks if the current schema violates it

AI rules are created directly as ACTIVE rules and run immediately — no approval needed.

Output stored in AgentTask.output for the UI:
  - table_type + confidence
  - rules_used: [{code, reason, severity_override}]
  - rules_skipped: [{code, reason}]
  - ai_rules_generated: [{code, name, description, violated: bool, evidence}]
"""
import json
import logging
import re
import uuid
from datetime import datetime
from typing import List, Optional, Dict, Any, Set
from sqlalchemy.orm import Session

from app.models.asset import Asset
from app.models.rule import Rule, RuleStatus, RuleCategory, RuleSeverity
from app.models.finding import Finding, FindingStatus
from app.services.claude_client import ask_claude

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a senior Snowflake data quality architect.

Given a table schema and a full-dataset data profile (per-column statistics
computed over every row), you will:
1. Classify the table type (fact/dimension/staging/config/audit/reference)
2. For each existing rule, decide if it applies to this table type
3. Generate NEW data quality rules specific to this table's apparent business purpose
4. For each new rule, check if the profile shows the data already violates it

Respond with valid JSON only — no markdown, no prose outside the JSON.
Be thorough: AI rules should catch business logic issues that static rules miss.
Focus on: value constraints, referential patterns, naming standards for this domain,
null semantics, data freshness, business key uniqueness patterns."""

USER_PROMPT_TEMPLATE = """Table: {fqn}
Row count: {row_count}
Owner: {owner}

=== SCHEMA ===
{columns}

=== DATA PROFILE (full-dataset per-column statistics from a live scan) ===
{data_profile}

=== EXISTING RULES TO EVALUATE ===
{existing_rules}

Respond with this JSON:
{{
  "table_type": "fact|dimension|staging|config|audit|reference|unknown",
  "table_type_confidence": <0-100>,
  "table_type_reason": "one sentence",
  "existing_rules": {{
    "<RULE_CODE>": {{
      "run": true/false,
      "severity_override": null or "critical|high|medium|low",
      "reason": "one sentence"
    }}
  }},
  "ai_rules": [
    {{
      "code": "UPPER_SNAKE_CASE_UNIQUE_CODE",
      "name": "Short descriptive name",
      "description": "What this rule checks and why it matters",
      "category": "data_quality|schema|naming|security|ownership",
      "severity": "critical|high|medium|low",
      "applies_to": ["table"] or ["column"],
      "column_name": "COLUMN_NAME_IF_COLUMN_RULE or null",
      "violation_detected": true/false,
      "violation_evidence": "what specifically is wrong, or null if not violated",
      "rationale": "why this rule matters for this specific table"
    }}
  ]
}}

IMPORTANT REQUIREMENTS:
- You MUST include an "ai_rules" array in your response. It must never be omitted or empty.
- Generate 3-8 AI rules. Every table has business-logic rules worth capturing.
- USE THE DATA PROFILE: base rules on the observed statistics, not guesses. If a
  numeric column's max is a statistical outlier (e.g. AGE max = 999), propose a
  bounded-range rule and mark violation_detected=true. If an ID-like column has
  duplicates, propose a uniqueness rule. If a date column is stale, propose a
  freshness rule. If an email/phone column has low pattern-match %, propose a
  format rule. Ground every threshold in the profile's min/max/avg/stddev/distinct.
- The profile's "ANOMALIES" list flags concrete issues already detected in the
  data — prefer generating rules that would catch each of those.
- If the table seems well-structured, suggest rules for data freshness, value constraints, uniqueness, or referential patterns.
- Include ALL {rule_count} existing rules in the existing_rules object.
- Your ENTIRE response must be a single valid JSON object starting with {{ and ending with }}."""


class RuleIntelligenceAgent:
    """
    Merged classifier + suggester + immediate execution.
    Single Claude call, results applied directly.
    """

    def __init__(self, db: Session):
        self.db = db

    def run(
        self,
        table_asset: Asset,
        column_assets: List[Asset],
        existing_rules: List[Rule],
        run_id: str,
        profile: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Returns a result dict with:
          - classification: table type + selected/skipped rules
          - ai_rules: list of generated Rule objects (already persisted)
          - ai_violations: list of finding dicts for violated AI rules

        When `profile` (from ProfilingAgent) is provided, its per-column
        statistics and anomaly signals are added to the prompt so Claude
        generates rules grounded in the actual data distribution.
        """
        logger.info(f"[RuleIntelligence] Analyzing {table_asset.fqn}")

        columns_text = self._format_columns(column_assets)
        rules_text = self._format_existing_rules(existing_rules)
        profile_text = self._format_profile(profile)

        prompt = USER_PROMPT_TEMPLATE.format(
            fqn=table_asset.fqn,
            row_count=table_asset.row_count or "unknown",
            owner=table_asset.owner or "unknown",
            columns=columns_text,
            data_profile=profile_text,
            existing_rules=rules_text,
            rule_count=len(existing_rules),
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
        elif "existing_rules" not in parsed and "ai_rules" not in parsed:
            parse_warning = (
                f"LLM response missing 'existing_rules'/'ai_rules' (keys: "
                f"{list(parsed.keys())}) — using defaults."
            )
            logger.error(f"[RuleIntelligence] {parse_warning}")
        else:
            ai_rules_raw = parsed.get("ai_rules", [])
            if not ai_rules_raw:
                logger.warning(
                    f"[RuleIntelligence] Claude returned 0 ai_rules. "
                    f"Keys in response: {list(parsed.keys())}. "
                    f"This may be a model refusal or prompt issue."
                )
            else:
                logger.info(f"[RuleIntelligence] Claude returned {len(ai_rules_raw)} ai_rules candidates")

        # Build classification result
        classification = {
            "table_type":            parsed.get("table_type", "unknown"),
            "table_type_confidence": parsed.get("table_type_confidence", 50),
            "table_type_reason":     parsed.get("table_type_reason", ""),
            "existing_rules":        parsed.get("existing_rules", {}),
            "parse_warning":         parse_warning,
        }

        # Normalize: fill missing rule decisions with default (run=True)
        for rule in existing_rules:
            if rule.code not in classification["existing_rules"]:
                classification["existing_rules"][rule.code] = {
                    "run": True, "severity_override": None,
                    "reason": "Default — rule not explicitly classified",
                }

        # Process AI rules — persist and check violations
        ai_rules_created = []
        ai_violations = []
        for ai_rule_data in parsed.get("ai_rules", []):
            rule, violation_finding = self._process_ai_rule(
                ai_rule_data, table_asset, column_assets, run_id
            )
            if rule:
                ai_rules_created.append(rule)
            if violation_finding:
                ai_violations.append(violation_finding)

        logger.info(
            f"[RuleIntelligence] Table={classification['table_type']}, "
            f"existing: {sum(1 for v in classification['existing_rules'].values() if v.get('run'))} run / "
            f"{sum(1 for v in classification['existing_rules'].values() if not v.get('run'))} skip, "
            f"AI rules: {len(ai_rules_created)} created, {len(ai_violations)} violations"
        )

        return {
            "classification":  classification,
            "ai_rules":        ai_rules_created,
            "ai_violations":   ai_violations,
        }

    def get_selected_codes(self, classification: dict) -> Set[str]:
        return {
            code for code, d in classification.get("existing_rules", {}).items()
            if d.get("run", True)
        }

    def get_skipped_codes(self, classification: dict) -> Set[str]:
        return {
            code for code, d in classification.get("existing_rules", {}).items()
            if not d.get("run", True)
        }

    def get_severity_override(self, classification: dict, rule_code: str) -> Optional[str]:
        d = classification.get("existing_rules", {}).get(rule_code, {})
        return d.get("severity_override") or None

    def apply_severity_overrides(self, classification: dict) -> None:
        """Temporarily patch Rule.severity for overridden rules."""
        for rule_code, decision in classification.get("existing_rules", {}).items():
            override = decision.get("severity_override")
            if not override or not decision.get("run", True):
                continue
            rule = self.db.query(Rule).filter(Rule.code == rule_code).first()
            if rule:
                rule._original_severity = rule.severity
                try:
                    rule.severity = RuleSeverity(override)
                except ValueError:
                    pass
        self.db.flush()

    def restore_severity_overrides(self, classification: dict) -> None:
        for rule_code in classification.get("existing_rules", {}):
            rule = self.db.query(Rule).filter(Rule.code == rule_code).first()
            if rule and hasattr(rule, "_original_severity"):
                rule.severity = rule._original_severity
                del rule._original_severity
        self.db.commit()

    def _process_ai_rule(
        self,
        data: dict,
        table_asset: Asset,
        column_assets: List[Asset],
        run_id: str,
    ):
        code = (data.get("code") or "").upper().strip()
        name = (data.get("name") or "").strip()
        description = (data.get("description") or "").strip()
        if not code or not name or not description:
            return None, None

        # Skip if rule already exists
        existing = self.db.query(Rule).filter(Rule.code == code).first()

        if not existing:
            category_raw = (data.get("category") or "data_quality").lower()
            severity_raw = (data.get("severity") or "medium").lower()
            applies_to   = data.get("applies_to") or ["table"]

            valid_cats = {c.value for c in RuleCategory}
            valid_sevs = {s.value for s in RuleSeverity}
            category = category_raw if category_raw in valid_cats else "data_quality"
            severity = severity_raw if severity_raw in valid_sevs else "medium"

            rationale = data.get("rationale", "")
            full_desc = f"{description}\n\n[AI Rationale] {rationale}" if rationale else description

            rule = Rule(
                code=code,
                name=name,
                description=full_desc,
                category=category,
                severity=severity,
                applies_to=applies_to if isinstance(applies_to, list) else ["table"],
                rule_config={"source_run_id": run_id, "ai_generated": True},
                is_active=True,       # runs immediately — no approval needed
                status=RuleStatus.ACTIVE,
                owner="rule-intelligence-agent",
                created_by="rule_intelligence_agent",
                version=1,
            )
            self.db.add(rule)
            self.db.flush()
            target_rule = rule
        else:
            target_rule = existing

        # If Claude detected a violation, create a finding
        violation_finding = None
        if data.get("violation_detected"):
            evidence_text = data.get("violation_evidence") or "Violation detected by AI rule check"
            col_name = data.get("column_name")

            # Find the right asset (column or table)
            if col_name:
                col_fqn = f"{table_asset.fqn}.{col_name}"
                asset = (
                    self.db.query(Asset).filter(Asset.fqn == col_fqn).first()
                    or table_asset
                )
            else:
                asset = table_asset

            violation_finding = {
                "asset_id":    asset.id,
                "scan_id":     None,  # will be filled by caller
                "rule_id":     target_rule.id,
                "title":       f"{name} violated on {asset.fqn.split('.')[-1]}",
                "description": f"AI Rule Intelligence detected: {evidence_text}",
                "severity":    target_rule.severity.value if hasattr(target_rule.severity, "value") else str(target_rule.severity),
                "status":      FindingStatus.DETECTED,
                "context": {
                    "rule_code":     code,
                    "fqn":           asset.fqn,
                    "table_name":    table_asset.table_name,
                    "schema_name":   table_asset.schema_name,
                    "database_name": table_asset.database_name,
                    "column_name":   col_name or "",
                    "ai_generated":  True,
                },
                "evidence": {"ai_evidence": evidence_text},
            }

        return target_rule, violation_finding

    def _format_columns(self, column_assets: List[Asset]) -> str:
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

    def _format_profile(self, profile: Optional[Dict[str, Any]]) -> str:
        """
        Render the FULL data profile for the prompt — every stat the profiling
        agent computed over the complete dataset, one block per column, plus the
        distilled anomaly list. This is the primary data signal for rule
        generation, so nothing computed is dropped: null%, distinct count + %,
        duplicates, min/max, avg/stddev, freshness, pattern-match, and the actual
        most-frequent values (top_values) — the latter is what lets Claude write
        precise allowed-values rules for status/categorical columns.
        """
        if not profile or not profile.get("columns"):
            return "(no data profile available — base rules on schema only)"

        tbl = profile.get("table", {})
        lines = [f"Rows profiled (full dataset): {tbl.get('row_count', '?')}", ""]

        for c in profile["columns"]:
            name = c.get("column_name")
            parts = [f"null={c.get('null_percentage')}%"]
            if c.get("distinct_count") is not None:
                dpct = f" ({c['distinct_pct']}%)" if c.get("distinct_pct") is not None else ""
                parts.append(f"distinct={c['distinct_count']}{dpct}")
            if c.get("duplicate_count") is not None:
                parts.append(f"dup_values={c['duplicate_count']}")
            if c.get("min_value") is not None or c.get("max_value") is not None:
                parts.append(f"range={c.get('min_value')}..{c.get('max_value')}")
            if c.get("avg_value") is not None:
                parts.append(f"avg={c['avg_value']}")
            if c.get("stddev") is not None:
                parts.append(f"stddev={c['stddev']}")
            if c.get("freshness_days") is not None:
                parts.append(f"freshness={c['freshness_days']}d")
            if c.get("pattern_match_pct") is not None:
                parts.append(f"pattern_match={c['pattern_match_pct']}%")
            if c.get("outlier_hint"):
                parts.append("OUTLIER!")

            lines.append(f"  {name} [{c.get('category')}]: " + ", ".join(parts))

            # Actual most-frequent values — critical for allowed-values rules.
            tv = c.get("top_values") or []
            if tv:
                rendered = ", ".join(
                    f"{t.get('value')!r} (x{t.get('count')})" for t in tv[:8]
                )
                lines.append(f"      top values: {rendered}")

        anomalies = profile.get("anomalies", [])
        if anomalies:
            lines.append("")
            lines.append("ANOMALIES DETECTED IN DATA:")
            for a in anomalies:
                lines.append(f"  - [{a.get('type')}] {a.get('column')}: {a.get('detail')}")
        return "\n".join(lines)

    def _format_existing_rules(self, rules: List[Rule]) -> str:
        lines = []
        for rule in rules:
            cat = rule.category.value if hasattr(rule.category, "value") else rule.category
            sev = rule.severity.value if hasattr(rule.severity, "value") else rule.severity
            app = "/".join(rule.applies_to or [])
            lines.append(
                f"  {rule.code:<45} [{cat}] severity={sev} applies_to={app}"
                f"\n    {rule.description[:100]}"
            )
        return "\n".join(lines)

    def _call_model(self, prompt: str) -> str:
        # Call Bedrock (Opus 4.8) directly. We deliberately do NOT route through
        # Snowflake Cortex here: the 2-arg CORTEX.COMPLETE has a low, unraisable
        # output cap that truncates the large rule-classification response, and
        # its single-quote-only escaping breaks on the prompt's JSON braces.
        # ask_claude streams internally with a 32k ceiling, so the full response
        # for 100+ rules comes back intact.
        return ask_claude(prompt, system=SYSTEM_PROMPT, max_tokens=32000)

    @staticmethod
    def _extract_json(text: str) -> dict:
        # Try fenced code block first — use GREEDY match to get the full object
        match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
        if match:
            try:
                result = json.loads(match.group(1))
                if "ai_rules" in result or "table_type" in result:
                    return result
            except Exception:
                pass
        # Fallback: find outermost { ... } — greedy, gets the full JSON
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                result = json.loads(match.group(0))
                if "ai_rules" in result or "table_type" in result:
                    return result
            except Exception:
                pass
        # Last resort: try parsing the whole text directly
        try:
            return json.loads(text.strip())
        except Exception:
            pass
        logger.warning(f"[RuleIntelligence] Could not extract JSON. Raw response (first 500 chars): {text[:500]}")
        return {}
