import json
import re
import logging
from typing import List
from sqlalchemy.orm import Session

from app.models.agent_run import AgentTask
from app.models.asset import Asset
from app.models.rule import Rule, RuleStatus, RuleCategory, RuleSeverity
from app.services.claude_client import ask_claude

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are a Snowflake data quality expert. "
    "Analyze the given table schema and suggest NEW data quality rules specific to this table's business logic. "
    "Only suggest rules that are NOT already covered by generic schema checks like: "
    "missing comments, missing owner, nullable ID columns, date/boolean stored as VARCHAR, or missing timestamps. "
    "Focus on business-logic rules such as: allowed value sets for status/type columns, "
    "numeric range constraints, referential patterns, naming consistency for this domain, etc. "
    "Always respond with valid JSON only — a JSON array, no markdown, no prose outside the array. "
    'Use this exact schema: [{"code": "UPPER_SNAKE_CASE", "name": "...", "description": "...", '
    '"category": "data_quality|schema|naming|security", "severity": "high|medium|low", '
    '"applies_to": ["column"], "rationale": "why this rule matters for this specific table"}]'
)

VALID_CATEGORIES = {c.value for c in RuleCategory}
VALID_SEVERITIES = {s.value for s in RuleSeverity}


class RuleSuggestionAgent:
    """
    Analyzes the table schema and asks Claude to propose NEW business-logic rules.
    Creates Rule rows with status=PENDING so they land in the approval queue.
    """

    def __init__(self, db: Session):
        self.db = db

    def run(
        self, table_asset: Asset, column_assets: List[Asset],
        run_id: str, task: AgentTask
    ) -> int:
        logger.info(f"[RuleSuggestionAgent] Analyzing {table_asset.fqn}")

        task.output = {"progress": "Building schema summary for Claude..."}
        self.db.commit()

        prompt = self._build_schema_prompt(table_asset, column_assets)

        task.output = {"progress": "Asking Claude for rule suggestions..."}
        self.db.commit()

        raw = ask_claude(prompt, system=SYSTEM_PROMPT, max_tokens=2048)
        suggestions = self._extract_json_array(raw)

        created = 0
        for suggestion in suggestions:
            rule = self._create_rule(suggestion, run_id)
            if rule:
                self.db.add(rule)
                created += 1

        self.db.commit()
        logger.info(f"[RuleSuggestionAgent] Created {created} PENDING rule suggestions")

        task.output = {
            "progress": f"Done — {created} rule proposals created",
            "suggestions_count": created,
            "table": table_asset.fqn,
        }
        self.db.commit()
        return created

    def _build_schema_prompt(self, table_asset: Asset, column_assets: List[Asset]) -> str:
        fqn = table_asset.fqn
        lines = [
            f"Table: {fqn}",
            f"Owner: {table_asset.owner or 'unknown'}",
            f"Comment: {table_asset.comment or 'none'}",
            f"Row count: {table_asset.row_count or 'unknown'}",
            "",
            "Columns:",
        ]
        for col in column_assets:
            meta = col.raw_metadata or {}
            data_type = meta.get("data_type", "UNKNOWN")
            nullable = meta.get("is_nullable", "YES")
            comment = col.comment or "no comment"
            lines.append(
                f"  - {col.column_name}: {data_type}, nullable={nullable}, comment={comment}"
            )

        lines += [
            "",
            "Based on this schema, suggest 3-5 NEW data quality rules specific to this table.",
            "Return a JSON array of rule suggestions.",
        ]
        return "\n".join(lines)

    def _create_rule(self, suggestion: dict, run_id: str) -> Rule | None:
        code = (suggestion.get("code") or "").upper().strip()
        name = suggestion.get("name", "").strip()
        description = suggestion.get("description", "").strip()
        category_raw = suggestion.get("category", "data_quality").lower()
        severity_raw = suggestion.get("severity", "medium").lower()
        applies_to = suggestion.get("applies_to", ["table"])
        rationale = suggestion.get("rationale", "")

        if not code or not name or not description:
            logger.warning(f"[RuleSuggestionAgent] Skipping incomplete suggestion: {suggestion}")
            return None

        # Normalize category and severity to valid enum values
        category = category_raw if category_raw in VALID_CATEGORIES else "data_quality"
        severity = severity_raw if severity_raw in VALID_SEVERITIES else "medium"

        # Skip if a rule with the same code already exists
        existing = self.db.query(Rule).filter(Rule.code == code).first()
        if existing:
            logger.info(f"[RuleSuggestionAgent] Rule {code} already exists, skipping")
            return None

        return Rule(
            code=code,
            name=name,
            description=f"{description}\n\n[AI Rationale] {rationale}",
            category=category,
            severity=severity,
            applies_to=applies_to if isinstance(applies_to, list) else ["table"],
            rule_config={"source_run_id": run_id},
            is_active=False,
            status=RuleStatus.PENDING,
            owner="ai-suggestion",
            created_by="rule_suggestion_agent",
            version=1,
        )

    @staticmethod
    def _extract_json_array(text: str) -> list:
        # Strip markdown code fences
        match = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
        if match:
            return json.loads(match.group(1))
        # Find bare JSON array
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        logger.warning(f"[RuleSuggestionAgent] No JSON array in response: {text[:200]}")
        return []
