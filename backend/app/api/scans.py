from fastapi import APIRouter, HTTPException
from typing import Optional
from app.schemas.scan import ScanResponse, ScanListResponse
from app.services.scan_service import ScanService
from pydantic import BaseModel

router = APIRouter()


class ScanTableRequest(BaseModel):
    database: str
    schema: str
    table: str


@router.post("/table", response_model=ScanResponse, status_code=202)
def scan_table(request: ScanTableRequest):
    """
    Trigger a scan for a specific table.
    """
    scan_service = ScanService()
    try:
        scan = scan_service.scan_table(request.database, request.schema, request.table)
        return scan
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Scan failed: {str(e)}")


@router.get("", response_model=ScanListResponse)
def list_scans(asset_id: Optional[str] = None, skip: int = 0, limit: int = 50):
    """List all scans, optionally filtered by asset"""
    scan_service = ScanService()
    scans = scan_service.list_scans(asset_id=asset_id, limit=limit)

    return ScanListResponse(total=len(scans), scans=scans)


@router.get("/{scan_id}", response_model=ScanResponse)
def get_scan(scan_id: str):
    """Get a specific scan by ID"""
    scan_service = ScanService()
    scan = scan_service.get_scan(scan_id)

    if not scan:
        raise HTTPException(status_code=404, detail="Scan not found")

    return scan
