"""SQL Validation Agent -- marks each rule's SQL as valid/invalid.

The mandatory hard gate (architecture.md §6, deferred-and-future-work.md #1)
finally gets wired into the pipeline here -- tools/sql_validation_tools.py
existed but nothing called it yet before this agent. Every rule's SQL is
run through validate_sql() (SELECT-only + forbidden-keyword + no-chaining +
allowed-table checks) before it's allowed to reach storage or, later,
execution.

allowed_tables is derived from the rule's own database/schema/table rather
than a separately-supplied scan scope -- every rule produced by the current
pipeline (5 deterministic skills -> rule_template_tools) is generated for,
and should only ever reference, the one table it was scanned from (see
rule_template_tools._fqn()). A rule whose SQL references a *different*
table than its own metadata says is exactly the kind of drift
validate_allowed_table() exists to catch, so scoping the allow-list to
"this rule's own table" per rule (not the whole scan's table list) is the
tighter, more correct check -- a rule for CUSTOMER should never reference
ORDERS, even if both are in-scope for the same scan.
"""

from __future__ import annotations

import json
from typing import Any

from tools.sql_validation_tools import validate_sql


def run_sql_validation_agent(rules: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate every rule's generated_sql. Sets validation_status
    (VALID/INVALID) and validation_errors on each rule dict.

    Output: {"rules": [...]} -- same rule dicts, each annotated with
    validation_status ("VALID" | "INVALID") and validation_errors (list[str],
    empty when valid). Field names deliberately differ from
    RECOMMENDED_INSTANCES.VALIDATION_STATUS's PENDING/PASSED/FAILED vocabulary --
    this agent's job ends at VALID/INVALID; mapping that into the storage
    layer's PASSED/FAILED status string is the caller's (orchestrator's) job,
    not this agent's, since PENDING (the third storage state, "not checked
    yet") never applies to a rule that's already been through this agent.

    For scope=CROSS_TABLE rules (docs/rules-architecture.md §5.7), the
    allowed-table list is expanded to also include the ref table named in
    target_config (ref_database/ref_schema/ref_table) -- a CROSS_TABLE rule's
    SQL legitimately JOINs a second table, so restricting allowed_tables to
    just the rule's own table would make every CROSS_TABLE rule fail
    validate_allowed_table() for referencing "a table not in scope" when
    that second table is in fact the whole point of the check. Every other
    rule (no scope, or scope != CROSS_TABLE -- i.e. every rule in the
    codebase today) keeps the original single-table allow-list unchanged.
    """
    validated = []
    for rule in rules:
        allowed_tables = [
            f"{rule['database_name']}.{rule['schema_name']}.{rule['table_name']}"
        ]

        if rule.get("scope") == "CROSS_TABLE":
            target_config = rule.get("target_config")
            if isinstance(target_config, str):
                try:
                    target_config = json.loads(target_config)
                except (ValueError, TypeError):
                    target_config = None
            if isinstance(target_config, dict):
                ref_database = target_config.get("ref_database")
                ref_schema = target_config.get("ref_schema")
                ref_table = target_config.get("ref_table")
                if ref_database and ref_schema and ref_table:
                    allowed_tables.append(f"{ref_database}.{ref_schema}.{ref_table}")

        result = validate_sql(rule.get("generated_sql", ""), allowed_tables=allowed_tables)
        validated.append(
            {
                **rule,
                "validation_status": "VALID" if result.is_valid else "INVALID",
                "validation_errors": result.errors,
            }
        )

    return {"rules": validated}
