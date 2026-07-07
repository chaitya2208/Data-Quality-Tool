"""
Snowflake Cortex recommendation client.

Uses SNOWFLAKE.CORTEX.COMPLETE() via the existing SSO session — no extra
credentials, no API key. Includes actual DESCRIBE TABLE output in the prompt
so the model sees real column names, types, and constraints.

Falls back to Claude/Bedrock if Cortex is unavailable or errors.
"""
import json
import logging
from typing import Optional

from app.services.snowflake_session import session as sf_session
from app.services.claude_client import ask_claude

logger = logging.getLogger(__name__)

CORTEX_MODEL = "claude-opus-4-8"

SYSTEM_PROMPT = """You are a Snowflake SQL expert and data quality engineer.
Given a data quality finding and the actual table schema, generate ONE precise SQL fix.

Strict rules:
- Use exact column names and types from the schema provided
- Write Snowflake-compatible SQL only
- Respond with valid JSON only — no markdown, no prose outside the JSON
- JSON schema: {"explanation":"...","sql_query":"...","confidence":<0-100>,"impact":"..."}
- sql_query must contain ONLY the single most appropriate fix SQL statement (or two steps if truly necessary, separated by a semicolon and newline)
- DO NOT include Option A / Option B alternatives — pick the best single fix
- DO NOT include verification SELECT statements in sql_query
- DO NOT add comments about alternatives — put the best fix directly
- The sql_query must be executable directly in Snowflake with no editing required
- Snowflake syntax rules you MUST follow:
  * Column defaults: use CURRENT_TIMESTAMP not CURRENT_TIMESTAMP() — parentheses are invalid in DEFAULT clauses
  * Use TIMESTAMP_NTZ not TIMESTAMP for new timestamp columns
  * ALTER TABLE ... ADD COLUMN uses DEFAULT not SET DEFAULT"""


def ask_for_recommendation(
    rule_code: str,
    rule_description: str,
    finding_title: str,
    fqn: str,
    database: str,
    schema: str,
    table: str,
    column_name: str,
    data_type: str,
    evidence: dict,
) -> dict:
    """
    Build a rich prompt including live DESCRIBE TABLE output and call Cortex.
    Falls back to Claude/Bedrock on any Cortex error.
    Returns dict: {explanation, sql_query, confidence, impact, source}
    where source is 'cortex' or 'claude'.
    """
    # Fetch live schema so Cortex has real column context
    schema_context = _get_schema_context(database, schema, table)

    prompt = _build_prompt(
        rule_code=rule_code,
        rule_description=rule_description,
        finding_title=finding_title,
        fqn=fqn,
        column_name=column_name,
        data_type=data_type,
        evidence=evidence,
        schema_context=schema_context,
    )

    # Try Cortex first
    try:
        raw = sf_session.ask_cortex(prompt, model=CORTEX_MODEL)
        parsed = _extract_json(raw)
        if parsed.get("sql_query") and parsed.get("explanation"):
            logger.info(f"[Cortex] Recommendation generated for {rule_code} on {fqn}")
            parsed["sql_query"] = _sanitize_sql(parsed["sql_query"])
            return {**parsed, "source": "cortex"}
        raise ValueError(f"Cortex returned incomplete JSON: {parsed}")
    except Exception as e:
        logger.warning(f"[Cortex] Failed ({e}), falling back to Claude/Bedrock")

    # Fallback: Claude via Bedrock
    try:
        raw = ask_claude(prompt, system=SYSTEM_PROMPT, max_tokens=1024)
        parsed = _extract_json(raw)
        logger.info(f"[Cortex→Claude] Fallback recommendation for {rule_code} on {fqn}")
        parsed["sql_query"] = _sanitize_sql(parsed.get("sql_query", ""))
        return {**parsed, "source": "claude"}
    except Exception as e2:
        logger.error(f"[Cortex→Claude] Both Cortex and Claude failed: {e2}")
        return {
            "explanation": f"Could not generate recommendation: {e2}",
            "sql_query": "-- Generation failed. Check backend logs.",
            "confidence": 0,
            "impact": "Unknown",
            "source": "error",
        }


def _get_schema_context(database: str, schema: str, table: str) -> str:
    """
    Returns a compact schema summary from DESCRIBE TABLE.
    Example:
      CUSTOMER_ID NUMBER NOT NULL (primary key)
      FIRST_NAME  VARCHAR nullable
      CREATED_AT  TIMESTAMP_NTZ NOT NULL
    """
    if not database or not schema or not table:
        return ""
    try:
        rows = sf_session.describe_table(database, schema, table)
        if not rows:
            return ""
        lines = []
        for row in rows:
            name = row.get("name") or row.get("NAME") or ""
            dtype = row.get("type") or row.get("TYPE") or ""
            nullable = row.get("null?") or row.get("NULL?") or row.get("nullable") or "Y"
            primary_key = row.get("primary key") or row.get("PRIMARY KEY") or "N"
            comment = row.get("comment") or row.get("COMMENT") or ""
            null_str = "NOT NULL" if str(nullable).upper() in ("N", "NO", "NOT NULL") else "nullable"
            pk_str = " (primary key)" if str(primary_key).upper() in ("Y", "YES") else ""
            comment_str = f" -- {comment}" if comment else ""
            lines.append(f"  {name}  {dtype}  {null_str}{pk_str}{comment_str}")
        return "Table schema (DESCRIBE TABLE output):\n" + "\n".join(lines)
    except Exception as e:
        logger.warning(f"[Cortex] Could not fetch schema for {database}.{schema}.{table}: {e}")
        return ""


