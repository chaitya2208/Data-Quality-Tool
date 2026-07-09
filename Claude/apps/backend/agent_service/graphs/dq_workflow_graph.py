"""First LangGraph workflow -- the same agent chain as agents/scan_pipeline.py,
expressed as a LangGraph StateGraph instead of a plain function chain, per
architecture.md's committed direction (Python + LangGraph,
state/checkpoints/retries/human-in-the-loop as first-class) and
mvp-scope.md's suggested build order (#10: "LangGraph recommendation flow
tying [templates through scoring] together").

    START -> metadata_agent -> profiling_agent -> pii_agent
          -> rule_recommendation_agent -> sql_generation_agent
          -> sql_validation_agent -> rule_test_execution_agent -> END

pii_agent (added later, per architecture.md §4a's pipeline order --
"Profiling -> PII/Sensitivity -> Rule Recommendation") classifies every
column's PII/sensitivity before rule_recommendation_agent's Claude call
sees the profile, so tools/claude_tools.py's _mask_column_profile() (which
already existed but was permanently a no-op -- every column's is_pii was
always False) finally has real classifications to mask against. See
agents/pii_agent.py's docstring for the two-tier (deterministic +
LLM-assist) design.

rule_test_execution_agent (added later, per architecture.md §4a / mvp-scope.md
invariant #5 -- every rule is test-run before a human sees it) test-runs each
VALID rule's SQL against the source connection right now, so the returned
rules carry test_status/test_result (would-it-pass, fail count/%) alongside
validation_status -- see agents/rule_test_execution_agent.py's docstring.

Each node wraps the *same* agents/*.py functions scan_pipeline.py calls --
no new agent logic here, only the graph wiring and the state shape. Kept as
a second, parallel entry point rather than replacing scan_pipeline.py: the
ask explicitly separated "build the deterministic agents" from "build the
LangGraph workflow" as two milestones, and scan_pipeline.py is simpler to
call from a script/test than a compiled graph -- no reason to delete a
working plain-Python path once the graph exists alongside it.

State shape follows the ask, with one adjustment flagged: the ask's
DQWorkflowState uses `schema_name` (not `schema`) to avoid shadowing
Python's stdlib/pydantic-adjacent `schema` name -- kept as given. `table_fqn`
is computed once at graph entry (build_initial_state()) purely for display/
logging; every node still calls the underlying agents with
database/schema_name/table_name separately, matching every other tool in
this codebase (see metadata_agent.py's docstring on why table_fqn isn't
threaded through internals).

Errors: per architecture.md, agents should not crash the whole run over one
bad step. Each node catches exceptions from its own agent call, appends a
structured entry to `errors`, and returns the state otherwise unchanged so
downstream nodes still run (e.g. a profiling failure still lets
recommendation/generation/validation run against an empty column_profiles
list, rather than aborting the graph) -- consistent with orchestrator
guidance elsewhere in this codebase to flag/record failures rather than
hide them, without letting one step's failure silently blank out the rest
of the pipeline.
"""

from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from agents.metadata_agent import run_metadata_agent
from agents.pii_agent import run_pii_agent
from agents.profiling_agent import run_profiling_agent
from agents.rule_recommendation_agent import run_rule_recommendation_agent
from agents.rule_test_execution_agent import run_rule_test_execution_agent
from agents.sql_generation_agent import run_sql_generation_agent
from agents.sql_validation_agent import run_sql_validation_agent
from tools.storage_tools import log_agent_run


class DQWorkflowState(TypedDict):
    scan_id: str
    database: str
    schema_name: str
    table_name: str
    table_fqn: str

    metadata: dict
    table_profile: dict
    column_profiles: list[dict]

    recommended_rules: list[dict]
    generated_rules: list[dict]
    validated_rules: list[dict]
    tested_rules: list[dict]

    table_classification: dict | None

    errors: list[dict]


