"""
Findings Agent — runs every approved instance (python_handler, dynamic, and
sql_template alike) through RuleEngine and persists all findings, logging
one RULE_EXECUTIONS row per instance evaluated.

sql_template instances (Claude-authored checks with real, validated SQL) are
executed live here exactly like the built-in checks — there is no separate
"AI violation" one-time path anymore. A Claude-authored check is re-run on
every findings pass and every verification pass, same as any other rule.

Receives:
  - scan: Scan (from MetadataAgent)
  - table_asset, column_assets: from MetadataAgent
  - allowed_instance_ids: the approved RULE_INSTANCES ids to run
  - severity_overrides: {instance_id: severity} for human/Claude edits

Returns: List[Finding] — all persisted findings for this run.
"""
import logging
from datetime import datetime
from typing import List, Dict, Any, Set, Optional

from app.services import storage
from app.services.rule_engine import RuleEngine
from app.services.scan_finalizer import finalize_scan

logger = logging.getLogger(__name__)


class FindingsAgent:
    """
    Executes every approved instance (with severity overrides) and persists
    all resulting findings. Logs a RULE_EXECUTIONS row per instance run.
    """

    def __init__(self):
        self.rule_engine = RuleEngine()

    def run(
        self,
        scan: Any,
        table_asset: Any,
        column_assets: List[Any],
        allowed_instance_ids: Set[str],
        severity_overrides: Optional[Dict[str, str]] = None,
        run_id: Optional[str] = None,
    ) -> List[Any]:
        severity_overrides = severity_overrides or {}

        allowed_codes = self._resolve_handler_codes(allowed_instance_ids)
        instance_id_by_handler_key = self._resolve_instance_id_map(allowed_instance_ids)

        logger.info(
            f"[FindingsAgent] Running {len(allowed_instance_ids)} approved instances on {table_asset.fqn}"
        )

        # Resolve the finding's own data source so sql_template checks run
        # against the right database (Snowflake OR Postgres/RDS), not always
        # the shared Snowflake session.
        source = None
        try:
            from app.services.datasources import get_source
            source = get_source(getattr(scan, "connection_id", None))
        except Exception as e:
            logger.warning(f"[FindingsAgent] Could not resolve source for scan {scan.id}: {e}")

        # Pass allowed_codes as an EMPTY set (not None) when there are no
        # approved python_handler instances — the downstream _allowed() check
        # treats None as "allow everything," which used to be intentional for
        # the globals era but is now a bug: it lets every dynamic check
        # function fire against a table that Claude never proposed the check
        # for. An empty set means "allow nothing," which is correct now that
        # every handler instance is per-table proposed.
        findings_data = self.rule_engine.execute_all_rules(
            table_asset, column_assets, scan.id,
            allowed_rule_codes=allowed_codes,
            allowed_instance_ids=allowed_instance_ids,
            instance_id_by_handler_key=instance_id_by_handler_key,
            source=source,
        )

        # Schema drift findings — computed in MetadataAgent's scan pass and
        # stashed on the scan object. Merge in BEFORE severity overrides so
        # drift severity can also be overridden if configured. Their
        # instance_ids are threaded into executed_instance_ids so the
        # finalizer's PASS branch can auto-resolve a drift incident once the
        # schema is stable again (e.g. a removed column re-added).
        # Template path attaches drift_findings via setattr on the live scan
        # object; agentic path re-fetches scan from storage after the review
        # pause, which drops the attribute. Fall back to scan_results, where
        # scan_metadata_only persists a durable copy.
        drift_findings = list(
            getattr(scan, "drift_findings", None)
            or (scan.scan_results or {}).get("drift_findings", [])
            or []
        )
        drift_executed_iids: Set[str] = {
            f["instance_id"] for f in drift_findings if f.get("instance_id")
        }
        if drift_findings:
            findings_data = list(findings_data) + drift_findings
            logger.info(f"[FindingsAgent] Merged {len(drift_findings)} drift finding(s)")

        # Severity overrides are applied to the produced findings in memory —
        # NEVER by mutating the shared instance row. The old approach wrote the
        # override onto RULE_INSTANCES, ran the check, then restored it; if
        # execute_all_rules raised, the restore never happened and the instance
        # was left permanently overridden. Worse, batch tables advance in
        # parallel threads (coordinator._advance_batch) and can share a global
        # ('*'-scoped) instance, so one run's temporary mutation was visible to
        # another run mid-scan. Rewriting the finding dict here avoids both.
        self._apply_severity_overrides(findings_data, severity_overrides)

        # Log RULE_EXECUTIONS for every instance that actually ran (pass or fail).
        # This is the durable "the check ran" audit trail — separate from
        # incidents. Trend charts + rule-execution history feed off this.
        executed_instance_ids = self._log_executions(
            findings_data, allowed_instance_ids, scan.id, run_id,
        )
        # Drift instances aren't in allowed_instance_ids (they're auto-
        # provisioned per-table by schema_drift, not part of the approved
        # rule set for this scan). Include them in the finalizer's executed
        # set so PASS-branch auto-resolve fires when drift disappears.
        for iid in drift_executed_iids:
            executed_instance_ids.add(iid)
        # Also mark passed drift instances: for any drift handler we didn't
        # emit a finding for this scan, log a PASSED execution + include in
        # the executed set. This is what lets a prior drift incident resolve.
        drift_failed_handler_keys = {
            (f.get("context") or {}).get("rule_code", "").lower()
            for f in drift_findings
        }
        from app.services.schema_drift import DRIFT_HANDLER_KEYS, _get_per_table_drift_instance
        for hk in DRIFT_HANDLER_KEYS:
            if hk in drift_failed_handler_keys:
                continue  # already accounted for via drift_executed_iids
            # Only log a PASS if a prior open incident exists. Use the
            # read-only getter so we don't eagerly provision all 5 drift
            # instances on every scan of every table.
            inst = _get_per_table_drift_instance(
                hk, table_asset.database_name, table_asset.schema_name,
                table_asset.table_name,
            )
            if inst and storage.find_open_finding(inst.id, table_asset.id):
                storage.create_execution(
                    instance_id=inst.id, status="passed",
                    scan_id=scan.id, run_id=run_id, evidence=None,
                )
                executed_instance_ids.add(inst.id)

        # Incident lifecycle: UPDATE / RESOLVE / REOPEN / CREATE per
        # (instance, asset). Replaces the old supersede-then-bulk-insert flow.
        # A finding is a persistent object across scans, not a per-run twin.
        stats = finalize_scan(
            scan_id=scan.id,
            asset_id_for_passed=table_asset.id,
            findings_data=findings_data,
            executed_instance_ids=executed_instance_ids,
        )
        logger.info(f"[FindingsAgent] Lifecycle: {stats}")

        # findings_count on SCANS reflects "open incidents involving this
        # scan" — new + reopened + still-failing updates. Auto-resolved don't
        # count as findings THIS scan created (they were pre-existing).
        active_findings_count = stats["created"] + stats["reopened"] + stats["updated"]

        storage.update_scan(
            scan.id,
            rules_checked=len(allowed_instance_ids),
            findings_count=active_findings_count,
            status="completed",
            completed_at=datetime.utcnow(),
        )

        findings = storage.list_findings_by_scan(scan.id)
        # Annotate each finding with the lifecycle event that produced it in
        # this scan (created / updated / reopened) plus prev/curr counts, so
        # the UI can show "was N failing rows, now M" instead of a static
        # count that hides whether anything changed. Events for findings this
        # scan didn't touch (rare — e.g. a finding on a different asset) get
        # no event and the UI treats them as pre-existing / unchanged.
        events_by_id = {e["finding_id"]: e for e in stats.get("events", [])}
        for f in findings:
            evt = events_by_id.get(f.id)
            if evt:
                f.lifecycle_event = evt["event"]
                f.prev_fail_count = evt.get("prev_fail_count")
                f.prev_total_count = evt.get("prev_total_count")
        logger.info(
            f"[FindingsAgent] Done — {len(findings)} findings "
            f"(created={stats['created']} updated={stats['updated']} "
            f"reopened={stats['reopened']} auto_resolved={stats['resolved']})"
        )
        # Stash lifecycle stats on the scan object so the coordinator can
        # write them into the findings_agent task output without needing a
        # separate return channel.
        setattr(scan, "lifecycle_stats", stats)
        return findings

    def _resolve_handler_codes(self, instance_ids: Set[str]) -> Set[str]:
        codes = set()
        for instance_id in instance_ids:
            instance = storage.get_instance(instance_id)
            if not instance:
                continue
            definition = storage.get_definition(instance.definition_id)
            if not definition or definition.status == "disabled":
                continue
            if definition.check_kind == "python_handler" and definition.handler_key:
                codes.add(definition.handler_key.upper())
        return codes

    def _resolve_instance_id_map(self, instance_ids: Set[str]) -> Dict[str, str]:
        """{handler_key_lower: instance_id} for approved python_handler
        instances. Threaded into run_dynamic_checks so each finding it emits
        gets stamped with its approved per-table instance_id (globals are
        gone; a dynamic-check finding with no matching approved instance is
        dropped inside run_dynamic_checks)."""
        mapping: Dict[str, str] = {}
        for instance_id in instance_ids:
            instance = storage.get_instance(instance_id)
            if not instance:
                continue
            definition = storage.get_definition(instance.definition_id)
            if not definition or definition.status == "disabled":
                continue
            if definition.check_kind == "python_handler" and definition.handler_key:
                mapping[definition.handler_key.lower()] = instance_id
        return mapping

    def _apply_severity_overrides(
        self, findings_data: List[dict], overrides: Dict[str, str],
    ) -> None:
        """Rewrite each finding's severity from the human/Claude override map,
        keyed by instance_id. In-memory only — the shared RULE_INSTANCES row is
        never touched, so there is nothing to restore and nothing another
        concurrent run can observe."""
        if not overrides:
            return
        for fd in findings_data:
            override = overrides.get(fd.get("instance_id"))
            if override:
                fd["severity"] = override

    def _log_executions(
        self, findings_data: List[dict], allowed_instance_ids: Set[str],
        scan_id: str, run_id: Optional[str],
    ) -> Set[str]:
        """One RULE_EXECUTIONS row per instance that ran (python_handler or
        sql_template): FAILED if it produced at least one finding, PASSED
        otherwise. Returns the set of instance_ids that actually executed —
        used by the finalizer to identify the PASS set (executed - failed)."""
        failed_ids = {fd.get("instance_id") for fd in findings_data if fd.get("instance_id")}
        executed: Set[str] = set()
        for instance_id in allowed_instance_ids:
            instance = storage.get_instance(instance_id)
            if not instance:
                continue
            definition = storage.get_definition(instance.definition_id)
            if not definition or definition.check_kind not in ("python_handler", "sql_template"):
                continue
            if definition.check_kind == "python_handler" and not definition.handler_key:
                continue
            status = "failed" if instance_id in failed_ids else "passed"
            evidence = None
            if status == "failed":
                # Attach the evidence contract so RULE_EXECUTIONS carries
                # fail_count/total_count for trend charts without needing a
                # join back to FINDINGS.
                fd = next((f for f in findings_data if f.get("instance_id") == instance_id), None)
                if fd:
                    evidence = fd.get("evidence")
            storage.create_execution(
                instance_id=instance_id, status=status,
                scan_id=scan_id, run_id=run_id, evidence=evidence,
            )
            executed.add(instance_id)
        return executed
