"""
Data Explorer / profiling endpoints.

  GET  /profiling/columns/{database}/{schema}/{table}   → column list (name/type/nullable/pk)
  POST /profiling/profile/{database}/{schema}/{table}   → per-column statistics

All endpoints take an optional `connection_id` (query param) selecting the data
source; when omitted the registry falls back to the default connection.
"""
import logging
from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.services.datasources import get_source
from app.services.profiling_service import get_columns_with_pk, get_table_info, profile_table

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/table-info/{database}/{schema}/{table}")
def table_info(database: str, schema: str, table: str, connection_id: str | None = None,
               db: Session = Depends(get_db)):
    """Table-level metadata (rows, size, kind, owner, comment) — no data."""
    try:
        source = get_source(connection_id, db=db)
        return get_table_info(source, database, schema, table)
    except Exception as e:
        logger.error(f"[Profiling] table_info failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/columns/{database}/{schema}/{table}")
def list_columns(database: str, schema: str, table: str, connection_id: str | None = None,
                 db: Session = Depends(get_db)):
    """Column metadata for a table: name, data type, nullable, primary key."""
    try:
        source = get_source(connection_id, db=db)
        return {"columns": get_columns_with_pk(source, database, schema, table)}
    except Exception as e:
        logger.error(f"[Profiling] list_columns failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/profile/{database}/{schema}/{table}")
def profile(database: str, schema: str, table: str, connection_id: str | None = None,
            db: Session = Depends(get_db)):
    """Compute per-column statistics (null %, distinct, min/max, avg/stddev, top values)."""
    try:
        source = get_source(connection_id, db=db)
        return profile_table(source, database, schema, table)
    except Exception as e:
        logger.error(f"[Profiling] profile failed for {database}.{schema}.{table}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
