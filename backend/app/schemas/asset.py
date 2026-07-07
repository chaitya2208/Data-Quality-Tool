from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List
from datetime import datetime


class AssetBase(BaseModel):
    asset_type: str = Field(..., description="Type of asset: database, schema, table, column")
    database_name: str
    schema_name: Optional[str] = None
    table_name: Optional[str] = None
    column_name: Optional[str] = None
    fqn: str = Field(..., description="Fully qualified name")
    owner: Optional[str] = None
    comment: Optional[str] = None
    row_count: Optional[int] = None
    size_bytes: Optional[int] = None
    raw_metadata: Optional[Dict[str, Any]] = None


class AssetCreate(AssetBase):
    pass


class AssetUpdate(BaseModel):
    owner: Optional[str] = None
    comment: Optional[str] = None
    row_count: Optional[int] = None
    size_bytes: Optional[int] = None
    raw_metadata: Optional[Dict[str, Any]] = None
    last_scanned_at: Optional[datetime] = None


class AssetResponse(AssetBase):
    id: str
    created_at: datetime
    updated_at: datetime
    last_scanned_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class AssetListResponse(BaseModel):
    total: int
    assets: List[AssetResponse]
