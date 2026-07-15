"""
Shared batch-start logic for workflow runs.

Both the HTTP endpoint (POST /agent/runs/batch) and the scheduler
(schedule_runner) call run_batch() so a scheduled run behaves identically to a
manual one: one AgentRun per table sharing a batch_id, processed sequentially
(the WorkflowCoordinator auto-advances the rest). Scope expansion lives here so
callers only pass a scope + target, not a pre-enumerated table list.
"""
import threading
import uuid
import logging
from typing import List, Optional, Tuple

from app.services import storage
from app.services.datasources import get_source
from app.services.agents.coordinator import WorkflowCoordinator, DB_AGENT_ORDER

logger = logging.getLogger(__name__)


def expand_scope(
    source,
    scope: str,
    database: str,
    schema_name: Optional[str],
    table: Optional[str],
) -> List[tuple]:
    """Resolve a scope into an ordered list of (database, schema, table).

    Raises ValueError on invalid input (callers translate to HTTP 400).
    """
    scope = (scope or "table").lower()

    if scope == "table":
        if not (schema_name and table):
            raise ValueError("scope=table requires schema_name and table")
        return [(database, schema_name, table)]

    if scope == "schema":
        if not schema_name:
            raise ValueError("scope=schema requires schema_name")
        tables = [t["name"] for t in source.list_tables(database, schema_name)]
        return [(database, schema_name, t) for t in tables]

    if scope == "database":
        targets: List[tuple] = []
        for sch in source.list_schemas(database):
            for t in source.list_tables(database, sch):
                targets.append((database, sch, t["name"]))
        return targets

    raise ValueError(f"Unknown scope '{scope}'")


def run_batch(
    connection_id: Optional[str],
    scope: str,
    database: str,
    schema_name: Optional[str] = None,
    table: Optional[str] = None,
    workflow_template_id: Optional[str] = None,
    schedule_id: Optional[str] = None,
) -> Tuple[str, List]:
    """
    Expand the scope, create one pending AgentRun per target sharing a batch_id,
    seed their tasks, and kick off the first run's coordinator in a daemon
    thread. Returns (batch_id, [AgentRun, ...]).

    Raises ValueError for invalid scope input and propagates any error from
    scope enumeration (bad connection, unreachable source) to the caller.
    """
    source = get_source(connection_id)
    targets = expand_scope(source, scope, database, schema_name, table)
    if not targets:
        raise ValueError("No tables found for the selected scope")

    batch_id = str(uuid.uuid4())
    runs = []
    for idx, (db, sch, tbl) in enumerate(targets):
        run = storage.create_agent_run(
            connection_id=connection_id,
            database=db,
            schema_name=sch,
            table=tbl,
            status="pending",
            batch_id=batch_id,
            batch_index=idx,
            workflow_template_id=workflow_template_id,
            schedule_id=schedule_id,
        )
        storage.create_agent_tasks(run.id, DB_AGENT_ORDER)
        runs.append(storage.get_agent_run(run.id))

    # Kick off only the first table — the coordinator advances the rest sequentially
    first = runs[0]
    threading.Thread(target=WorkflowCoordinator(run_id=first.id).run, daemon=True).start()

    logger.info(
        f"[BatchRunner] Started batch {batch_id} scope={scope} — {len(runs)} tables, "
        f"first={first.database}.{first.schema_name}.{first.table}"
    )
    return batch_id, runs
