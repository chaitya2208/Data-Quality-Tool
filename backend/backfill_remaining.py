"""Backfill check_type on remaining rules not covered by the first pass."""
import json, re, logging, sys
logging.basicConfig(level=logging.WARNING, format='%(message)s')

from app.core.database import SessionLocal
from app.models.rule import Rule
from app.services.claude_client import ask_claude

STATIC_CODES = {
    'MISSING_TABLE_COMMENT','MISSING_TABLE_OWNER','MISSING_COLUMN_COMMENT',
    'NO_PRIMARY_KEY_HINT','MISSING_CREATED_AT','MISSING_UPDATED_AT',
    'TOO_MANY_COLUMNS','INCONSISTENT_COLUMN_NAMING','PII_COLUMN_NO_MASKING',
    'GENERIC_COLUMN_NAME','COLUMN_TYPE_MISMATCH','NULLABLE_ID_COLUMN',
    'DATE_STORED_AS_VARCHAR','BOOLEAN_STORED_AS_VARCHAR','FK_COLUMN_NO_CONSTRAINT',
    'COLUMN_ID_WRONG_TYPE','COLUMN_DATE_WRONG_TYPE',
}

SYSTEM = (
    "You are a data quality rule engine developer. "
    "Given a rule name and description, output check_type and check_config. "
    "check_type options: not_null | not_empty | allowed_values | positive | non_negative | "
    "min_value | max_value | regex | column_exists | columns_exist | comparison | custom_sql. "
    'Respond with JSON only: {"check_type": "...", "check_config": {"column": null, '
    '"allowed_values": null, "min_value": null, "max_value": null, "regex_pattern": null, '
    '"compare_columns": null, "compare_operator": null, "required_columns": null, "custom_sql_where": null}}'
)


def extract_json(text):
    for pat in [r"```(?:json)?\s*(\{.*\})\s*```", r"\{.*\}"]:
        m = re.search(pat, text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1) if "```" in pat else m.group(0))
            except Exception:
                pass
    return {}


db = SessionLocal()
try:
    rules = db.query(Rule).filter(Rule.is_active == 1).all()
    needs = [
        r for r in rules
        if r.code not in STATIC_CODES
        and not (r.rule_config or {}).get("check_type")
    ]
    print(f"Backfilling {len(needs)} rules...")
    updated = 0
    for rule in needs:
        prompt = (
            f"Rule name: {rule.name}\n"
            f"Description: {rule.description[:300]}\n"
            "Output check_type and check_config JSON."
        )
        try:
            raw = ask_claude(prompt, system=SYSTEM, max_tokens=512)
            parsed = extract_json(raw)
            if parsed.get("check_type"):
                current = dict(rule.rule_config or {})
                current["check_type"]   = parsed["check_type"]
                current["check_config"] = parsed.get("check_config") or {}
                current["ai_generated"] = True
                rule.rule_config = current
                db.flush()
                updated += 1
                print(f"  OK  {rule.code:<45} -> {parsed['check_type']}")
            else:
                print(f"  SKIP {rule.code} (no check_type in response)")
        except Exception as e:
            print(f"  FAIL {rule.code}: {e}")
    db.commit()
    print(f"\nDone: {updated}/{len(needs)} backfilled")
finally:
    db.close()
