import logging
from datetime import datetime
from typing import Set, Tuple
from sqlalchemy.orm import Session

from app.models.agent_run import AgentRun, AgentTask
from app.models.finding import Finding, FindingStatus
from app.models.asset import Asset
from app.services.scan_service import ScanService
from app.services.rule_engine import RuleEngine

logger = logging.getLogger(__name__)

# Statuses that are already considered closed — skip re-checking these
CLOSED_STATUSES = {
    FindingStatus.RESOLVED,
    FindingStatus.FALSE_POSITIVE,
    FindingStatus.WONT_FIX,
    FindingStatus.CLOSED,
}


class VerificationAgent:
    """
    Re-scans the table against Snowflake to check which findings have been
    resolved — whether fixed via the dashboard OR directly in Snowflake.

    Flow:
      1. Re-fetch fresh metadata from Snowflake (updates Asset rows in DB)
      2. Re-run all rules → get set of (rule_code, asset_fqn) that still fire
      3. For each DETECTED finding:
           - If its rule no longer fires → auto-mark resolved
           - If it still fires → leave as detected
      4. Return stats
    """

    def __init__(self, db: Session):
        self.db = db

    def run(self, run: AgentRun, task: AgentTask) -> dict:
        if not run.scan_id:
            raise ValueError("No scan_id on run — cannot verify")

        task.output = {"progress": "Refreshing table metadata from Snowflake..."}
        self.db.commit()

        # ── Step 1: Re-fetch fresh metadata ───────────────────────────────────
        try:
            service = ScanService(self.db)
            _, table_asset, column_assets = service.scan_metadata_only(
                run.database, run.schema_name, run.table
            )
            logger.info(
                f"[VerificationAgent] Refreshed metadata for {table_asset.fqn} "
                f"({len(column_assets)} columns)"
            )
        except Exception as e:
            raise ValueError(f"Failed to refresh metadata from Snowflake: {e}")

        task.output = {"progress": "Re-running quality rules against fresh schema..."}
        self.db.commit()

        # ── Step 2: Re-run all rules — collect still-firing violations ────────
        rule_engine = RuleEngine(self.db)
        still_firing: Set[Tuple[str, str]] = set()  # (rule_code, asset_fqn)

        try:
            # Use a sentinel scan_id so we don't create new Finding rows
            findings_data = rule_engine.execute_all_rules(
                table_asset, column_assets, scan_id="__verification__"
            )
            for fd in findings_data:
                ctx = fd.get("context") or {}
                rule_code = ctx.get("rule_code", "")
                asset_fqn = ctx.get("fqn", "")
                if rule_code and asset_fqn:
                    still_firing.add((rule_code, asset_fqn))

            logger.info(
                f"[VerificationAgent] {len(still_firing)} violations still present "
                f"out of original findings"
            )
        except Exception as e:
            logger.error(f"[VerificationAgent] Rule re-run failed: {e}")
            raise

        task.output = {"progress": "Checking which findings are now resolved..."}
        self.db.commit()

        # ── Step 3: Compare against open findings ─────────────────────────────
        all_findings = (
            self.db.query(Finding)
            .filter(Finding.scan_id == run.scan_id)
            .all()
        )

        newly_resolved = 0
        already_resolved = 0
        still_open = 0

        for finding in all_findings:
            if finding.status in CLOSED_STATUSES:
                already_resolved += 1
                continue

            ctx = finding.context or {}
            rule_code = ctx.get("rule_code", "")
            # Get the asset's FQN
            asset = self.db.query(Asset).filter(Asset.id == finding.asset_id).first()
            asset_fqn = asset.fqn if asset else ctx.get("fqn", "")

            if (rule_code, asset_fqn) not in still_firing:
                # Rule no longer fires → issue is resolved
                finding.status = FindingStatus.RESOLVED
                finding.resolved_at = datetime.utcnow()
                finding.resolution_notes = (
                    "Auto-resolved by verification scan — rule no longer fires on current schema."
                )
                finding.updated_at = datetime.utcnow()
                newly_resolved += 1
                logger.info(
                    f"[VerificationAgent] Auto-resolved: {finding.title} "
                    f"({rule_code} on {asset_fqn})"
                )
            else:
                still_open += 1

        self.db.commit()

        total = len(all_findings)
        total_resolved = already_resolved + newly_resolved
        pct = round((total_resolved / total * 100) if total > 0 else 0)

        result = {
            "total_findings": total,
            "resolved": total_resolved,
            "newly_auto_resolved": newly_resolved,
            "already_resolved": already_resolved,
            "remaining": still_open,
            "resolution_pct": pct,
            "fully_resolved": still_open == 0,
        }

        logger.info(
            f"[VerificationAgent] Done — {total_resolved}/{total} resolved "
            f"({newly_resolved} new, {already_resolved} prior), {still_open} remaining"
        )
        return result
