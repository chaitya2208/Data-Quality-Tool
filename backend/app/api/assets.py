from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import Optional
from app.core.database import get_db
from app.models.asset import Asset
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
    db: Session = Depends(get_db)
):
    """List all assets with optional filters"""
    query = db.query(Asset)

    if asset_type:
        query = query.filter(Asset.asset_type == asset_type)
    if database_name:
        query = query.filter(Asset.database_name == database_name)
    if schema_name:
        query = query.filter(Asset.schema_name == schema_name)

    total = query.count()
    assets = query.offset(skip).limit(limit).all()

    return AssetListResponse(total=total, assets=assets)


@router.get("/{asset_id}", response_model=AssetResponse)
def get_asset(asset_id: str, db: Session = Depends(get_db)):
    """Get a specific asset by ID"""
    asset = db.query(Asset).filter(Asset.id == asset_id).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    return asset


@router.get("/discover/databases")
def discover_databases(connection_id: Optional[str] = None, db: Session = Depends(get_db)):
    try:
        source = get_source(connection_id, db=db)
        dbs = source.list_databases()
        return {"databases": dbs, "count": len(dbs)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/discover/schemas/{database}")
def discover_schemas(database: str, connection_id: Optional[str] = None, db: Session = Depends(get_db)):
    try:
        source = get_source(connection_id, db=db)
        schemas = source.list_schemas(database)
        return {"schemas": schemas, "count": len(schemas)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/discover/tables/{database}/{schema}")
def discover_tables(database: str, schema: str, connection_id: Optional[str] = None, db: Session = Depends(get_db)):
    try:
        source = get_source(connection_id, db=db)
        tables = [t["name"] for t in source.list_tables(database, schema)]
        return {"tables": tables, "count": len(tables)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
