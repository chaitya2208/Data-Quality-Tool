import json
import re
import logging
from typing import List, Optional
from sqlalchemy.orm import Session

from app.models.agent_run import AgentTask, AgentRecommendation
from app.models.finding import Finding
from app.models.asset import Asset
from app.models.rule import Rule
from app.services.claude_client import ask_claude

logger = logging.getLogger(__name__)

MAX_FINDINGS = 10
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}

SYSTEM_PROMPT = (
    "You are a Snowflake data quality expert. "
    "Given a data quality finding, provide a precise remediation. "
    "Always respond with valid JSON only — no markdown, no prose outside the JSON object. "
    'Use this exact schema: {"explanation": "...", "sql_query": "...", "confidence": <0-100>, "impact": "..."} '
    "The sql_query should be ready to run in Snowflake. "
    "If multiple steps are needed, separate with blank lines and SQL comments."
)


class RecommendationAgent:
    """
    Generates SQL fix recommendations for each Finding via Claude.
    Cache-first: reuses prior recommendations for the same rule_code + data_type
    to avoid repeat Claude calls. Updates task.output live for frontend progress.
    """

    def __init__(self, db: Session):
        self.db = db

    def run(self, findings: List[Finding], run_id: str, task: AgentTask) -> int:
        sorted_findings = sorted(
            findings, key=lambda f: SEVERITY_ORDER.get(f.severity, 5)
        )[:MAX_FINDINGS]

        generated = 0
        cache_hits = 0
        claude_calls = 0

        for i, finding in enumerate(sorted_findings):
            task.output = {
                "progress": f"Generating {i + 1}/{len(sorted_findings)}...",
                "cache_hits": cache_hits,
                "claude_calls": claude_calls,
                "current_finding": finding.title,
            }
            self.db.commit()

            try:
                rec, from_cache = self._process_finding(finding, run_id)
                if rec:
                    self.db.add(rec)
                    self.db.commit()
                    generated += 1
                    if from_cache:
                        cache_hits += 1
                    else:
                        claude_calls += 1
            except Exception as e:
                logger.warning(f"[RecommendationAgent] Failed on {finding.id}: {e}")

        task.output = {
            "progress": f"Done — {generated}/{len(sorted_findings)} recommendations",
            "cache_hits": cache_hits,
            "claude_calls": claude_calls,
        }
        self.db.commit()
        return generated

    def _process_finding(
        self, finding: Finding, run_id: str
    ) -> tuple[Optional[AgentRecommendation], bool]:
        context = finding.context or {}
        rule_code = context.get("rule_code", "")
        asset = self.db.query(Asset).filter(Asset.id == finding.asset_id).first()
        data_type = ""
        if asset and asset.raw_metadata:
            data_type = asset.raw_metadata.get("data_type", "")
        cache_key = f"{rule_code}::{data_type}"

        # Cache lookup
        existing = (
            self.db.query(AgentRecommendation)
            .filter(AgentRecommendation.cache_key == cache_key)
            .order_by(AgentRecommendation.created_at.desc())
            .first()
        )
        if existing:
            logger.info(f"[RecommendationAgent] Cache hit for {cache_key}")
            cloned = AgentRecommendation(
                run_id=run_id,
                finding_id=finding.id,
                explanation=existing.explanation,
                sql_query=existing.sql_query,
                confidence=existing.confidence,
                impact=existing.impact,
                cache_key=cache_key,
                raw_response="[from cache]",
            )
            return cloned, True

        # Cache miss — call Claude
        rule = (
            self.db.query(Rule).filter(Rule.id == finding.rule_id).first()
            if finding.rule_id else None
        )
        prompt = self._build_prompt(finding, rule, asset, context, finding.evidence or {})
        raw = ask_claude(prompt, system=SYSTEM_PROMPT, max_tokens=1024)
        parsed = self._extract_json(raw)

        rec = AgentRecommendation(
            run_id=run_id,
            finding_id=finding.id,
            explanation=parsed.get("explanation", "No explanation provided"),
            sql_query=parsed.get("sql_query", "-- No SQL generated"),
            confidence=int(parsed.get("confidence", 0)),
            impact=parsed.get("impact", ""),
            cache_key=cache_key,
            raw_response=raw[:4000],
        )
        return rec, False

    def _build_prompt(
        self, finding: Finding, rule: Optional[Rule],
        asset: Optional[Asset], context: dict, evidence: dict
    ) -> str:
        fqn = context.get("fqn", "UNKNOWN.SCHEMA.TABLE")
        col_name = context.get("column_name", "")
        rule_code = context.get("rule_code", rule.code if rule else "UNKNOWN")
        rule_desc = rule.description if rule else ""
        data_type = ""
        if asset and asset.raw_metadata:
            data_type = asset.raw_metadata.get("data_type", "")

        lines = [
            f"Finding: {finding.title}",
            f"Rule: {rule_code}" + (f" — {rule_desc}" if rule_desc else ""),
            f"Table: {fqn}",
        ]
        if col_name:
            lines.append(f"Column: {col_name}" + (f" (type: {data_type})" if data_type else ""))
        if evidence:
            lines.append(f"Evidence: {json.dumps(evidence, default=str)}")
        lines.append(
            '\nRespond with JSON:\n'
            '{"explanation": "...", "sql_query": "...", "confidence": 85, "impact": "..."}'
        )
        return "\n".join(lines)

    @staticmethod
    def _extract_json(text: str) -> dict:
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if match:
            return json.loads(match.group(1))
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise ValueError(f"No JSON in Claude response: {text[:200]}")
