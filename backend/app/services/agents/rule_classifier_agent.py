"""
Rule Classifier Agent — Phase 2 LangGraph-style intelligence.

Calls Claude (via Cortex) to intelligently decide which data quality rules
are relevant for a specific table, and whether to adjust severity based on
the table's inferred business purpose (fact, dimension, staging, etc.).

Output stored in AgentTask.output so the UI can show:
  - Which rules were selected and why
  - Which rules were skipped and why
  - The inferred table type with confidence %
"""
import json
import logging
import re
from typing import List, Dict, Any, Optional
from sqlalchemy.orm import Session

from app.models.asset import Asset
from app.models.rule import Rule
from app.services.cortex_client import ask_for_recommendation as _call_cortex
from app.services.snowflake_session import session as sf_session
from app.services.claude_client import ask_claude

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a Snowflake data quality expert and data architect.

Given a table schema, infer the table's business purpose and decide which
data quality rules are relevant. Consider:
- Staging/raw tables: don't need audit timestamps or FK constraints
- Fact tables: need PK, timestamps, no NULLs on key columns, high ownership bar
- Dimension tables: need comments, PII masking, consistent naming
- Config/lookup tables: minimal rules apply
- Audit/log tables: append-only, different standards

Respond with valid JSON only — no markdown, no prose outside the JSON.
"""

USER_PROMPT_TEMPLATE = """Table: {fqn}
Row count: {row_count}

Columns:
{columns}

Available data quality rules:
{rules_summary}

For each rule decide:
- run: true or false
- severity_override: null, or one of "critical"/"high"/"medium"/"low" if different from default
- reason: one sentence explaining the decision

Respond with this exact JSON structure:
{{
  "table_type": "fact|dimension|staging|config|audit|reference|unknown",
  "table_type_confidence": <0-100>,
  "table_type_reason": "one sentence",
  "rules": {{
    "<RULE_CODE>": {{"run": true/false, "severity_override": null, "reason": "..."}}
  }}
}}

Include ALL {rule_count} rules in the response — even ones you skip."""


class RuleClassification:
    """Result from the classifier agent."""
    def __init__(self, raw: dict):
        self.table_type: str = raw.get("table_type", "unknown")
        self.table_type_confidence: int = int(raw.get("table_type_confidence", 50))
        self.table_type_reason: str = raw.get("table_type_reason", "")
        # rule_code → {run: bool, severity_override: str|None, reason: str}
        self.rules: Dict[str, Dict[str, Any]] = raw.get("rules", {})

    def should_run(self, rule_code: str) -> bool:
        decision = self.rules.get(rule_code)
        if decision is None:
            return True  # default: run if not mentioned
        return bool(decision.get("run", True))

    def severity_override(self, rule_code: str) -> Optional[str]:
        decision = self.rules.get(rule_code)
        if not decision:
            return None
        return decision.get("severity_override") or None

    def reason(self, rule_code: str) -> str:
        decision = self.rules.get(rule_code)
        if not decision:
            return ""
        return decision.get("reason", "")

    def selected_codes(self) -> List[str]:
        return [code for code, d in self.rules.items() if d.get("run", True)]

    def skipped_codes(self) -> List[str]:
        return [code for code, d in self.rules.items() if not d.get("run", True)]

    def to_task_output(self) -> dict:
        """Structured output for AgentTask.output — shown in UI log panel."""
        return {
            "table_type": self.table_type,
            "table_type_confidence": self.table_type_confidence,
            "table_type_reason": self.table_type_reason,
            "rules_selected": len(self.selected_codes()),
            "rules_skipped": len(self.skipped_codes()),
            "selected": {
                code: {
                    "severity_override": self.severity_override(code),
                    "reason": self.reason(code),
                }
                for code in self.selected_codes()
            },
            "skipped": {
                code: self.reason(code)
                for code in self.skipped_codes()
            },
        }


class RuleClassifierAgent:
    """
    Uses Claude to intelligently filter and tune data quality rules
    based on the table's inferred business purpose.
    """

    def __init__(self, db: Session):
        self.db = db

    def run(
        self,
        table_asset: Asset,
        column_assets: List[Asset],
        all_rules: List[Rule],
    ) -> RuleClassification:
        logger.info(f"[RuleClassifier] Classifying table {table_asset.fqn}")

        columns_text = self._format_columns(column_assets)
        rules_summary = self._format_rules(all_rules)

        prompt = USER_PROMPT_TEMPLATE.format(
            fqn=table_asset.fqn,
            row_count=table_asset.row_count or "unknown",
            columns=columns_text,
            rules_summary=rules_summary,
            rule_count=len(all_rules),
        )

        raw_response = self._call_model(prompt)
        parsed = self._extract_json(raw_response)

        # Fill in any missing rules with default (run=True)
        for rule in all_rules:
            if rule.code not in parsed.get("rules", {}):
                parsed.setdefault("rules", {})[rule.code] = {
                    "run": True,
                    "severity_override": None,
                    "reason": "Default — rule not classified",
                }

        classification = RuleClassification(parsed)
        logger.info(
            f"[RuleClassifier] Table type: {classification.table_type} "
            f"({classification.table_type_confidence}% confidence). "
            f"Running {len(classification.selected_codes())}/{len(all_rules)} rules, "
            f"skipping {len(classification.skipped_codes())}."
        )
        return classification

    def _format_columns(self, column_assets: List[Asset]) -> str:
        lines = []
        for col in column_assets:
            meta = col.raw_metadata or {}
            dtype = meta.get("data_type", "UNKNOWN")
            nullable = meta.get("is_nullable", "Y")
            null_str = "NOT NULL" if str(nullable).upper() in ("N", "NO") else "nullable"
            comment = col.comment or ""
            comment_str = f'  -- "{comment}"' if comment else ""
            lines.append(f"  {col.column_name:<30} {dtype:<20} {null_str}{comment_str}")
        return "\n".join(lines) if lines else "  (no columns found)"

    def _format_rules(self, rules: List[Rule]) -> str:
        lines = []
        for rule in rules:
            applies = "/".join(rule.applies_to or [])
            lines.append(
                f"  {rule.code:<40} [{rule.category.value if hasattr(rule.category, 'value') else rule.category}]"
                f"  severity={rule.severity.value if hasattr(rule.severity, 'value') else rule.severity}"
                f"  applies_to={applies}"
                f"  — {rule.description[:80]}"
            )
        return "\n".join(lines)

    def _call_model(self, prompt: str) -> str:
        # Try Cortex first (has table context from Snowflake env)
        try:
            return sf_session.ask_cortex(prompt, model="claude-opus-4-8")
        except Exception as e:
            logger.warning(f"[RuleClassifier] Cortex failed ({e}), falling back to Claude/Bedrock")

        # Fallback: Claude via Bedrock
        return ask_claude(prompt, system=SYSTEM_PROMPT, max_tokens=2048)

    @staticmethod
    def _extract_json(text: str) -> dict:
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if match:
            return json.loads(match.group(1))
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        logger.warning(f"[RuleClassifier] Could not extract JSON from response: {text[:300]}")
        return {}
