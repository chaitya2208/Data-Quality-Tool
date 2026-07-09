"""
WorkflowCoordinator — new pipeline:

  Coordinator
      ↓
  [Metadata Agent ∥ Rules Fetch Agent]   ← parallel threads
      ↓
  Rule Intelligence Agent                ← Claude: selects + generates rules
      ↓
  Findings Agent                         ← runs rules, persists findings
      ↓  [developer fixes — manual]
  Verification Agent                     ← re-scans, auto-completes when 0 remaining
"""
import logging
import threading
from datetime import datetime
from typing import List, Any

from app.services import storage
from app.services.snowflake_session import session as sf_session

logger = logging.getLogger(__name__)

AGENT_ORDER = [
    "coordinator",
    "metadata_agent",
    "rules_fetch_agent",
    "rule_intelligence_agent",
    "findings_agent",
    "fix_issues",          # UI-only node
    "verification_agent",
]

DB_AGENT_ORDER = [
    "coordinator",
    "metadata_agent",
    "rules_fetch_agent",
    "rule_intelligence_agent",
    "findings_agent",
    "verification_agent",
]


class WorkflowCoordinator:

    def __init__(self, run_id: str):
        self.run_id = run_id

    def run(self) -> None:
        try:
            self._execute()
        except Exception as e:
            logger.error(f"[Coordinator] Unhandled error in run {self.run_id}: {e}")
            self._mark_run_failed(str(e))

    def _execute(self) -> None:
        run = storage.get_agent_run(self.run_id)
        if not run:
            raise ValueError(f"AgentRun {self.run_id} not found")

        storage.update_agent_run(self.run_id, status="running", started_at=datetime.utcnow())

        all_downstream = [
            "metadata_agent", "rules_fetch_agent", "rule_intelligence_agent",
            "findings_agent", "verification_agent",
        ]

        # ── Coordinator: validate target ──────────────────────────────────────
        coord_task = self._get_task("coordinator")
        self._start_task(coord_task)
        try:
            rows = sf_session.query(
                f"SHOW TABLES LIKE '{run.table}' IN {run.database}.{run.schema_name}"
            )
            match = next(
                (r for r in rows if (r.get("name") or "").upper() == run.table.upper()),
                None,
            )
            if not match:
                raise ValueError(
                    f"Table {run.database}.{run.schema_name}.{run.table} not found in Snowflake"
                )
            self._complete_task(coord_task, output={
                "target":    f"{run.database}.{run.schema_name}.{run.table}",
                "row_count": match.get("rows"),
            })
        except Exception as e:
            self._fail_task(coord_task, str(e))
            self._skip_tasks(all_downstream)
            self._mark_run_failed(str(e))
            return

        # ── Parallel: Metadata Agent + Rules Fetch Agent ──────────────────────
        meta_task   = self._get_task("metadata_agent")
        rules_task  = self._get_task("rules_fetch_agent")
        self._start_task(meta_task)
        self._start_task(rules_task)

        meta_result  = {"scan_id": None, "table_asset_id": None, "column_ids": None, "error": None}
        rules_result = {"rule_codes": None, "error": None}

        def _run_metadata():
            try:
                from app.services.agents.metadata_agent import MetadataAgent
                scan, table_asset, column_assets = MetadataAgent().run(
                    run.database, run.schema_name, run.table
                )
                meta_result["scan_id"]        = scan.id
                meta_result["table_asset_id"] = table_asset.id
                meta_result["column_ids"]     = [c.id for c in column_assets]
                storage.update_agent_run(self.run_id, scan_id=scan.id)
                self._complete_task(meta_task, output={
                    "scan_id":       scan.id,
                    "columns_found": len(column_assets),
                    "table_fqn":     table_asset.fqn,
                    "row_count":     table_asset.row_count,
                })
            except Exception as e:
                meta_result["error"] = str(e)
                self._fail_task(meta_task, str(e))

        def _run_rules_fetch():
            try:
                from app.services.agents.rules_fetch_agent import RulesFetchAgent
                rules = RulesFetchAgent().run()
                rules_result["rule_codes"] = [r.code for r in rules]
                self._complete_task(rules_task, output={
                    "rules_loaded": len(rules),
                    "rule_codes": rules_result["rule_codes"],
                })
            except Exception as e:
                rules_result["error"] = str(e)
                self._fail_task(rules_task, str(e))

        t1 = threading.Thread(target=_run_metadata,    daemon=True)
        t2 = threading.Thread(target=_run_rules_fetch, daemon=True)
        t1.start(); t2.start()
        t1.join();  t2.join()

        run = storage.get_agent_run(self.run_id)

        if meta_result["error"]:
            self._skip_tasks(["rule_intelligence_agent", "findings_agent", "verification_agent"])
            self._mark_run_failed(meta_result["error"])
            return

        scan          = storage.get_scan(meta_result["scan_id"])
        table_asset   = storage.get_asset(meta_result["table_asset_id"])
        column_assets = [storage.get_asset(cid) for cid in meta_result["column_ids"]]

        # Re-fetch rules (or load all active if fetch failed)
        if rules_result["rule_codes"]:
            existing_rules = [storage.get_rule_by_code(c) for c in rules_result["rule_codes"]]
            existing_rules = [r for r in existing_rules if r]
        else:
            if rules_result["error"]:
                logger.warning(f"[Coordinator] Rules fetch failed: {rules_result['error']}, loading all active rules")
            from app.services.rule_engine import RuleEngine
            engine = RuleEngine()
            seen, existing_rules = set(), []
            for r in engine.get_active_rules("table") + engine.get_active_rules("column"):
                if r.code not in seen:
                    seen.add(r.code)
                    existing_rules.append(r)

        # ── Rule Intelligence Agent ───────────────────────────────────────────
        intel_task = self._get_task("rule_intelligence_agent")
        self._start_task(intel_task)
        intel_result = None
        agent = None
        try:
            from app.services.agents.rule_intelligence_agent import RuleIntelligenceAgent
            agent = RuleIntelligenceAgent()
            intel_result = agent.run(table_asset, column_assets, existing_rules, run.id)

            classification = intel_result["classification"]
            ai_rules       = intel_result["ai_rules"]
            ai_violations  = intel_result["ai_violations"]

            storage.update_agent_run(self.run_id, ai_rules_count=len(ai_rules))

            # Build readable output for UI log
            selected = agent.get_selected_codes(classification)
            skipped  = agent.get_skipped_codes(classification)
            existing_rules_raw = classification.get("existing_rules", {})

            self._complete_task(intel_task, output={
                "table_type":            classification["table_type"],
                "table_type_confidence": classification["table_type_confidence"],
                "table_type_reason":     classification["table_type_reason"],
                "existing_rules_selected": len(selected),
                "existing_rules_skipped":  len(skipped),
                "ai_rules_generated":    len(ai_rules),
                "ai_violations_found":   len(ai_violations),
                "skipped": {
                    code: existing_rules_raw.get(code, {}).get("reason", "")
                    for code in skipped
                },
                "selected_with_overrides": {
                    code: {
                        "severity_override": existing_rules_raw.get(code, {}).get("severity_override"),
                        "reason": existing_rules_raw.get(code, {}).get("reason", ""),
                    }
                    for code in selected
                    if existing_rules_raw.get(code, {}).get("severity_override")
                },
                "ai_rules": [
                    {
                        "code":     r.code,
                        "name":     r.name,
                        "violated": any(
                            v.get("context", {}).get("rule_code") == r.code
                            for v in ai_violations
                        ),
                    }
                    for r in ai_rules
                ],
            })
        except Exception as e:
            logger.error(f"[Coordinator] RuleIntelligenceAgent failed: {e}")
            self._fail_task(intel_task, str(e))
            # Non-fatal — run all existing rules with no AI enhancement
            intel_result = {
                "classification": {"table_type": "unknown", "existing_rules": {}},
                "ai_rules":       [],
                "ai_violations":  [],
            }
            from app.services.agents.rule_intelligence_agent import RuleIntelligenceAgent
            agent = RuleIntelligenceAgent()

        # ── PAUSE: build initial rule_review_state and wait for user review ─────
        classification     = intel_result["classification"]
        ai_rules           = intel_result["ai_rules"]
        ai_violations      = intel_result["ai_violations"]
        existing_rules_raw = classification.get("existing_rules", {})

        # Build initial review state from Claude's decisions
        active_rules = []
        skipped_rules = []

        # Existing rules
        for rule in existing_rules:
            decision = existing_rules_raw.get(rule.code, {})
            entry = {
                "code":             rule.code,
                "name":             rule.name,
                "description":      rule.description,
                "severity":         decision.get("severity_override") or rule.severity,
                "original_severity": rule.severity,
                "reason":           decision.get("reason", ""),
                "is_ai_generated":  False,
                "category":         rule.category,
                "applies_to":       rule.applies_to or [],
            }
            if decision.get("run", True):
                active_rules.append(entry)
            else:
                skipped_rules.append(entry)

        # AI-generated rules (all start in active by default)
        for ai_rule in ai_rules:
            violated = any(
                v.get("context", {}).get("rule_code") == ai_rule.code
                for v in ai_violations
            )
            active_rules.append({
                "code":             ai_rule.code,
                "name":             ai_rule.name,
                "description":      ai_rule.description,
                "severity":         ai_rule.severity,
                "original_severity": ai_rule.severity,
                "reason":           "AI-generated rule for this table type",
                "is_ai_generated":  True,
                "category":         ai_rule.category,
                "applies_to":       ai_rule.applies_to or [],
                "violated":         violated,
                "ai_violation_evidence": next(
                    (v.get("evidence", {}).get("ai_evidence", "") for v in ai_violations
                     if v.get("context", {}).get("rule_code") == ai_rule.code),
                    ""
                ),
            })

        storage.update_agent_run(
            self.run_id,
            rule_review_state={"active": active_rules, "skipped": skipped_rules},
            status="awaiting_rule_review",
        )
        logger.info(
            f"[Coordinator] Run {self.run_id} paused for rule review — "
            f"{len(active_rules)} active, {len(skipped_rules)} skipped."
        )

        # Rules are ready — advance the batch now so the next table computes its
        # rules while the user reviews this one at their own pace.
        self._advance_batch()

    # ── Phase 2: triggered by POST /runs/{id}/run-pipeline ───────────────────

    def run_pipeline_after_review(self, snowflake_user: str = "data-governance-team") -> None:
        """Run FindingsAgent using the user-approved rule set from rule_review_state."""
        try:
            self._run_findings(snowflake_user)
        except Exception as e:
            logger.error(f"[Coordinator] run_pipeline error in run {self.run_id}: {e}")
            self._mark_run_failed(str(e), advance=False)  # already advanced at review

    def _run_findings(self, snowflake_user: str) -> None:
        run = storage.get_agent_run(self.run_id)
        if not run:
            raise ValueError(f"AgentRun {self.run_id} not found")

        storage.update_agent_run(self.run_id, status="running")

        review_state = run.rule_review_state or {"active": [], "skipped": []}
        active_entries = review_state.get("active", [])

        # ── Persist approved AI rules to the permanent rules library ─────────
        ai_rules_persisted = []
        for entry in active_entries:
            if not entry.get("is_ai_generated"):
                continue
            code = entry["code"]
            existing = storage.get_rule_by_code(code)
            if existing:
                # Update name/description/severity with user's edits
                updated = storage.update_rule(
                    existing.id,
                    name=entry["name"],
                    description=entry["description"],
                    severity=entry["severity"],
                    is_active=True,
                    status="active",
                    owner=snowflake_user,
                )
                ai_rules_persisted.append(updated)
            else:
                # New AI rule — create permanently
                category = entry.get("category", "data_quality")
                severity = entry["severity"]
                new_rule = storage.create_rule(
                    code=code,
                    name=entry["name"],
                    description=entry["description"],
                    category=category,
                    severity=severity,
                    applies_to=entry.get("applies_to") or ["table"],
                    rule_config={"source_run_id": run.id, "ai_generated": True},
                    is_active=True,
                    status="active",
                    owner=snowflake_user,
                    created_by="rule_intelligence_agent",
                    version=1,
                )
                ai_rules_persisted.append(new_rule)

        storage.update_agent_run(self.run_id, ai_rules_count=len(ai_rules_persisted))

        # ── Re-fetch assets ───────────────────────────────────────────────────
        scan = storage.get_scan(run.scan_id)
        if not scan:
            raise ValueError(f"Scan {run.scan_id} not found")

        table_asset = storage.get_table_asset(run.database, run.schema_name, run.table)
        if not table_asset:
            raise ValueError(f"Table asset not found for {run.database}.{run.schema_name}.{run.table}")

        column_assets = storage.list_column_assets(run.database, run.schema_name, run.table)

        # ── Build approved set for FindingsAgent ─────────────────────────────
        approved_codes = {e["code"] for e in active_entries}

        # Build a classification dict from approved entries for severity overrides
        existing_rules_overrides = {}
        for entry in active_entries:
            if not entry.get("is_ai_generated"):
                existing_rules_overrides[entry["code"]] = {
                    "run": True,
                    "severity_override": (
                        entry["severity"]
                        if entry["severity"] != entry.get("original_severity")
                        else None
                    ),
                    "reason": entry.get("reason", ""),
                }

        classification = {
            "table_type":      (run.rule_review_state or {}).get("table_type", "unknown"),
            "existing_rules":  existing_rules_overrides,
        }

        # AI violations from active AI rules only
        ai_violations = []
        for entry in active_entries:
            if entry.get("is_ai_generated") and entry.get("violated"):
                rule_obj = storage.get_rule_by_code(entry["code"])
                if rule_obj and table_asset:
                    col_name = ""
                    if "column" in (entry.get("applies_to") or []):
                        col_name = entry.get("column_name", "")
                    asset = table_asset
                    if col_name:
                        col_fqn = f"{table_asset.fqn}.{col_name}"
                        asset = storage.get_asset_by_fqn(col_fqn) or table_asset

                    ai_violations.append({
                        "asset_id":    asset.id,
                        "scan_id":     None,
                        "rule_id":     rule_obj.id,
                        "title":       f"{entry['name']} violated",
                        "description": entry.get("ai_violation_evidence") or entry["description"],
                        "severity":    entry["severity"],
                        "status":      "detected",
                        "context": {
                            "rule_code":     entry["code"],
                            "fqn":           table_asset.fqn,
                            "table_name":    run.table,
                            "schema_name":   run.schema_name,
                            "database_name": run.database,
                            "column_name":   col_name,
                            "ai_generated":  True,
                        },
                        "evidence": {"ai_evidence": entry.get("ai_violation_evidence", "")},
                    })

        # ── Findings Agent ────────────────────────────────────────────────────
        findings_task = self._get_task("findings_agent")
        self._start_task(findings_task)
        try:
            from app.services.agents.findings_agent import FindingsAgent
            from app.services.agents.rule_intelligence_agent import RuleIntelligenceAgent

            _ria = RuleIntelligenceAgent()
            findings = FindingsAgent().run(
                scan, table_asset, column_assets,
                classification, ai_violations, _ria,
                allowed_codes=approved_codes,
            )
            storage.update_agent_run(self.run_id, findings_count=len(findings))

            sev_breakdown = {}
            for f in findings:
                sev_breakdown[f.severity] = sev_breakdown.get(f.severity, 0) + 1

            self._complete_task(findings_task, output={
                "findings_count":     len(findings),
                "ai_rule_findings":   len(ai_violations),
                "severity_breakdown": sev_breakdown,
                "scan_id":            scan.id,
                "ai_rules_persisted": len(ai_rules_persisted),
            })
        except Exception as e:
            self._fail_task(findings_task, str(e))
            self._skip_tasks(["verification_agent"])
            self._mark_run_failed(str(e), advance=False)  # already advanced at review
            return

        run = storage.update_agent_run(self.run_id, status="awaiting_fixes")
        logger.info(
            f"[Coordinator] Run {self.run_id} — pipeline complete. "
            f"{run.findings_count} findings, {run.ai_rules_count} AI rules persisted."
        )

        # Start background auto-verification (every 5 min while awaiting fixes)
        from app.services.agents.auto_verify_scheduler import schedule as schedule_verify
        schedule_verify(run.id)
        # Note: batch already advanced when this run reached rule review — do not
        # advance again here, or a table could be skipped.

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _get_task(self, name: str) -> Any:
        return storage.get_agent_task(self.run_id, name)

    def _start_task(self, task: Any) -> None:
        storage.update_agent_task(task.id, status="running", started_at=datetime.utcnow())

    def _complete_task(self, task: Any, output: dict = None) -> None:
        storage.update_agent_task(task.id, status="completed", completed_at=datetime.utcnow(), output=output or {})

    def _fail_task(self, task: Any, error: str) -> None:
        storage.update_agent_task(task.id, status="failed", completed_at=datetime.utcnow(), error_message=error[:1024])

    def _skip_task(self, task: Any) -> None:
        storage.update_agent_task(task.id, status="skipped")

    def _skip_tasks(self, names: List[str]) -> None:
        for name in names:
            t = self._get_task(name)
            if t:
                self._skip_task(t)

    def _mark_run_failed(self, error: str, advance: bool = True) -> None:
        run = storage.get_agent_run(self.run_id)
        if run:
            storage.update_agent_run(self.run_id, status="failed", completed_at=datetime.utcnow(), error_message=error[:1024])
            # Don't let one bad table stall the rest of the batch. Only advance for
            # pre-review failures — post-review failures already advanced at the
            # rule-review point, so advancing again would skip a table.
            if advance:
                self._advance_batch()

    def _advance_batch(self) -> None:
        """
        Sequential batch processing: once this run has reached rule review /
        awaiting fixes / failed, kick off the next pending run in the same batch.
        No-op for single-table runs (batch_id is None or only one member).
        """
        run = storage.get_agent_run(self.run_id)
        if not run or not run.batch_id:
            return

        next_run = storage.get_next_pending_batch_run(run.batch_id)
        if not next_run:
            logger.info(f"[Coordinator] Batch {run.batch_id} — no more pending tables.")
            return

        next_id = next_run.id
        logger.info(
            f"[Coordinator] Batch {run.batch_id} — advancing to "
            f"{next_run.database}.{next_run.schema_name}.{next_run.table} (run {next_id})"
        )
        # Run in a fresh daemon thread so we don't block the current request/thread
        threading.Thread(
            target=WorkflowCoordinator(run_id=next_id).run, daemon=True
        ).start()


def _severity_counts(findings) -> dict:
    counts: dict = {}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    return counts
