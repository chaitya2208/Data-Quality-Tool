"""Metadata Agent -- first deterministic agent in the scan pipeline.

Wraps tools/snowflake_metadata_tools.describe_table() in the input/output
shape the ask specifies. Deliberately thin: no new discovery logic here,
the tool layer already does the real work (and is the thing verified
against real Snowflake). "Agent" here means "one step of the pipeline with
a fixed input/output contract," not a new query -- consistent with
mvp-scope.md's LangGraph-later stance: these are plain Python functions,
not LLM calls or graph nodes, yet.

Deviation from the literal ask: the requested input shape is
{"database": ..., "schema": ..., "table": ...}, but every existing tool in
this codebase (metadata/profiling/storage/rule_template) takes
database_name/schema_name/table_name as separate params, not a nested dict
or a combined table_fqn (snowflake_profiling_tools.py's docstring already
flagged table_fqn as a superseded convention from a prior session). To stay
consistent, run_metadata_agent() takes the same three plain string params
every other function in this codebase takes; a thin wrapper adapts the
literal request/response dict shape at the very edge (main.py's future
route), not threaded through internal call signatures.
"""

from __future__ import annotations

from typing import Any

from tools.snowflake_metadata_tools import describe_table


def run_metadata_agent(
    database_name: str, schema_name: str, table_name: str
) -> dict[str, Any]:
    """Discover one table's columns. Read-only (DESCRIBE TABLE only).

    Output: {"metadata": {"database", "schema", "table", "columns": [...]}}
    columns is describe_table()'s existing shape (column_name, data_type,
    is_nullable, default, primary_key, unique_key, comment) -- not narrowed,
    since the Profiling/Rule Recommendation agents downstream may need any
    of those fields later (e.g. primary_key already hints at uniqueness).
    """
    columns = describe_table(database_name, schema_name, table_name)
    return {
        "metadata": {
            "database": database_name,
            "schema": schema_name,
            "table": table_name,
            "columns": columns,
        }
    }
