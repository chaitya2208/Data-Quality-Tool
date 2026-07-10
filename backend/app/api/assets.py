from fastapi import APIRouter, HTTPException
from typing import Optional
from app.services import storage
from app.schemas.asset import AssetResponse, AssetListResponse
from app.services.datasources import get_source

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
def discover_databases(connection_id: Optional[str] = None):
    try:
        source = get_source(connection_id)
        dbs = source.list_databases()
        return {"databases": dbs, "count": len(dbs)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/discover/schemas/{database}")
def discover_schemas(database: str, connection_id: Optional[str] = None):
    try:
        source = get_source(connection_id)
        schemas = source.list_schemas(database)
        return {"schemas": schemas, "count": len(schemas)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/discover/tables/{database}/{schema}")
def discover_tables(database: str, schema: str, connection_id: Optional[str] = None):
    try:
        source = get_source(connection_id)
        tables = [t["name"] for t in source.list_tables(database, schema)]
        return {"tables": tables, "count": len(tables)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
