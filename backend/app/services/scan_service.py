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
        self, database: str, schema: str, table: str, source=None, connection_id: str = None
    ) -> Tuple[Any, Any, List[Any]]:
        """
        Fetch metadata via the resolved DataSource and create/update Asset rows
        WITHOUT running rules. Used by the agent pipeline so MetadataAgent and
        RulesAgent stay separate. Returns (scan, table_asset, column_assets) with
        scan in RUNNING status.
        """
        logger.info(f"[MetadataAgent] Fetching metadata for {database}.{schema}.{table}")

        if source is None:
            from app.services.datasources import get_source
            source = get_source(connection_id)

        info = source.table_info(database, schema, table)
        columns = source.list_columns(database, schema, table)
        if not columns:
            raise ValueError(f"Table {database}.{schema}.{table} not found or has no columns")

        # Schema drift Tier 1: snapshot the prior column asset state BEFORE
        # the upsert overwrites it. The diff runs after the upsert.
        from app.services import schema_drift
        prior_column_snapshot = schema_drift.snapshot_columns(database, schema, table)

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
        scan = storage.create_scan(
            asset_id=table_asset.id,
            connection_id=connection_id,
            scan_type="metadata",
            status="running",
            started_at=datetime.utcnow(),
        )

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

        storage.update_asset_last_scanned(table_asset.id)

        # Compute drift findings vs prior snapshot and stash on the scan for
        # FindingsAgent to fold into its lifecycle pass. Non-fatal if it fails.
        try:
            drift_findings = schema_drift.detect_column_drift(
                scan_id=scan.id,
                table_asset=table_asset,
                prior_snapshot=prior_column_snapshot,
                live_columns=columns,
            )
        except Exception as e:
            logger.warning(f"[MetadataAgent] drift detection failed for {table_fqn}: {e}")
            drift_findings = []
        # Attach as a plain attribute for the in-memory template path AND
        # persist to SCAN_RESULTS so the agentic path (which re-fetches the
        # scan row after the rule-review pause) can recover them. Without the
        # persisted copy the setattr is lost on the next storage.get_scan().
        setattr(scan, "drift_findings", drift_findings)
        if drift_findings:
            try:
                existing_results = scan.scan_results or {}
                existing_results["drift_findings"] = drift_findings
                storage.update_scan(scan.id, scan_results=existing_results)
            except Exception as e:
                logger.warning(f"[MetadataAgent] Could not persist drift findings to scan_results: {e}")

        logger.info(
            f"[MetadataAgent] Done: {table_fqn} — {len(column_assets)} columns"
            + (f", {len(drift_findings)} drift finding(s)" if drift_findings else "")
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

            # Schema drift snapshot (BEFORE upsert overwrites ASSETS).
            from app.services import schema_drift
            prior_column_snapshot = schema_drift.snapshot_columns(database, schema, table)

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

            # Fold in schema drift findings — same shape as rule engine output.
            try:
                live_cols = [
                    {"column_name": c["COLUMN_NAME"],
                     "data_type":   c.get("DATA_TYPE"),
                     "is_nullable": c.get("IS_NULLABLE")}
                    for c in columns
                ]
                drift_findings = schema_drift.detect_column_drift(
                    scan_id=scan.id, table_asset=table_asset,
                    prior_snapshot=prior_column_snapshot, live_columns=live_cols,
                )
            except Exception as e:
                logger.warning(f"drift detection failed for {table_fqn}: {e}")
                drift_findings = []
            findings_data = list(findings_data) + drift_findings

            # Persist findings through the incident-lifecycle finalizer so
            # this legacy path stays consistent with the agentic workflow —
            # no more raw create_finding twins. We only know the FAILED
            # instance ids here (legacy path doesn't track "which rules ran"
            # end-to-end), so pass those as the executed set — auto-resolve
            # is limited to instances that failed-then-passed, which never
            # happens in a single call: safe.
            from app.services.scan_finalizer import finalize_scan
            failed_iids = {fd.get("instance_id") for fd in findings_data if fd.get("instance_id")}
            # Include drift instance ids for both failing AND passing drift
            # checks in this scan, so a resolved drift incident (e.g. a
            # re-added column) actually auto-closes.
            drift_failed_handlers = {
                (f.get("context") or {}).get("rule_code", "").lower()
                for f in drift_findings
            }
            for hk in schema_drift.DRIFT_HANDLER_KEYS:
                if hk in drift_failed_handlers:
                    continue
                inst = schema_drift._get_per_table_drift_instance(
                    hk, database, schema, table,
                )
                if inst and storage.find_open_finding(inst.id, table_asset.id):
                    failed_iids.add(inst.id)
            stats = finalize_scan(
                scan_id=scan.id,
                asset_id_for_passed=table_asset.id,
                findings_data=findings_data,
                executed_instance_ids=failed_iids,
            )
            active_findings_count = stats["created"] + stats["reopened"] + stats["updated"]

            active_table_rules  = len(self.rule_engine.get_active_rules("table"))
            active_column_rules = len(self.rule_engine.get_active_rules("column"))
            scan = storage.update_scan(
                scan.id,
                status="completed",
                completed_at=datetime.utcnow(),
                rules_checked=active_table_rules + active_column_rules * len(column_assets),
                findings_count=active_findings_count,
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
