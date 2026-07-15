import logging
from datetime import datetime
from typing import Set, Tuple, Any

from app.services import storage
from app.services.scan_service import ScanService
from app.services.rule_engine import RuleEngine

logger = logging.getLogger(__name__)

# Statuses that are already considered closed — skip re-checking these
CLOSED_STATUSES = {"resolved", "false_positive", "wont_fix", "closed", "superseded"}


class VerificationAgent:
    """
    Re-scans the table against its own data source to check which findings
    have been resolved — whether fixed via the dashboard OR directly in the
    source (Snowflake or Postgres/RDS).

    Flow:
      1. Re-fetch fresh metadata from the data source (updates Asset rows in DB)
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

        storage.update_agent_task(task.id, output={"progress": "Refreshing table metadata from the data source..."})

        # Resolve the run's OWN data source so verification re-checks against the
        # right database (Snowflake OR Postgres/RDS) — the every-N-min auto-verify
        # and the manual Verify both flow through here.
        source = None
        try:
            from app.services.datasources import get_source
            source = get_source(getattr(run, "connection_id", None))
        except Exception as e:
            logger.warning(f"[VerificationAgent] Could not resolve source for run {run.id}: {e}")

        # ── Step 1: Re-fetch fresh metadata ───────────────────────────────────
        try:
            service = ScanService()
            _, table_asset, column_assets = service.scan_metadata_only(
                run.database, run.schema_name, run.table,
                connection_id=getattr(run, "connection_id", None),
            )
            logger.info(
                f"[VerificationAgent] Refreshed metadata for {table_asset.fqn} "
                f"({len(column_assets)} columns)"
            )
        except Exception as e:
            raise ValueError(f"Failed to refresh metadata from the data source: {e}")

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

        # Only instances whose definition is also active were actually re-run.
        # Findings for disabled definitions must not be auto-resolved — the rule
        # was simply skipped, not passed.
        checked_instance_ids: Set[str] = set()
        for inst in active_instances:
            defn = storage.get_definition(inst.definition_id)
            if defn and defn.status == "active":
                checked_instance_ids.add(inst.id)

        try:
            # Use a sentinel scan_id so we don't create new Finding rows
            findings_data = rule_engine.execute_all_rules(
                table_asset, column_assets, scan_id="__verification__",
                allowed_instance_ids=active_instance_ids,
                source=source,
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

            # Only auto-resolve if the rule was actually re-run (definition active).
            # If the definition is disabled the rule was skipped, not passed.
            rule_was_checked = (
                finding.instance_id is None or finding.instance_id in checked_instance_ids
            )

            if not still_firing_now and rule_was_checked:
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
            elif not still_firing_now and not rule_was_checked:
                logger.info(
                    f"[VerificationAgent] Skipped (rule disabled): {finding.title} "
                    f"({rule_code} on {asset_fqn})"
                )
                still_open += 1
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
