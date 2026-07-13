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

        logger.info(
            f"[FindingsAgent] Running {len(allowed_instance_ids)} approved instances on {table_asset.fqn}"
        )

        findings_data = self.rule_engine.execute_all_rules(
            table_asset, column_assets, scan.id,
            allowed_rule_codes=allowed_codes if allowed_codes else None,
            allowed_instance_ids=allowed_instance_ids,
        )

        # Severity overrides are applied to the produced findings in memory —
        # NEVER by mutating the shared instance row. The old approach wrote the
        # override onto RULE_INSTANCES, ran the check, then restored it; if
        # execute_all_rules raised, the restore never happened and the instance
        # was left permanently overridden. Worse, batch tables advance in
        # parallel threads (coordinator._advance_batch) and can share a global
        # ('*'-scoped) instance, so one run's temporary mutation was visible to
        # another run mid-scan. Rewriting the finding dict here avoids both.
        self._apply_severity_overrides(findings_data, severity_overrides)

        # Log RULE_EXECUTIONS for every instance that actually ran
        self._log_executions(findings_data, allowed_instance_ids, scan.id, run_id)

        # Persist all findings
        storage.create_findings_bulk(findings_data)

        storage.update_scan(
            scan.id,
            rules_checked=len(allowed_instance_ids),
            findings_count=len(findings_data),
            status="completed",
            completed_at=datetime.utcnow(),
        )

        findings = storage.list_findings_by_scan(scan.id)
        logger.info(f"[FindingsAgent] Done — {len(findings)} findings")
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
    ) -> None:
        """One RULE_EXECUTIONS row per instance that ran (python_handler or
        sql_template): FAILED if it produced at least one finding, PASSED
        otherwise."""
        failed_ids = {fd.get("instance_id") for fd in findings_data if fd.get("instance_id")}
        for instance_id in allowed_instance_ids:
            instance = storage.get_instance(instance_id)
            if not instance:
                continue
            definition = storage.get_definition(instance.definition_id)
            if not definition or definition.check_kind not in ("python_handler", "sql_template"):
                continue  # not actually executed this pass
            if definition.check_kind == "python_handler" and not definition.handler_key:
                continue
            status = "failed" if instance_id in failed_ids else "passed"
            storage.create_execution(
                instance_id=instance_id, status=status,
                scan_id=scan_id, run_id=run_id,
            )
