"""SQL Validation Tools -- the mandatory hard gate before any generated SQL

(template or, later, LLM-generated) is allowed to execute against the source
Snowflake connection. Per architecture.md #6 and
docs/deferred-and-future-work.md #1, this was the biggest tracked safety gap:
rule_template_tools.py's SQL is SELECT-only by construction (fixed strings),
but nothing stopped a future LLM-generated or edited-by-user SQL path from
containing something unsafe. This module is that stop.

Uses sqlglot (real parser, added to requirements.txt) rather than regex alone
-- architecture.md #6 explicitly calls for a real parser, and regex alone
can't reliably tell "the column named DROP_REQUEST" apart from "an actual
DROP statement," or catch `SELECT 1; DROP TABLE X` chained after a valid
first statement.

Four checks, composed by validate_sql() into one pass/fail result:
- validate_select_only(sql)         -- exactly one statement, and it must be
                                        a query (SELECT/WITH), not SHOW/
                                        DESCRIBE/anything else.
- detect_forbidden_keywords(sql)    -- token-level scan for the blocked verbs,
                                        independent of parse success -- runs
                                        even on SQL that fails to parse, as a
                                        defense-in-depth layer.
- validate_no_semicolon_chaining(sql) -- rejects multiple statements
                                        (`SELECT 1; DROP TABLE X`), including
                                        a lone trailing semicolon on an
                                        otherwise-multi-statement body.
- validate_allowed_table(sql, allowed_tables) -- every table referenced must
                                        be in the caller-supplied allow-list
                                        (the tables actually discovered/in
                                        scope for this scan), per
                                        architecture.md #6's "only
                                        tables/schemas that were discovered
                                        and are in scope may be referenced."

SHOW/DESCRIBE are listed as "allowed" in the ask (they're how
snowflake_metadata_tools.py inspects structure), but rule SQL itself --
what this module exists to gate -- must be a SELECT/WITH query, since only a
query produces the FAILED_COUNT/TOTAL_COUNT shape rule execution depends on.
validate_select_only() enforces that narrower bar; SHOW/DESCRIBE fail it
(they're not SELECT) but are still recognized as safe, non-mutating
statements by detect_forbidden_keywords() and validate_sql()'s keyword check.

This module has no opinion on *how* a rule's SQL was produced -- it is called
after generation (template or LLM), before storage/execution. Wiring it into
storage_tools.store_recommended_rule() / a future execution engine is the
next step, not done here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError

# Verbs that must never appear in rule SQL -- covers every mutating/DDL/
# privilege statement type, plus CALL (arbitrary stored procedure execution)
# and "USE ROLE" (privilege-context switching). Matched at the token level
# via sqlglot's tokenizer, not substring search, so a column literally named
# DROP_REQUEST or a string literal containing 'UPDATE' does not false-positive
# (verified directly: sqlglot tokenizes SELECT DROP_REQUEST FROM FOO as VAR,
# not the DROP keyword; a string literal 'UPDATE' tokenizes as STRING, not
# UPDATE).
_FORBIDDEN_KEYWORDS = frozenset(
    {
        "INSERT",
        "UPDATE",
        "DELETE",
        "DROP",
        "ALTER",
        "CREATE",
        "MERGE",
        "TRUNCATE",
        "COPY",
        "CALL",
        "GRANT",
        "REVOKE",
        "USE",
    }
)


@dataclass
class ValidationResult:
    """Result of one validation check. `errors` is empty iff `is_valid`."""

    is_valid: bool
    errors: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.is_valid


def _parse_single_statement(sql: str) -> tuple[Any, list[str]]:
    """Parse sql as exactly one Snowflake statement.

    Returns (statement_or_None, errors). Centralizes the "did this parse,
    and was it exactly one statement" logic that validate_select_only() and
    validate_no_semicolon_chaining() both need, so they agree on what
    counts as a parse failure vs. a multi-statement body.
    """
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
    """Rule SQL must be exactly one SELECT/WITH query -- nothing else.

    SHOW/DESCRIBE are valid Snowflake read-only statements elsewhere in this
    codebase, but they don't produce the FAILED_COUNT/TOTAL_COUNT row shape a
    rule check needs, so they fail this specific check even though they pass
    detect_forbidden_keywords(). This is deliberately the narrowest of the
    four checks.
    """
    statement, errors = _parse_single_statement(sql)
    if statement is None:
        return ValidationResult(is_valid=False, errors=errors)

    if not isinstance(statement, exp.Query):
        return ValidationResult(
            is_valid=False,
            errors=[
                f"SQL must be a SELECT/WITH query; got {type(statement).__name__}"
            ],
        )

    return ValidationResult(is_valid=True)


def detect_forbidden_keywords(sql: str) -> ValidationResult:
    """Token-level scan for forbidden verbs (see _FORBIDDEN_KEYWORDS).

    Deliberately independent of parse success -- runs on the raw token
    stream, not the parsed AST, so it still catches a forbidden verb even in
    SQL that's otherwise malformed (e.g. `SELECT 1; DROP TABLE X` -- the
    DROP is caught here even though validate_select_only() would reject this
    for a different reason: multiple statements). Two independent checks
    catching the same attack from different angles is intentional defense in
    depth, not redundancy to remove.

    "USE ROLE" is blocked by flagging the USE token alone (Snowflake's
    `USE` statement has no other read-only mutation-free form worth
    special-casing here -- USE WAREHOUSE/DATABASE/SCHEMA change session
    context just as unexpectedly for a rule-check statement, so all USE
    forms are blocked, not just USE ROLE specifically).
    """
    if not sql or not sql.strip():
        return ValidationResult(is_valid=False, errors=["SQL is empty"])

    try:
        tokens = sqlglot.tokenize(sql, read="snowflake")
    except TokenError as exc:
        return ValidationResult(is_valid=False, errors=[f"SQL failed to tokenize: {exc}"])

    found = sorted(
        {tok.text.upper() for tok in tokens if tok.text.upper() in _FORBIDDEN_KEYWORDS}
    )
    if found:
        return ValidationResult(
            is_valid=False,
            errors=[f"Forbidden keyword(s) found: {', '.join(found)}"],
        )

    return ValidationResult(is_valid=True)


def validate_no_semicolon_chaining(sql: str) -> ValidationResult:
    """Reject multiple statements, however they're separated.

    Delegates to the same parse used by validate_select_only() -- sqlglot's
    parser already splits on statement-terminating semicolons and reports
    each resulting statement, including catching a lone trailing semicolon
    on a body that turns out to hide a second statement. A single trailing
    semicolon with nothing after it (`SELECT 1;`) is fine -- verified
    directly, sqlglot parses that as exactly one statement.
    """
    statement, errors = _parse_single_statement(sql)
    if statement is None:
        return ValidationResult(is_valid=False, errors=errors)
    return ValidationResult(is_valid=True)


def validate_allowed_table(sql: str, allowed_tables: list[str]) -> ValidationResult:
    """Every table referenced in sql must be in allowed_tables.

    allowed_tables entries are fully-qualified `DATABASE.SCHEMA.TABLE`
    (case-insensitive) -- the set of tables actually discovered/in scope for
    the current scan, per architecture.md #6 ("only tables/schemas that were
    discovered and are in scope may be referenced"). A rule's SQL should
    only ever reference the one table it was generated for (see
    rule_template_tools.py's _fqn()), so this catches SQL that's drifted
    from its own rule metadata, whether via a bug or a manual edit.

    A table reference must be fully qualified (database.schema.table) to be
    checked meaningfully -- a bare, unqualified table name is rejected
    outright rather than guessed at, since this codebase's own SQL
    generation (_fqn() in rule_template_tools.py) always fully qualifies.
    """
    statement, errors = _parse_single_statement(sql)
    if statement is None:
        return ValidationResult(is_valid=False, errors=errors)

    allowed = {t.upper() for t in allowed_tables}
    referenced = sorted(
        {
            f"{tbl.catalog}.{tbl.db}.{tbl.name}".upper()
            for tbl in statement.find_all(exp.Table)
        }
    )

    # A reference missing catalog/db (e.g. an unqualified table name) is
    # rejected here too -- sqlglot leaves catalog/db as empty strings when
    # absent, which produces a blank segment ("".TABLE or DB."") rather than
    # a KeyError, so it must be checked explicitly rather than relying on a
    # lookup miss.
    disallowed = [
        ref
        for ref in referenced
        if ref not in allowed or any(part == "" for part in ref.split("."))
    ]

    if disallowed:
        return ValidationResult(
            is_valid=False,
            errors=[
                "SQL references table(s) not in the allowed scope: "
                f"{', '.join(disallowed)}"
            ],
        )

    if not referenced:
        return ValidationResult(
            is_valid=False, errors=["SQL references no table at all"]
        )

    return ValidationResult(is_valid=True)


def validate_sql(sql: str, allowed_tables: list[str] | None = None) -> ValidationResult:
    """Run every applicable check and combine into one pass/fail result.

    This is the actual hard gate a caller (storage_tools before persisting a
    recommended rule, or a future rule execution engine before running one)
    should call -- the four functions above are its building blocks, kept
    individually callable because the ask lists them as separate functions
    and because detect_forbidden_keywords() in particular is useful standalone
    (it degrades gracefully on unparseable SQL, the others don't).

    allowed_tables is optional here (None skips that check) because not every
    caller has a scope list on hand yet -- e.g. validating a template's SQL
    right after generation, before it's tied to a scan's discovered-table
    list. Callers that do have a scope (storage before persisting, execution
    before running) should always pass it.
    """
    errors: list[str] = []

    for result in (
        detect_forbidden_keywords(sql),
        validate_no_semicolon_chaining(sql),
        validate_select_only(sql),
    ):
        errors.extend(result.errors)

    if allowed_tables is not None:
        errors.extend(validate_allowed_table(sql, allowed_tables).errors)

    # validate_no_semicolon_chaining()/validate_select_only() both delegate
    # to _parse_single_statement() and so raise the same "failed to parse" /
    # "N statements" error independently -- dedupe while preserving order
    # (dict.fromkeys) so a caller displaying errors doesn't see the same
    # message three times for one root cause.
    errors = list(dict.fromkeys(errors))

    return ValidationResult(is_valid=not errors, errors=errors)
