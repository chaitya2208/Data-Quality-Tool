"""
SQL Validation — the mandatory hard gate before any RULE_INSTANCES.rule_sql
(template-rendered or Claude-drafted) is allowed to execute against the
source Snowflake connection.

Uses sqlglot (real parser) rather than regex — regex alone can't reliably
tell "a column literally named DROP_REQUEST" apart from an actual DROP
statement, or catch `SELECT 1; DROP TABLE X` chained after a valid first
statement.

Four checks, composed by validate_sql() into one pass/fail result:
- validate_select_only(sql)           — exactly one statement, and it must
                                         be a query (SELECT/WITH).
- detect_forbidden_keywords(sql)      — token-level scan for blocked verbs,
                                         independent of parse success.
- validate_no_semicolon_chaining(sql) — rejects multiple statements.
- validate_allowed_table(sql, allowed_tables) — every table referenced must
                                         be in the caller-supplied allow-list.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError

_FORBIDDEN_KEYWORDS = frozenset({
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "MERGE",
    "TRUNCATE", "COPY", "CALL", "GRANT", "REVOKE", "USE",
})


@dataclass
class ValidationResult:
    is_valid: bool
    errors: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.is_valid


def _parse_single_statement(sql: str) -> tuple[Any, list[str]]:
    if not sql or not sql.strip():
        return None, ["SQL is empty"]
    try:
        statements = [s for s in sqlglot.parse(sql, read="snowflake") if s is not None]
    except (ParseError, TokenError) as exc:
        return None, [f"SQL failed to parse: {exc}"]
    if not statements:
        return None, ["SQL is empty"]
    if len(statements) > 1:
        return None, [
            f"SQL contains {len(statements)} statements; only one is allowed "
            "(possible semicolon-chained statements)"
        ]
    return statements[0], []


def validate_select_only(sql: str) -> ValidationResult:
    statement, errors = _parse_single_statement(sql)
    if statement is None:
        return ValidationResult(is_valid=False, errors=errors)
    if not isinstance(statement, exp.Query):
        return ValidationResult(
            is_valid=False,
            errors=[f"SQL must be a SELECT/WITH query; got {type(statement).__name__}"],
        )
    return ValidationResult(is_valid=True)


def detect_forbidden_keywords(sql: str) -> ValidationResult:
    if not sql or not sql.strip():
        return ValidationResult(is_valid=False, errors=["SQL is empty"])
    try:
        tokens = sqlglot.tokenize(sql, read="snowflake")
    except TokenError as exc:
        return ValidationResult(is_valid=False, errors=[f"SQL failed to tokenize: {exc}"])
    found = sorted({tok.text.upper() for tok in tokens if tok.text.upper() in _FORBIDDEN_KEYWORDS})
    if found:
        return ValidationResult(is_valid=False, errors=[f"Forbidden keyword(s) found: {', '.join(found)}"])
    return ValidationResult(is_valid=True)


def validate_no_semicolon_chaining(sql: str) -> ValidationResult:
    statement, errors = _parse_single_statement(sql)
    if statement is None:
        return ValidationResult(is_valid=False, errors=errors)
    return ValidationResult(is_valid=True)


def validate_allowed_table(sql: str, allowed_tables: list[str]) -> ValidationResult:
    """allowed_tables entries are fully-qualified DATABASE.SCHEMA.TABLE
    (case-insensitive)."""
    statement, errors = _parse_single_statement(sql)
    if statement is None:
        return ValidationResult(is_valid=False, errors=errors)

    allowed = {t.upper() for t in allowed_tables}
    referenced = sorted({
        f"{tbl.catalog}.{tbl.db}.{tbl.name}".upper()
        for tbl in statement.find_all(exp.Table)
    })

    disallowed = [
        ref for ref in referenced
        if ref not in allowed or any(part == "" for part in ref.split("."))
    ]
    if disallowed:
        return ValidationResult(
            is_valid=False,
            errors=[f"SQL references table(s) not in the allowed scope: {', '.join(disallowed)}"],
        )
    if not referenced:
        return ValidationResult(is_valid=False, errors=["SQL references no table at all"])
    return ValidationResult(is_valid=True)


def validate_sql(sql: str, allowed_tables: Optional[list[str]] = None) -> ValidationResult:
    """The actual hard gate. allowed_tables is optional — pass it whenever
    the caller knows the scan's target table(s)."""
    errors: list[str] = []
    for result in (
        detect_forbidden_keywords(sql),
        validate_no_semicolon_chaining(sql),
        validate_select_only(sql),
    ):
        errors.extend(result.errors)
    if allowed_tables is not None:
        errors.extend(validate_allowed_table(sql, allowed_tables).errors)
    errors = list(dict.fromkeys(errors))
    return ValidationResult(is_valid=not errors, errors=errors)