def build_initial_state(
    scan_id: str, database: str, schema_name: str, table_name: str
) -> DQWorkflowState:
    """Build a DQWorkflowState with every field present (LangGraph nodes
    return partial dict updates, but the graph's starting state should not
    have missing keys for nodes that read before they write, e.g.
    profiling_agent_node reading state["metadata"] if a future node wanted
    it).
    """
    return {
        "scan_id": scan_id,
        "database": database,
        "schema_name": schema_name,
        "table_name": table_name,
        "table_fqn": f"{database}.{schema_name}.{table_name}",
        "metadata": {},
        "table_profile": {},
        "column_profiles": [],
        "recommended_rules": [],
        "generated_rules": [],
        "validated_rules": [],
        "tested_rules": [],
        "table_classification": None,
        "errors": [],
    }


def _record_error(state: DQWorkflowState, node_name: str, exc: Exception) -> dict[str, Any]:
    return {"errors": [*state["errors"], {"node": node_name, "message": str(exc)}]}


def _log(state: DQWorkflowState, agent_name: str, step_name: str, status: str, message: str) -> None:
    # Best-effort: a logging failure must not break the scan itself -- same
    # "must not fail the pipeline" convention this codebase applies to the
    # Claude/Bedrock calls (see rule_recommendation_agent.py).
    try:
        log_agent_run(state["scan_id"], agent_name, step_name, status, message=message)
    except Exception:  # noqa: BLE001
        pass


def metadata_agent_node(state: DQWorkflowState) -> dict[str, Any]:
    _log(state, "metadata_agent", "METADATA_DISCOVERY", "STARTED", "Metadata discovery started")
    try:
        result = run_metadata_agent(state["database"], state["schema_name"], state["table_name"])
        _log(state, "metadata_agent", "METADATA_DISCOVERY", "COMPLETED", "Metadata discovery completed")
        return {"metadata": result["metadata"]}
    except Exception as exc:  # noqa: BLE001 -- see module docstring on error handling
        _log(state, "metadata_agent", "METADATA_DISCOVERY", "FAILED", str(exc))
        return _record_error(state, "metadata_agent", exc)


def profiling_agent_node(state: DQWorkflowState) -> dict[str, Any]:
    _log(state, "profiling_agent", "PROFILING", "STARTED", "Profiling started")
    try:
        result = run_profiling_agent(
            state["database"],
            state["schema_name"],
            state["table_name"],
            metadata=state["metadata"],
        )
        _log(state, "profiling_agent", "PROFILING", "COMPLETED", "Profiling completed")
        return {
            "column_profiles": result["column_profiles"],
            "table_profile": result["table"],
        }
    except Exception as exc:  # noqa: BLE001
        _log(state, "profiling_agent", "PROFILING", "FAILED", str(exc))
        return _record_error(state, "profiling_agent", exc)


def pii_agent_node(state: DQWorkflowState) -> dict[str, Any]:
    _log(state, "pii_agent", "PII_CLASSIFICATION", "STARTED", "PII classification started")
    try:
        result = run_pii_agent(
            state["database"],
            state["schema_name"],
            state["table_name"],
            state["column_profiles"],
        )
        pii_count = sum(1 for c in result["column_profiles"] if c.get("is_pii"))
        _log(
            state,
            "pii_agent",
            "PII_CLASSIFICATION",
            "COMPLETED",
            f"PII classification completed ({pii_count} PII columns)",
        )
        return {"column_profiles": result["column_profiles"]}
    except Exception as exc:  # noqa: BLE001
        _log(state, "pii_agent", "PII_CLASSIFICATION", "FAILED", str(exc))
        return _record_error(state, "pii_agent", exc)


def rule_recommendation_agent_node(state: DQWorkflowState) -> dict[str, Any]:
    _log(state, "rule_recommendation_agent", "RULE_RECOMMENDATION", "STARTED", "Rule recommendation started")
    try:
        row_count = state["table_profile"].get("row_count", 0) or 0
        result = run_rule_recommendation_agent(
            state["database"],
            state["schema_name"],
            state["table_name"],
            row_count,
            state["column_profiles"],
        )
        claude_error = result.get("claude_error")
        completed_msg = (
            f"Rules recommended ({len(result['recommended_rules'])}, template-only — Claude call failed: {claude_error})"
            if claude_error
            else f"Rules recommended ({len(result['recommended_rules'])})"
        )
        _log(state, "rule_recommendation_agent", "RULE_RECOMMENDATION", "COMPLETED", completed_msg)
        return {
            "recommended_rules": result["recommended_rules"],
            "table_classification": result.get("table_classification"),
        }
    except Exception as exc:  # noqa: BLE001
        _log(state, "rule_recommendation_agent", "RULE_RECOMMENDATION", "FAILED", str(exc))
        return _record_error(state, "rule_recommendation_agent", exc)


