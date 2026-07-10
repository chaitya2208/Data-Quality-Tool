import logging
from datetime import datetime
from typing import Set, Tuple, Any

from app.services import storage
from app.services.scan_service import ScanService
from app.services.rule_engine import RuleEngine

logger = logging.getLogger(__name__)

# Statuses that are already considered closed — skip re-checking these
CLOSED_STATUSES = {"resolved", "false_positive", "wont_fix", "closed"}


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

    def __init__(self):
        pass

    def run(self, run: Any, task: Any) -> dict:
        if not run.scan_id:
            raise ValueError("No scan_id on run — cannot verify")

        storage.update_agent_task(task.id, output={"progress": "Refreshing table metadata from Snowflake..."})

        # ── Step 1: Re-fetch fresh metadata ───────────────────────────────────
        try:
            service = ScanService()
            _, table_asset, column_assets = service.scan_metadata_only(
                run.database, run.schema_name, run.table
            )
            logger.info(
                f"[VerificationAgent] Refreshed metadata for {table_asset.fqn} "
                f"({len(column_assets)} columns)"
            )
        except Exception as e:
            raise ValueError(f"Failed to refresh metadata from Snowflake: {e}")

        storage.update_agent_task(task.id, output={"progress": "Re-running quality rules against fresh schema..."})

        # ── Step 2: Re-run all rules — collect still-firing violations ────────
        rule_engine = RuleEngine()
        still_firing: Set[Tuple[str, str]] = set()  # (rule_code, asset_fqn)

        # Active sql_template instances for this table — without this,
        # Claude-authored checks would silently stop being re-verified after
        # their first findings pass (the exact one-time-opinion bug this was
        # meant to fix).
        _, active_instances = storage.list_instances(
            database_name=table_asset.database_name,
            schema_name=table_asset.schema_name,
            table_name=table_asset.table_name,
            status="active",
            limit=1000,
        )
        active_instance_ids = {i.id for i in active_instances}

        try:
            # Use a sentinel scan_id so we don't create new Finding rows
            findings_data = rule_engine.execute_all_rules(
                table_asset, column_assets, scan_id="__verification__",
                allowed_instance_ids=active_instance_ids,
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

        storage.update_agent_task(task.id, output={"progress": "Checking which findings are now resolved..."})

        # ── Step 3: Compare against open findings ─────────────────────────────
        all_findings = storage.list_findings_by_scan(run.scan_id)

        newly_resolved = 0
        already_resolved = 0
        still_open = 0
        logged_instance_ids: Set[str] = set()

        for finding in all_findings:
            if finding.status in CLOSED_STATUSES:
                already_resolved += 1
                continue

            ctx = finding.context or {}
            rule_code = ctx.get("rule_code", "")
            # Get the asset's FQN
            asset = storage.get_asset(finding.asset_id)
            asset_fqn = asset.fqn if asset else ctx.get("fqn", "")

            still_firing_now = (rule_code, asset_fqn) in still_firing

            # Log one RULE_EXECUTIONS row per instance re-checked this pass
            # (skip duplicates if multiple findings share the same instance).
            if finding.instance_id and finding.instance_id not in logged_instance_ids:
                storage.create_execution(
                    instance_id=finding.instance_id,
                    status="failed" if still_firing_now else "passed",
                    run_id=run.id,
                    evidence={"source": "verification_agent"},
                )
                logged_instance_ids.add(finding.instance_id)

            if not still_firing_now:
                # Rule no longer fires → issue is resolved
                storage.update_finding(
                    finding.id,
                    status="resolved",
                    resolved_at=datetime.utcnow(),
                    resolution_notes="Auto-resolved by verification scan — rule no longer fires on current schema.",
                )
                newly_resolved += 1
                logger.info(
                    f"[VerificationAgent] Auto-resolved: {finding.title} "
                    f"({rule_code} on {asset_fqn})"
                )
            else:
                still_open += 1

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
