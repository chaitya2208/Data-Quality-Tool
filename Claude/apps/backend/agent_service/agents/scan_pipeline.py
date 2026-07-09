"""Scan pipeline -- chains the deterministic agents for one table.

Definition of done (per the ask): "For one table, system can scan -> profile
-> recommend rules -> generate SQL -> validate SQL." This is that chain, plus
Rule Test Execution (added later, per architecture.md §4a / mvp-scope.md
invariant #5 -- every rule is test-run before a human sees it):

    Metadata Agent -> Profiling Agent -> Rule Recommendation Agent v1
        -> SQL Generation Agent -> SQL Validation Agent
        -> Rule Test Execution Agent

Deliberately does NOT persist anything to the app-owned DB (no scan_id, no
storage_tools calls) -- the ask's definition of done is about the agents
themselves working end to end in memory, not about storage. Wiring this into
a real scan (CORE.SCAN_RUNS + store_recommended_rule() per validated rule,
with VALID/INVALID mapped to storage's PASSED/FAILED vocabulary) is the
natural next step, tracked in docs/deferred-and-future-work.md rather than
built here -- keeps this step scoped to what was actually asked, matching
this project's "flag deviations, don't silently expand scope" convention.

Not LangGraph (per mvp-scope.md / context.md's explicit instruction not to
introduce that yet, and this task's own "do not start with Claude first" --
these are plain deterministic Python functions called in sequence).
"""

from __future__ import annotations

from typing import Any

from agents.metadata_agent import run_metadata_agent
from agents.profiling_agent import run_profiling_agent
from agents.rule_recommendation_agent import run_rule_recommendation_agent
from agents.rule_test_execution_agent import run_rule_test_execution_agent
from agents.sql_generation_agent import run_sql_generation_agent
from agents.sql_validation_agent import run_sql_validation_agent


def run_scan_pipeline(
    database_name: str, schema_name: str, table_name: str
) -> dict[str, Any]:
    """Run the full deterministic pipeline for one table.

    Output:
    {
      "metadata": {...},               # Metadata Agent output
      "column_profiles": [...],        # Profiling Agent output
      "table": {"row_count", "column_count"},
      "recommended_rules": [...],      # each with generated_sql,
                                        # validation_status, validation_errors,
                                        # test_status, test_result
    }
    """
    metadata_result = run_metadata_agent(database_name, schema_name, table_name)

    profiling_result = run_profiling_agent(
        database_name, schema_name, table_name, metadata=metadata_result["metadata"]
    )
    column_profiles = profiling_result["column_profiles"]
    table_stats = profiling_result["table"]

    recommendation_result = run_rule_recommendation_agent(
        database_name,
        schema_name,
        table_name,
        table_stats["row_count"],
        column_profiles,
    )

    generation_result = run_sql_generation_agent(recommendation_result["recommended_rules"])
    validation_result = run_sql_validation_agent(generation_result["rules"])
    test_result = run_rule_test_execution_agent(validation_result["rules"], column_profiles)

    return {
        "metadata": metadata_result["metadata"],
        "column_profiles": column_profiles,
        "table": table_stats,
        "recommended_rules": test_result["rules"],
    }