def sql_generation_agent_node(state: DQWorkflowState) -> dict[str, Any]:
    _log(state, "sql_generation_agent", "SQL_GENERATION", "STARTED", "SQL generation started")
    try:
        result = run_sql_generation_agent(state["recommended_rules"])
        _log(state, "sql_generation_agent", "SQL_GENERATION", "COMPLETED", "SQL generated")
        return {"generated_rules": result["rules"]}
    except Exception as exc:  # noqa: BLE001
        _log(state, "sql_generation_agent", "SQL_GENERATION", "FAILED", str(exc))
        return _record_error(state, "sql_generation_agent", exc)


def sql_validation_agent_node(state: DQWorkflowState) -> dict[str, Any]:
    _log(state, "sql_validation_agent", "SQL_VALIDATION", "STARTED", "SQL validation started")
    try:
        result = run_sql_validation_agent(state["generated_rules"])
        _log(state, "sql_validation_agent", "SQL_VALIDATION", "COMPLETED", "SQL validated")
        return {"validated_rules": result["rules"]}
    except Exception as exc:  # noqa: BLE001
        _log(state, "sql_validation_agent", "SQL_VALIDATION", "FAILED", str(exc))
        return _record_error(state, "sql_validation_agent", exc)


def rule_test_execution_agent_node(state: DQWorkflowState) -> dict[str, Any]:
    _log(state, "rule_test_execution_agent", "RULE_TEST_EXECUTION", "STARTED", "Testing started")
    try:
        result = run_rule_test_execution_agent(state["validated_rules"], state["column_profiles"])
        _log(state, "rule_test_execution_agent", "RULE_TEST_EXECUTION", "COMPLETED", "Testing completed")
        return {"tested_rules": result["rules"]}
    except Exception as exc:  # noqa: BLE001
        _log(state, "rule_test_execution_agent", "RULE_TEST_EXECUTION", "FAILED", str(exc))
        return _record_error(state, "rule_test_execution_agent", exc)


def build_dq_workflow_graph():
    """Compile the graph. Returns a CompiledStateGraph -- callers use
    .invoke(build_initial_state(...)) to run it once end to end (no
    human-in-the-loop interrupt in this first graph; approval stays a
    separate async API step per architecture.md #8, not a graph pause).
    """
    graph = StateGraph(DQWorkflowState)

    graph.add_node("metadata_agent", metadata_agent_node)
    graph.add_node("profiling_agent", profiling_agent_node)
    graph.add_node("pii_agent", pii_agent_node)
    graph.add_node("rule_recommendation_agent", rule_recommendation_agent_node)
    graph.add_node("sql_generation_agent", sql_generation_agent_node)
    graph.add_node("sql_validation_agent", sql_validation_agent_node)
    graph.add_node("rule_test_execution_agent", rule_test_execution_agent_node)

    graph.add_edge(START, "metadata_agent")
    graph.add_edge("metadata_agent", "profiling_agent")
    graph.add_edge("profiling_agent", "pii_agent")
    graph.add_edge("pii_agent", "rule_recommendation_agent")
    graph.add_edge("rule_recommendation_agent", "sql_generation_agent")
    graph.add_edge("sql_generation_agent", "sql_validation_agent")
    graph.add_edge("sql_validation_agent", "rule_test_execution_agent")
    graph.add_edge("rule_test_execution_agent", END)

    return graph.compile()


# Compiled once at import time -- the graph structure is static, no reason
# to recompile it on every request. Same pattern as this codebase's
# module-level connection caching (tools/snowflake_connection.py).
dq_workflow_graph = build_dq_workflow_graph()


def run_dq_workflow(scan_id: str, database: str, schema_name: str, table_name: str) -> dict[str, Any]:
    """Run the compiled graph once for one table. Returns the final state."""
    initial_state = build_initial_state(scan_id, database, schema_name, table_name)
    return dq_workflow_graph.invoke(initial_state)
