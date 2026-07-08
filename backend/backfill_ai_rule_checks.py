"""
One-time migration: backfill check_type + check_config on existing AI-generated rules
that were created before the check_expression feature was added.

Run once:
  .\venv\Scripts\python.exe backfill_ai_rule_checks.py
"""
import json
import re
import sys
import logging

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

from app.core.database import SessionLocal
from app.models.rule import Rule
from app.services.claude_client import ask_claude

SYSTEM = """You are a data quality rule engine developer.
Given a rule name and description, output the check_type and check_config
that can be used to automatically validate this rule against column metadata.

check_type options:
  not_null        — column should be NOT NULL
  not_empty       — column should be NOT NULL and non-empty string
  allowed_values  — column value must be in a set: check_config.allowed_values = [...]
  positive        — column must be > 0
  non_negative    — column must be >= 0
  min_value       — column must be >= check_config.min_value
  max_value       — column must be <= check_config.max_value
  regex           — column must match check_config.regex_pattern
  column_exists   — table must have check_config.required_columns
  comparison      — check_config.compare_columns + check_config.compare_operator (< > <= >= !=)
  custom_sql      — check_config.custom_sql_where (WHERE clause returning 0 rows if OK)

Respond with JSON only:
{
  "check_type": "...",
  "check_config": {
    "column": "COLUMN_NAME_OR_NULL",
    "allowed_values": null,
    "min_value": null,
    "max_value": null,
    "regex_pattern": null,
    "compare_columns": null,
    "compare_operator": null,
    "required_columns": null,
    "custom_sql_where": null
  }
}"""


def extract_json(text: str) -> dict:
    for pattern in [r"```(?:json)?\s*(\{.*\})\s*```", r"\{.*\}"]:
        m = re.search(pattern, text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1) if "```" in pattern else m.group(0))
            except Exception:
                continue
    return {}


def backfill():
    db = SessionLocal()
    try:
        rules = db.query(Rule).filter(
            Rule.created_by == "rule_intelligence_agent",
        ).all()

        needs_backfill = [
            r for r in rules
            if not (r.rule_config or {}).get("check_type")
        ]

        logger.info(f"Found {len(rules)} AI rules, {len(needs_backfill)} need check_type backfill")

        updated = 0
        for rule in needs_backfill:
            prompt = f"Rule name: {rule.name}\nDescription: {rule.description[:300]}\n\nOutput check_type and check_config JSON."
            try:
                raw = ask_claude(prompt, system=SYSTEM, max_tokens=512)
                parsed = extract_json(raw)
                if parsed.get("check_type"):
                    current = dict(rule.rule_config or {})
                    current["check_type"]   = parsed["check_type"]
                    current["check_config"] = parsed.get("check_config") or {}
                    rule.rule_config = current
                    db.flush()
                    updated += 1
                    logger.info(f"  OK  {rule.code:<45} → {parsed['check_type']}")
                else:
                    logger.warning(f"  SKIP {rule.code} — no check_type in response")
            except Exception as e:
                logger.warning(f"  FAIL {rule.code}: {e}")

        db.commit()
        logger.info(f"\nBackfilled {updated}/{len(needs_backfill)} rules")
    finally:
        db.close()


if __name__ == "__main__":
    backfill()
