import logging
from typing import Set, Tuple, Any, Dict

from app.services import storage
from app.services.scan_service import ScanService
from app.services.rule_engine import RuleEngine

logger = logging.getLogger(__name__)


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
        # Key on instance_id — a stable primary key — instead of (rule_code,
        # asset_fqn). Definition names are mutable display strings (a rename
        # via the Rules Library would previously break the compare and
        # auto-resolve every finding for that instance), and rule_engine +
        # verification resolve asset_fqn on independent paths whose fallbacks
        # can diverge (audit findings #2 and #10). Legacy findings whose
        # instance_id is null fall back to the old string key below.
        rule_engine = RuleEngine()
        still_firing_instance_ids: Set[str] = set()
        still_firing_legacy: Set[Tuple[str, str]] = set()  # (rule_code, asset_fqn) for pre-instance findings

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

        # Only instances whose definition was not disabled were actually re-run.
        # Findings for disabled definitions must not be auto-resolved — the rule
        # was simply skipped, not passed. Mirror rule_engine's guard exactly:
        # it skips "disabled" only, so "pending"/"rejected" definitions DO run.
        defs_by_id = storage.get_definitions_by_ids(
            [inst.definition_id for inst in active_instances]
        )
        checked_instance_ids: Set[str] = set()
        for inst in active_instances:
            defn = defs_by_id.get(inst.definition_id)
            if defn and defn.status != "disabled":
                checked_instance_ids.add(inst.id)

        try:
            # Build handler_key → instance_id map so dynamic checks emit
            # findings stamped with the correct per-table instance_id (globals
            # are gone — a dynamic finding without a matching approved instance
            # would otherwise be dropped inside run_dynamic_checks).
            # allowed_codes is derived from the same set — passed as empty
            # (not None) so no-approved-handler tables run zero dynamic
            # checks (see FindingsAgent for the reasoning).
            instance_id_by_handler_key: Dict[str, str] = {}
            allowed_codes: Set[str] = set()
            for inst in active_instances:
                defn = defs_by_id.get(inst.definition_id)
                if defn and defn.check_kind == "python_handler" and defn.handler_key:
                    instance_id_by_handler_key[defn.handler_key.lower()] = inst.id
                    allowed_codes.add(defn.handler_key.upper())

            # Use a sentinel scan_id so we don't create new Finding rows
            findings_data = rule_engine.execute_all_rules(
                table_asset, column_assets, scan_id="__verification__",
                allowed_rule_codes=allowed_codes,
                allowed_instance_ids=active_instance_ids,
                instance_id_by_handler_key=instance_id_by_handler_key,
                source=source,
            )
            for fd in findings_data:
                inst_id = fd.get("instance_id")
                if inst_id:
                    still_firing_instance_ids.add(inst_id)
                    continue
                # Only reached if a finding somehow lacks an instance_id — the
                # legacy tuple key is our last-resort backstop.
                ctx = fd.get("context") or {}
                rule_code = ctx.get("rule_code", "")
                asset_fqn = ctx.get("fqn", "")
                if rule_code and asset_fqn:
                    still_firing_legacy.add((rule_code, asset_fqn))

            still_firing_total = len(still_firing_instance_ids) + len(still_firing_legacy)
            logger.info(
                f"[VerificationAgent] {still_firing_total} violations still present "
                f"({len(still_firing_instance_ids)} by instance_id, {len(still_firing_legacy)} legacy) "
                f"out of original findings"
            )
        except Exception as e:
            logger.error(f"[VerificationAgent] Rule re-run failed: {e}")
            raise

        storage.update_agent_task(task.id, output={"progress": "Checking which findings are now resolved..."})

        # ── Step 3: Compare against open findings ─────────────────────────────
        # Query ALL open findings for this table's assets — not just the
        # originating scan's findings. The lifecycle model updates findings in
        # place across rescans (SCAN_ID never changes after creation), so
        # findings from prior runs on the same table would be permanently stuck
        # open if we filtered by scan_id.
        all_asset_ids = {table_asset.id} | {col.id for col in column_assets}
        open_findings = storage.list_open_findings_for_assets(all_asset_ids)

        # Batch-fetch asset rows for logging and the legacy (rule_code, fqn) key.
        # The primary comparison path uses instance_id and never needs this.
        finding_asset_ids = list({f.asset_id for f in open_findings if f.asset_id})
        assets_by_id = storage.get_assets_by_ids(finding_asset_ids)

        newly_resolved = 0
        still_open = 0
        logged_instance_ids: Set[str] = set()

        for finding in open_findings:
            ctx = finding.context or {}
            rule_code = ctx.get("rule_code", "")
            asset = assets_by_id.get(finding.asset_id)
            asset_fqn = ctx.get("fqn", "") or (asset.fqn if asset else "")

            if finding.instance_id:
                still_firing_now = finding.instance_id in still_firing_instance_ids
            elif still_firing_legacy:
                # Legacy finding with no instance_id — use the old string key
                # only when we actually collected legacy keys (i.e. something
                # re-fired via the legacy path). If still_firing_legacy is empty
                # it means the legacy re-run path produced nothing — which is
                # indistinguishable from "rule passed" vs "rule never ran", so
                # we must NOT auto-resolve these findings.
                still_firing_now = (rule_code, asset_fqn) in still_firing_legacy
            else:
                # No instance_id and no legacy re-fire data — treat as still
                # open (unverifiable) rather than incorrectly auto-resolving.
                still_open += 1
                continue

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

            # Only auto-resolve if the rule was actually re-run (definition not disabled).
            # If the definition is disabled the rule was skipped, not passed.
            rule_was_checked = (
                finding.instance_id is None or finding.instance_id in checked_instance_ids
            )

            if not still_firing_now and rule_was_checked:
                # Route through the lifecycle helper so LAST_SCAN_ID + audit
                # trail stay consistent with scan-time RESOLVE (this used to
                # bypass it and leave a stale LAST_SCAN_ID pointing at the
                # scan that ORIGINALLY detected the finding).
                storage.auto_resolve_finding(finding.id, scan_id="__verification__")
                storage.update_finding(
                    finding.id,
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

        total = len(open_findings)
        pct = round((newly_resolved / total * 100) if total > 0 else 0)

        result = {
            "total_findings": total,
            "resolved": newly_resolved,
            "newly_auto_resolved": newly_resolved,
            "remaining": still_open,
            "resolution_pct": pct,
            "fully_resolved": still_open == 0,
        }

        logger.info(
            f"[VerificationAgent] Done — {newly_resolved}/{total} resolved, "
            f"{still_open} remaining"
        )
        return result