def _build_prompt(
    rule_code: str,
    rule_description: str,
    finding_title: str,
    fqn: str,
    column_name: str,
    data_type: str,
    evidence: dict,
    schema_context: str,
) -> str:
    lines = [
        f"Finding: {finding_title}",
        f"Rule: {rule_code}" + (f" — {rule_description}" if rule_description else ""),
        f"Table: {fqn}",
    ]
    if column_name:
        lines.append(f"Column: {column_name}" + (f" (type: {data_type})" if data_type else ""))
    if evidence:
        lines.append(f"Evidence: {json.dumps(evidence, default=str)}")
    if schema_context:
        lines.append("")
        lines.append(schema_context)
    lines += [
        "",
        "IMPORTANT: sql_query must be ONE executable fix — no Options, no verification SELECTs, no alternatives.",
        "If two DDL steps are needed (e.g. ADD COLUMN then UPDATE), separate with semicolons.",
        "",
        "CRITICAL: If the fix requires referencing another table (e.g. FOREIGN KEY REFERENCES),",
        "DO NOT guess or invent table names. Instead use the placeholder <REFERENCED_TABLE>(<REFERENCED_COLUMN>).",
        "Example: FOREIGN KEY (BATCH_ID) REFERENCES <REFERENCED_TABLE>(<REFERENCED_COLUMN>) NOT ENFORCED RELY",
        "The user will replace the placeholder with the correct table before executing.",
        "",
        'Respond with JSON only:',
        '{"explanation":"...","sql_query":"...","confidence":85,"impact":"..."}',
    ]
    return "\n".join(lines)


def _extract_json(text: str) -> dict:
    import re
    # Strip markdown fences
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        return json.loads(match.group(1))
    # Bare JSON object
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        return json.loads(match.group(0))
    return {}


def _sanitize_sql(sql: str) -> str:
    """
    Fix Snowflake DDL anti-patterns that AI models consistently generate wrong.

    Two key issues:
    1. DEFAULT CURRENT_TIMESTAMP() — parens invalid in DEFAULT clauses → strip parens
    2. ALTER TABLE ADD COLUMN ... DEFAULT CURRENT_TIMESTAMP — Snowflake allows DEFAULT
       with literal values in ALTER TABLE but NOT with function-based defaults.
       Safe pattern: add column without DEFAULT, then UPDATE to backfill.
    """
    import re

    # Step 1: strip parens from zero-arg functions inside DEFAULT clauses
    sql = re.sub(r'(DEFAULT\s+)CURRENT_TIMESTAMP\s*\(\s*\)', r'\1CURRENT_TIMESTAMP', sql, flags=re.IGNORECASE)
    sql = re.sub(r'(DEFAULT\s+)CURRENT_DATE\s*\(\s*\)', r'\1CURRENT_DATE', sql, flags=re.IGNORECASE)
    sql = re.sub(r'(DEFAULT\s+)CURRENT_TIME\s*\(\s*\)', r'\1CURRENT_TIME', sql, flags=re.IGNORECASE)
    sql = re.sub(r'(DEFAULT\s+)SYSDATE\s*\(\s*\)', r'\1CURRENT_TIMESTAMP', sql, flags=re.IGNORECASE)
    sql = re.sub(r'(DEFAULT\s+)NOW\s*\(\s*\)', r'\1CURRENT_TIMESTAMP', sql, flags=re.IGNORECASE)
    sql = re.sub(r'(DEFAULT\s+)GETDATE\s*\(\s*\)', r'\1CURRENT_TIMESTAMP', sql, flags=re.IGNORECASE)
    # Catch-all for any remaining zero-arg function in DEFAULT
    sql = re.sub(r'(DEFAULT\s+[A-Z_][A-Z0-9_]*)\s*\(\s*\)', r'\1', sql, flags=re.IGNORECASE)

    # Step 2: Strip DEFAULT CURRENT_TIMESTAMP from ALTER TABLE ADD COLUMN.
    # Snowflake rejects function-based defaults in ALTER TABLE ADD COLUMN even without parens.
    # If the AI already emits a follow-up UPDATE for backfill, just drop the DEFAULT clause.
    # If there is no follow-up UPDATE, the column will be NULL for existing rows (acceptable
    # since we're adding an audit column, not a mandatory value).
    sql = re.sub(
        r'(ALTER\s+TABLE\s+\S+\s+ADD\s+COLUMN\s+\w+\s+TIMESTAMP[_A-Z0-9]*(?:\(\d+\))?)'
        r'\s+DEFAULT\s+CURRENT_TIMESTAMP\b',
        r'\1',
        sql,
        flags=re.IGNORECASE,
    )

    return sql
