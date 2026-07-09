"""Profiling Agent -- second deterministic agent in the scan pipeline.

Wraps tools/snowflake_profiling_tools.profile_table() (the pure-computation
half split out of profile_and_store_table() for exactly this purpose -- see
that module's docstring). Storage is a separate concern the orchestrator
handles, not this agent -- keeps this agent a pure function of its inputs,
matching metadata_agent.py's shape.

Deviation from the literal ask, same reasoning as metadata_agent.py: the
requested input is {"metadata": {...}, "table_fqn": "DB.SCHEMA.TABLE"}, but
table_fqn was already flagged in this codebase (snowflake_profiling_tools.py)
as a superseded convention from a prior session -- every tool function takes
database_name/schema_name/table_name separately. run_profiling_agent() does
the same; a table_fqn string is only ever produced/consumed at the edge
(e.g. logging, UI display), not threaded through internals. The `metadata`
input isn't actually needed to run the profiling queries themselves (the
tool layer re-derives columns via DESCRIBE TABLE internally) -- accepted as
an optional param so a caller can pass the Metadata Agent's output through
without the pipeline recomputing metadata it already has, but not required.
"""

from __future__ import annotations

from typing import Any

from tools.snowflake_profiling_tools import profile_table


def run_profiling_agent(
    database_name: str,
    schema_name: str,
    table_name: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Profile every column of one table. Read-only, no storage.

    Output: {"column_profiles": [...], "table": {"row_count", "column_count"}}
    column_profiles is profile_table()'s existing per-column shape
    (column_name, data_type, null_count, null_percentage, distinct_count,
    min_value, max_value, top_values) -- the same shape the 5 rule skills
    already expect as input, so the Rule Recommendation Agent can pass this
    straight through.

    `table` (row/column counts) is included even though the ask's output
    shape only lists column_profiles -- uniqueness_skill.py and
    volume_skill.py both require row_count as a separate argument (it's not
    part of any one column's profile), so the orchestrator needs it from
    somewhere; returning it here avoids a second call to profile_table_basic().
    """
    result = profile_table(database_name, schema_name, table_name)
    return {
        "column_profiles": result["columns"],
        "table": result["table"],
    }
