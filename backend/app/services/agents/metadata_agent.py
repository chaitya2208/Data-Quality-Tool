import logging
from typing import Tuple, List
from sqlalchemy.orm import Session
from app.models.asset import Asset
from app.models.scan import Scan
from app.services.scan_service import ScanService

logger = logging.getLogger(__name__)


class MetadataAgent:
    """
    Fetches Snowflake metadata and creates/updates Asset rows.
    Does NOT run rules — that is RulesAgent's job.
    """

    def __init__(self, db: Session):
        self.db = db
        self.service = ScanService(db)

    def run(self, database: str, schema: str, table: str, connection_id: str = None) -> Tuple[Scan, Asset, List[Asset]]:
        logger.info(f"[MetadataAgent] Starting for {database}.{schema}.{table}")
        scan, table_asset, column_assets = self.service.scan_metadata_only(
            database, schema, table, connection_id=connection_id
        )
        logger.info(
            f"[MetadataAgent] Done — asset {table_asset.fqn}, {len(column_assets)} columns"
        )
        return scan, table_asset, column_assets
