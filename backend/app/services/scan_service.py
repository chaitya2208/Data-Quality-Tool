from app.services import storage
from app.services.snowflake_session import session as sf_session
from app.services.rule_engine import RuleEngine
from datetime import datetime
from typing import Optional, List, Tuple, Any
import logging

logger = logging.getLogger(__name__)


class ScanService:
    """
    Service for executing scans against Snowflake assets.
    Phase 0: Metadata scans only.
    """

    def __init__(self):
        self.rule_engine = RuleEngine()

    def create_or_update_asset(
        self,
        fqn: str,
        asset_type: str,
        database_name: str,
        schema_name: Optional[str] = None,
        table_name: Optional[str] = None,
        column_name: Optional[str] = None,
        metadata: dict = None
    ) -> Any:
        """Create or update an asset in storage"""
        return storage.create_or_update_asset(
            fqn=fqn,
            asset_type=asset_type,
            database_name=database_name,
            schema_name=schema_name,
            table_name=table_name,
            column_name=column_name,
            metadata=metadata,
        )

    def scan_metadata_only(
        self, database: str, schema: str, table: str
    ) -> Tuple[Any, Any, List[Any]]:
        """
        Fetch Snowflake metadata and create/update Asset rows WITHOUT running rules.
        Used by the agent pipeline so MetadataAgent and RulesAgent stay separate.
        Returns (scan, table_asset, column_assets) with scan in RUNNING status.
        """
        logger.info(f"[MetadataAgent] Fetching metadata for {database}.{schema}.{table}")

        show_rows = sf_session.query(
            f"SHOW TABLES LIKE '{table}' IN {database}.{schema}"
        )
        table_metadata = next(
            (r for r in show_rows if (r.get("name") or "").upper() == table.upper()),
            {}
        )
        if not table_metadata:
            raise ValueError(f"Table {database}.{schema}.{table} not found in Snowflake")

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

        # Create scan in RUNNING state (RulesAgent will complete it)
        scan = storage.create_scan(
            asset_id=table_asset.id,
            scan_type="metadata",
            status="running",
            started_at=datetime.utcnow(),
        )

        column_assets = []
        for col in columns:
            column_fqn = f"{table_fqn}.{col['COLUMN_NAME']}"
            col_asset = self.create_or_update_asset(
                fqn=column_fqn,
                asset_type="column",
                database_name=database,
                schema_name=schema,
                table_name=table,
                column_name=col["COLUMN_NAME"],
                metadata={
                    "comment":           col.get("COMMENT"),
                    "data_type":         col.get("DATA_TYPE"),
                    "is_nullable":       col.get("IS_NULLABLE"),
                    "ordinal_position":  col.get("ORDINAL_POSITION"),
                }
            )
            column_assets.append(col_asset)

        storage.update_asset_last_scanned(table_asset.id)

        logger.info(
            f"[MetadataAgent] Done: {table_fqn} — {len(column_assets)} columns"
        )
        return scan, table_asset, column_assets

    def scan_table(self, database: str, schema: str, table: str) -> Any:
        """
        Scan a specific table: fetch metadata, create/update asset, run rules, create findings.
        """
        logger.info(f"Starting scan for table: {database}.{schema}.{table}")

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

            scan = storage.create_scan(
                asset_id=table_asset.id,
                scan_type="metadata",
                status="running",
                started_at=datetime.utcnow(),
            )

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

            storage.update_asset_last_scanned(table_asset.id)

            # Fetch column assets
            column_assets = storage.list_column_assets(database, schema, table)

            # Run static + dynamic rules together
            findings_data = self.rule_engine.execute_all_rules(
                table_asset, column_assets, scan.id
            )

            # Persist findings
            for finding_data in findings_data:
                storage.create_finding(**finding_data)

            # Update scan status
            active_table_rules  = len(self.rule_engine.get_active_rules("table"))
            active_column_rules = len(self.rule_engine.get_active_rules("column"))
            scan = storage.update_scan(
                scan.id,
                status="completed",
                completed_at=datetime.utcnow(),
                rules_checked=active_table_rules + active_column_rules * len(column_assets),
                findings_count=len(findings_data),
            )

            logger.info(f"Scan completed successfully. Found {len(findings_data)} issues.")
            return scan

        except Exception as e:
            logger.error(f"Scan failed: {str(e)}")
            if 'scan' in dir() and scan:
                storage.update_scan(
                    scan.id,
                    status="failed",
                    completed_at=datetime.utcnow(),
                    error_message=str(e),
                )
            raise

    def get_scan(self, scan_id: str) -> Optional[Any]:
        """Get a scan by ID"""
        return storage.get_scan(scan_id)

    def list_scans(self, asset_id: Optional[str] = None, limit: int = 50) -> List[Any]:
        """List scans, optionally filtered by asset"""
        return storage.list_scans(asset_id=asset_id, limit=limit)
