"""
Data Explorer / profiling endpoints.

  GET  /profiling/columns/{database}/{schema}/{table}   → column list (name/type/nullable/pk)
  POST /profiling/profile/{database}/{schema}/{table}   → per-column statistics

Discovery of databases/schemas/tables is served by the existing
/assets/discover/* endpoints — the Data Explorer reuses those.
"""
import logging
from fastapi import APIRouter, HTTPException

from app.services.profiling_service import get_columns_with_pk, get_table_info, profile_table

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/table-info/{database}/{schema}/{table}")
def table_info(database: str, schema: str, table: str):
    """Table-level metadata (rows, size, kind, owner, comment) — no data."""
    try:
        return get_table_info(database, schema, table)
    except Exception as e:
        logger.error(f"[Profiling] table_info failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/columns/{database}/{schema}/{table}")
def list_columns(database: str, schema: str, table: str):
    """Column metadata for a table: name, data type, nullable, primary key."""
    try:
        return {"columns": get_columns_with_pk(database, schema, table)}
    except Exception as e:
        logger.error(f"[Profiling] list_columns failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/profile/{database}/{schema}/{table}")
def profile(database: str, schema: str, table: str):
    """
    Compute per-column statistics (null %, distinct, min/max, top values).
    Sample-first for large tables. Returns {table, columns}.
    """
    try:
        return profile_table(database, schema, table)
    except Exception as e:
        logger.error(f"[Profiling] profile failed for {database}.{schema}.{table}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
