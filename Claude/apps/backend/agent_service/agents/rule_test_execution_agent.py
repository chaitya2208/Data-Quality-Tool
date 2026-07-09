"""Rule Test Execution Agent -- runs each rule's validated SQL now, so a
human sees expected pass/fail *before* approving (architecture.md §4a "Rule
Test Execution", §5's guardrail "Read-only, with LIMIT/timeout";
mvp-scope.md invariant #5 "every rule is test-run before a human sees it --
no approving blind").

Only rules that already passed SQL validation
(sql_validation_agent.run_sql_validation_agent() -> validation_status=="VALID")
are executed here. A rule that's INVALID (no SQL, or SQL that failed the
safety gate) has nothing safe to run -- it is left at test_status="PENDING",
test_result=None, same "not yet executable" treatment
sql_validation_agent.py/sql_generation_agent.py already give Claude-sourced
rule types the template dispatcher doesn't recognize (see those modules'
docstrings). This agent does not re-validate; it trusts the upstream gate.

Every template in tools/rule_template_tools.py returns exactly one row with
a FAILED_COUNT column, and (per the fix alongside this agent, uniqueness_sql()
included) a TOTAL_COUNT column -- this agent depends on both being present
for every rule it executes, since failure_percentage needs both.

Status logic (per the ask):
    failed_count == 0  -> PASSED
    failed_count > 0   -> FAILED
    SQL raised/errored -> ERROR

Deviation flagged: 04_create_rule_tables.sql's TEST_STATUS comment lists only
"PENDING / PASSED / FAILED" -- no ERROR. This agent still sets ERROR when a
query genuinely fails at execution time (distinct from FAILED, which means
the query ran fine and found failing rows) -- collapsing ERROR into FAILED
would hide "this rule's SQL is broken" behind "this table has bad data",
which are different problems a human needs to tell apart. Same category of
agent-vocabulary-vs-storage-vocabulary gap sql_validation_agent.py already
flags for VALID/INVALID vs. storage's PENDING/PASSED/FAILED -- mapping this
agent's PENDING/PASSED/FAILED/ERROR into whatever storage_tools.py's
TEST_STATUS column ultimately accepts is the storage-wiring caller's job,
not this agent's (see the open storage-wiring questions already tracked for
this project).

Sample failed rows (previously deferred, now built): test_result gains a
sample_failed_rows key, built by tools/sample_query_tools.build_sample_failed_rows()
-- a second, row-returning query per row-level rule type (COMPLETENESS/
UNIQUENESS/VALIDITY), or a table-level evidence fact for FRESHNESS/VOLUME/
no-SQL rule types. Rows are masked per each column's LLM_SHARING_POLICY
(tools/pii_detection_tools.mask_sample_rows()) using the column_profiles this
agent now receives -- pii_agent.py already classified them earlier in the
same pipeline (graphs/dq_workflow_graph.py: profiling -> pii -> recommend ->
... -> this agent), so no extra storage lookup is needed here.
"""

from __future__ import annotations

import datetime
from typing import Any

from tools.sample_query_tools import build_sample_failed_rows
from tools.snowflake_connection import run_query

# Simple aggregate queries (COUNT(*)-shaped, per rule_template_tools.py) --
# 30s is generous for these on any table size actually reachable today
# (mvp-scope.md/deferred-and-future-work.md #4: full-table scans, no
# sampling yet, so a genuinely slow query here is a real signal, not a
# transient blip worth a longer allowance).
_TEST_QUERY_TIMEOUT_SECONDS = 30


def _run_one_test(
    rule: dict[str, Any], column_policies: dict[str, str | None]
) -> tuple[str, dict[str, Any]]:
    """Execute one rule's SQL and return (test_status, test_result)."""
    sql = rule.get("generated_sql")
    evaluated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    try:
        rows = run_query(sql, timeout=_TEST_QUERY_TIMEOUT_SECONDS)
        if not rows:
            return "ERROR", {
                "would_pass": None,
                "total_count": None,
                "failed_count": None,
                "failure_percentage": None,
                "error_message": "SQL executed but returned no rows",
                "evaluated_at": evaluated_at,
                "sample_failed_rows": None,
            }

        row = rows[0]
        failed_count = row.get("FAILED_COUNT")
        total_count = row.get("TOTAL_COUNT")
        failure_percentage = (
            round(failed_count / total_count * 100, 4)
            if failed_count is not None and total_count
            else None
        )

        test_status = "PASSED" if failed_count == 0 else "FAILED"
        sample_failed_rows = build_sample_failed_rows(
            rule, failed_count, total_count, column_policies
        )
        return test_status, {
            "would_pass": failed_count == 0,
            "total_count": total_count,
            "failed_count": failed_count,
            "failure_percentage": failure_percentage,
            "error_message": None,
            "evaluated_at": evaluated_at,
            "sample_failed_rows": sample_failed_rows,
        }
    except Exception as exc:  # noqa: BLE001 -- one rule's SQL failing must not stop the batch
        return "ERROR", {
            "would_pass": None,
            "total_count": None,
            "failed_count": None,
            "failure_percentage": None,
            "error_message": str(exc),
            "evaluated_at": evaluated_at,
            "sample_failed_rows": None,
        }


def run_rule_test_execution_agent(
    rules: list[dict[str, Any]], column_profiles: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Test-run every VALID rule's SQL now. Sets test_status and test_result
    on each rule dict; rules that aren't VALID pass through with
    test_status="PENDING", test_result=None.

    column_profiles (new): this pipeline's already-classified column
    profiles (pii_agent.py runs before this agent -- see
    graphs/dq_workflow_graph.py), used only to build a
    {column_name: llm_sharing_policy} lookup for masking sample failed rows.
    Optional/defaults to None (empty policy map -- see
    tools/sample_query_tools.py) so scan_pipeline.py callers that predate
    this feature don't break; every real caller now passes it.

    Output: {"rules": [...]} -- same rule dicts, each annotated with
    test_status ("PENDING" | "PASSED" | "FAILED" | "ERROR") and test_result
    (dict | None; see module docstring for shape).
    """
    column_policies = {
        c["column_name"]: c.get("llm_sharing_policy") for c in (column_profiles or [])
    }

    tested = []
    for rule in rules:
        if rule.get("validation_status") != "VALID":
            tested.append({**rule, "test_status": "PENDING", "test_result": None})
            continue

        test_status, test_result = _run_one_test(rule, column_policies)
        tested.append({**rule, "test_status": test_status, "test_result": test_result})

    return {"rules": tested}
