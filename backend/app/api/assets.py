from fastapi import APIRouter, HTTPException
from typing import Optional
from app.services import storage
from app.schemas.asset import AssetResponse, AssetListResponse
from app.services.snowflake_session import session as sf_session

router = APIRouter()


@router.get("", response_model=AssetListResponse)
def list_assets(
    asset_type: Optional[str] = None,
    database_name: Optional[str] = None,
    schema_name: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
):
    """List all assets with optional filters"""
    total, assets = storage.list_assets(
        asset_type=asset_type,
        database_name=database_name,
        schema_name=schema_name,
        skip=skip,
        limit=limit,
    )
    return AssetListResponse(total=total, assets=assets)


@router.get("/{asset_id}", response_model=AssetResponse)
def get_asset(asset_id: str):
    """Get a specific asset by ID"""
    asset = storage.get_asset(asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    return asset


@router.get("/discover/databases")
def discover_databases():
    """Serves from startup cache — instant, no SSO."""
    ctx = sf_session.get_cached_context()
    if ctx:
        dbs = ctx["databases"]
        return {"databases": dbs, "count": len(dbs)}
    try:
        rows = sf_session.query("SHOW DATABASES")
        dbs = [r.get("name") or r.get("NAME") for r in rows if r.get("name") or r.get("NAME")]
        return {"databases": dbs, "count": len(dbs)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/discover/schemas/{database}")
def discover_schemas(database: str):
    try:
        rows = sf_session.query(f"SHOW SCHEMAS IN DATABASE {database}")
        schemas = [r.get("name") or r.get("NAME") for r in rows if r.get("name") or r.get("NAME")]
        return {"schemas": schemas, "count": len(schemas)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/discover/tables/{database}/{schema}")
def discover_tables(database: str, schema: str):
    try:
        rows = sf_session.query(f"SHOW TABLES IN {database}.{schema}")
        tables = [r.get("name") or r.get("NAME") for r in rows if r.get("name") or r.get("NAME")]
        return {"tables": tables, "count": len(tables)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
