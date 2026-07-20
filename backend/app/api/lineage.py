"""Data Lineage endpoints.

Powers the /lineage page: zoomable graph from all-databases → single-database
→ single-schema → single-table drill-down, with health / findings / rules-run
overlays and a saved-workflow highlight filter.

Snowflake-only in v1. Postgres connections return
{"available": false, "reason": "postgres_unsupported"} with 200 so the UI
renders an empty-state card rather than a spurious 4xx.
"""
from typing import Optional

from fastapi import APIRouter, HTTPException

from app.services import lineage

router = APIRouter()


@router.get("/status")
def get_status(connection_id: Optional[str] = None):
    """Refresh state per database + GET_LINEAGE capability probe result.
    Feeds the discovery-method badge + per-DB 'last refreshed' chips."""
    return lineage.get_status(connection_id)


@router.get("/graph")
def get_all_databases_graph(connection_id: Optional[str] = None):
    return lineage.build_all_databases_graph(connection_id)


@router.get("/graph/{database}")
def get_database_graph(database: str, connection_id: Optional[str] = None):
    return lineage.build_database_graph(connection_id, database)


@router.get("/graph/{database}/{schema}")
def get_schema_graph(
    database: str, schema: str, connection_id: Optional[str] = None,
):
    return lineage.build_schema_graph(connection_id, database, schema)


@router.get("/table/{database}/{schema}/{table}")
def get_table_lineage(
    database: str, schema: str, table: str,
    hops: int = 3, connection_id: Optional[str] = None,
):
    return lineage.build_table_lineage(connection_id, database, schema, table, hops)


@router.post("/refresh/{database}")
def refresh_database(database: str, connection_id: Optional[str] = None):
    resolved = lineage._resolve_connection_id(connection_id)
    if not resolved:
        raise HTTPException(status_code=400, detail="No connection available")
    try:
        return lineage.refresh_database(resolved, database).to_dict()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/index-catalog")
def index_all_databases(connection_id: Optional[str] = None):
    """Enumerate every database/schema/table the connection's role can see
    into LINEAGE_CATALOG. Cheap (INFORMATION_SCHEMA only, no GET_LINEAGE), so
    the user always sees the full nested tree even before granting lineage
    privileges. Per-DB Refresh then adds the arrows between tables."""
    resolved = lineage._resolve_connection_id(connection_id)
    if not resolved:
        raise HTTPException(status_code=400, detail="No connection available")
    try:
        return lineage.index_all_databases(resolved)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/workflow-highlight/{workflow_id}")
def get_workflow_highlight(workflow_id: str, connection_id: Optional[str] = None):
    return lineage.compute_workflow_highlight_set(connection_id, workflow_id)
