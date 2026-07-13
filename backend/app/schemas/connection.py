from pydantic import BaseModel
from typing import Optional, Any, Dict, List
from datetime import datetime


class ConnectionCreate(BaseModel):
    name: str
    type: str  # "snowflake" | "postgres"
    host: Optional[str] = None
    port: Optional[int] = None
    database: Optional[str] = None
    schema_name: Optional[str] = None       # maps to Connection.schema_
    username: Optional[str] = None
    secret: Optional[str] = None            # password / token (write-only)
    auth_method: Optional[str] = None
    extra: Optional[Dict[str, Any]] = None
    is_active: bool = True


class ConnectionUpdate(BaseModel):
    name: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    database: Optional[str] = None
    schema_name: Optional[str] = None
    username: Optional[str] = None
    secret: Optional[str] = None
    auth_method: Optional[str] = None
    extra: Optional[Dict[str, Any]] = None
    is_active: Optional[bool] = None


class ConnectionResponse(BaseModel):
    """Secret is never returned — only whether one is set."""
    id: str
    name: str
    type: str
    host: Optional[str] = None
    port: Optional[int] = None
    database: Optional[str] = None
    schema_name: Optional[str] = None
    username: Optional[str] = None
    has_secret: bool = False
    auth_method: Optional[str] = None
    extra: Optional[Dict[str, Any]] = None
    is_active: bool = True
    created_at: datetime


class ConnectionListResponse(BaseModel):
    total: int
    connections: List[ConnectionResponse]


class ConnectionTestResult(BaseModel):
    ok: bool
    user: Optional[str] = None
    detail: Optional[str] = None
