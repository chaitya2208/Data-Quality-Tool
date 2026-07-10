from sqlalchemy.orm import Session
from app.models.asset import Asset
from app.models.scan import Scan, ScanStatus, ScanType
from app.models.finding import Finding
from app.services.snowflake_session import session as sf_session
from app.services.rule_engine import RuleEngine
from datetime import datetime
from typing import Optional, List, Tuple
import logging

logger = logging.getLogger(__name__)


class ScanService:
    """
    Service for executing scans against Snowflake assets.
    Phase 0: Metadata scans only.
    """

    def __init__(self, db: Session):
        self.db = db
        self.rule_engine = RuleEngine(db)

    def create_or_update_asset(
        self,
        fqn: str,
        asset_type: str,
        database_name: str,
        schema_name: Optional[str] = None,
        table_name: Optional[str] = None,
        column_name: Optional[str] = None,
        metadata: dict = None
    ) -> Asset:
        """Create or update an asset in the database"""

        # Try to find existing asset
        asset = self.db.query(Asset).filter(Asset.fqn == fqn).first()

        if asset:
            # Update existing asset
            asset.owner = metadata.get("owner") if metadata else None
            asset.comment = metadata.get("comment") if metadata else None
            asset.row_count = metadata.get("row_count") if metadata else None
            asset.size_bytes = metadata.get("size_bytes") if metadata else None
            asset.raw_metadata = metadata
            asset.updated_at = datetime.utcnow()
        else:
            # Create new asset
            asset = Asset(
                fqn=fqn,
                asset_type=asset_type,
                database_name=database_name,
                schema_name=schema_name,
                table_name=table_name,
                column_name=column_name,
                owner=metadata.get("owner") if metadata else None,
                comment=metadata.get("comment") if metadata else None,
                row_count=metadata.get("row_count") if metadata else None,
                size_bytes=metadata.get("size_bytes") if metadata else None,
                raw_metadata=metadata,
            )
            self.db.add(asset)

        self.db.commit()
        self.db.refresh(asset)
        return asset

    def scan_metadata_only(
        self, database: str, schema: str, table: str, source=None, connection_id: str = None
    ) -> Tuple[Scan, Asset, List[Asset]]:
        """
        Fetch metadata via the resolved DataSource and create/update Asset rows
        WITHOUT running rules. Used by the agent pipeline so MetadataAgent and
        RulesAgent stay separate. Returns (scan, table_asset, column_assets) with
        scan in RUNNING status.
        """
        logger.info(f"[MetadataAgent] Fetching metadata for {database}.{schema}.{table}")

        if source is None:
            from app.services.datasources import get_source
            source = get_source(connection_id, db=self.db)

        info = source.table_info(database, schema, table)
        columns = source.list_columns(database, schema, table)
        if not columns:
            raise ValueError(f"Table {database}.{schema}.{table} not found or has no columns")

        table_fqn = f"{database}.{schema}.{table}"
        table_asset = self.create_or_update_asset(
            fqn=table_fqn,
            asset_type="table",
            database_name=database,
            schema_name=schema,
            table_name=table,
            metadata={
                "owner":           info.get("owner"),
                "comment":         info.get("comment"),
                "row_count":       info.get("row_count"),
                "size_bytes":      info.get("bytes"),
                "created_at":      "",
                "last_altered_at": "",
            }
        )

        # Create scan in RUNNING state (RulesAgent will complete it)
        scan = Scan(
            asset_id=table_asset.id,
            connection_id=connection_id,
            scan_type=ScanType.METADATA,
            status=ScanStatus.RUNNING,
            started_at=datetime.utcnow(),
        )
        self.db.add(scan)
        self.db.commit()
        self.db.refresh(scan)

        column_assets = []
        for col in columns:
            cname = col["column_name"]
            column_fqn = f"{table_fqn}.{cname}"
            col_asset = self.create_or_update_asset(
                fqn=column_fqn,
                asset_type="column",
                database_name=database,
                schema_name=schema,
                table_name=table,
                column_name=cname,
                metadata={
                    "comment":     col.get("comment"),
                    "data_type":   col.get("data_type"),
                    "is_nullable": "YES" if col.get("is_nullable") else "NO",
                }
            )
            column_assets.append(col_asset)

        table_asset.last_scanned_at = datetime.utcnow()
        self.db.commit()

        logger.info(
            f"[MetadataAgent] Done: {table_fqn} — {len(column_assets)} columns"
        )
        return scan, table_asset, column_assets

    def scan_table(self, database: str, schema: str, table: str) -> Scan:
        """
        Scan a specific table: fetch metadata, create/update asset, run rules, create findings.
        """
        logger.info(f"Starting scan for table: {database}.{schema}.{table}")

        # Create scan record
        scan = Scan(
            asset_id="temp",  # Will update after creating asset
            scan_type=ScanType.METADATA,
            status=ScanStatus.RUNNING,
            started_at=datetime.utcnow(),
        )

        try:
            # SHOW TABLES has confirmed keys: name, rows, bytes, owner,
            # comment, created_on. Use it as the primary source.
            show_rows = sf_session.query(
                f"SHOW TABLES LIKE '{table}' IN {database}.{schema}"
            )
            table_metadata = next(
                (r for r in show_rows
                 if (r.get("name") or "").upper() == table.upper()),
                {}
            )

            # INFORMATION_SCHEMA for LAST_ALTERED timestamp
            info_rows = sf_session.query(f"""
                SELECT last_altered as LAST_ALTERED
                FROM {database}.INFORMATION_SCHEMA.TABLES
                WHERE table_schema = '{schema}'
                AND   table_name   = '{table}'
            """)
            info_row = info_rows[0] if info_rows else {}

            columns = sf_session.query(f"""
                SELECT column_name as COLUMN_NAME,
                       ordinal_position as ORDINAL_POSITION,
                       data_type as DATA_TYPE,
                       is_nullable as IS_NULLABLE,
                       column_default as COLUMN_DEFAULT,
                       comment as COMMENT
                FROM {database}.INFORMATION_SCHEMA.COLUMNS
                WHERE table_schema = '{schema}'
                AND table_name = '{table}'
                ORDER BY ordinal_position
            """)

            if not table_metadata:
                raise ValueError(f"Table {database}.{schema}.{table} not found")

            # Keys confirmed from SHOW TABLES: rows, bytes, owner, comment
            # Keys confirmed from INFORMATION_SCHEMA: TABLE_OWNER, COMMENT, CREATED, LAST_ALTERED
            table_fqn = f"{database}.{schema}.{table}"
            table_asset = self.create_or_update_asset(
                fqn=table_fqn,
                asset_type="table",
                database_name=database,
                schema_name=schema,
                table_name=table,
                metadata={
                    "owner":           table_metadata.get("owner"),
                    "comment":         table_metadata.get("comment"),
                    "row_count":       table_metadata.get("rows"),
                    "size_bytes":      table_metadata.get("bytes"),
                    "created_at":      str(table_metadata.get("created_on", "")),
                    "last_altered_at": str(info_row.get("LAST_ALTERED", "")),
                }
            )

            # Update scan with correct asset_id
            scan.asset_id = table_asset.id
            self.db.add(scan)
            self.db.commit()
            self.db.refresh(scan)

            # Create or update column assets
            for col in columns:
                column_fqn = f"{table_fqn}.{col['COLUMN_NAME']}"
                self.create_or_update_asset(
                    fqn=column_fqn,
                    asset_type="column",
                    database_name=database,
                    schema_name=schema,
                    table_name=table,
                    column_name=col["COLUMN_NAME"],
                    metadata={
                        "comment": col.get("COMMENT"),
                        "data_type": col.get("DATA_TYPE"),
                        "is_nullable": col.get("IS_NULLABLE"),
                        "ordinal_position": col.get("ORDINAL_POSITION"),
                    }
                )

            # Update asset last_scanned_at
            table_asset.last_scanned_at = datetime.utcnow()
            self.db.commit()

            # Fetch column assets
            column_assets = self.db.query(Asset).filter(
                Asset.table_name == table,
                Asset.schema_name == schema,
                Asset.database_name == database,
                Asset.asset_type == "column"
            ).all()

            # Run static + dynamic rules together
            findings_data = self.rule_engine.execute_all_rules(
                table_asset, column_assets, scan.id
            )

            # Persist findings
            for finding_data in findings_data:
                finding = Finding(**finding_data)
                self.db.add(finding)

            # Update scan status
            scan.status = ScanStatus.COMPLETED
            scan.completed_at = datetime.utcnow()
            # Count all active rules (static + dynamic) applied during this scan
            active_table_rules  = len(self.rule_engine.get_active_rules("table"))
            active_column_rules = len(self.rule_engine.get_active_rules("column"))
            scan.rules_checked  = (
                active_table_rules
                + active_column_rules * len(column_assets)
            )
            scan.findings_count = len(findings_data)

            self.db.commit()
            self.db.refresh(scan)

            logger.info(f"Scan completed successfully. Found {len(findings_data)} issues.")
            return scan

        except Exception as e:
            logger.error(f"Scan failed: {str(e)}")
            scan.status = ScanStatus.FAILED
            scan.completed_at = datetime.utcnow()
            scan.error_message = str(e)
            self.db.commit()
            raise

    def get_scan(self, scan_id: str) -> Optional[Scan]:
        """Get a scan by ID"""
        return self.db.query(Scan).filter(Scan.id == scan_id).first()

    def list_scans(self, asset_id: Optional[str] = None, limit: int = 50) -> List[Scan]:
        """List scans, optionally filtered by asset"""
        query = self.db.query(Scan)
        if asset_id:
            query = query.filter(Scan.asset_id == asset_id)
        return query.order_by(Scan.created_at.desc()).limit(limit).all()
