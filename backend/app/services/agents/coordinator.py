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
from typing import List, Optional
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models.agent_run import AgentRun, AgentTask, AgentRunStatus, AgentTaskStatus
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
        db = SessionLocal()
        try:
            self._execute(db)
        except Exception as e:
            logger.error(f"[Coordinator] Unhandled error in run {self.run_id}: {e}")
            db2 = SessionLocal()
            try:
                self._mark_run_failed(db2, str(e))
            finally:
                db2.close()
        finally:
            db.close()

    def _execute(self, db: Session) -> None:
        run = db.query(AgentRun).filter(AgentRun.id == self.run_id).first()
        if not run:
            raise ValueError(f"AgentRun {self.run_id} not found")

        run.status = AgentRunStatus.RUNNING
        run.started_at = datetime.utcnow()
        db.commit()

        all_downstream = [
            "metadata_agent", "rules_fetch_agent", "rule_intelligence_agent",
            "findings_agent", "verification_agent",
        ]

        # ── Coordinator: validate target ──────────────────────────────────────
        coord_task = self._get_task(db, "coordinator")
        self._start_task(db, coord_task)
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
            self._complete_task(db, coord_task, output={
                "target":    f"{run.database}.{run.schema_name}.{run.table}",
                "row_count": match.get("rows"),
            })
        except Exception as e:
            self._fail_task(db, coord_task, str(e))
            self._skip_tasks(db, all_downstream)
            self._mark_run_failed(db, str(e))
            return

        # ── Parallel: Metadata Agent + Rules Fetch Agent ──────────────────────
        meta_task   = self._get_task(db, "metadata_agent")
        rules_task  = self._get_task(db, "rules_fetch_agent")
        self._start_task(db, meta_task)
        self._start_task(db, rules_task)

        # Threads store only IDs — never ORM objects — to avoid detached-instance errors.
        # The main session re-fetches everything after threads complete.
        meta_result  = {"scan_id": None, "table_asset_id": None, "column_ids": None, "error": None}
        rules_result = {"rule_codes": None, "error": None}

        def _run_metadata():
            dbt = SessionLocal()
            try:
                from app.services.agents.metadata_agent import MetadataAgent
                scan, table_asset, column_assets = MetadataAgent(dbt).run(
                    run.database, run.schema_name, run.table
                )
                # Store only IDs — ORM objects must not cross session boundaries
                meta_result["scan_id"]        = scan.id
                meta_result["table_asset_id"] = table_asset.id
                meta_result["column_ids"]     = [c.id for c in column_assets]
                # Write scan_id onto the run row
                run_upd = dbt.query(AgentRun).filter(AgentRun.id == self.run_id).first()
                if run_upd:
                    run_upd.scan_id = scan.id
                    dbt.commit()
                self._complete_task_in(dbt, meta_task.id, output={
                    "scan_id":       scan.id,
                    "columns_found": len(column_assets),
                    "table_fqn":     table_asset.fqn,
                    "row_count":     table_asset.row_count,
                })
            except Exception as e:
                meta_result["error"] = str(e)
                self._fail_task_in(dbt, meta_task.id, str(e))
            finally:
                dbt.close()

        def _run_rules_fetch():
            dbt = SessionLocal()
            try:
                from app.services.agents.rules_fetch_agent import RulesFetchAgent
                rules = RulesFetchAgent(dbt).run()
                # Store only codes — re-fetch via main session later
                rules_result["rule_codes"] = [r.code for r in rules]
                self._complete_task_in(dbt, rules_task.id, output={
                    "rules_loaded": len(rules),
                    "rule_codes": rules_result["rule_codes"],
                })
            except Exception as e:
                rules_result["error"] = str(e)
                self._fail_task_in(dbt, rules_task.id, str(e))
            finally:
                dbt.close()

        t1 = threading.Thread(target=_run_metadata,    daemon=True)
        t2 = threading.Thread(target=_run_rules_fetch, daemon=True)
        t1.start(); t2.start()
        t1.join();  t2.join()

        # Reload run so main session sees scan_id written by the metadata thread
        db.expire_all()
        run = db.query(AgentRun).filter(AgentRun.id == self.run_id).first()

        if meta_result["error"]:
            self._skip_tasks(db, ["rule_intelligence_agent", "findings_agent", "verification_agent"])
            self._mark_run_failed(db, meta_result["error"])
            return

        # Re-fetch all objects in the main session — safe to use downstream
        from app.models.asset import Asset
        from app.models.scan import Scan
        from app.models.rule import Rule

        scan         = db.query(Scan).filter(Scan.id == meta_result["scan_id"]).first()
        table_asset  = db.query(Asset).filter(Asset.id == meta_result["table_asset_id"]).first()
        column_assets= db.query(Asset).filter(Asset.id.in_(meta_result["column_ids"])).all()

        # Re-fetch rules in main session (or load all active if fetch failed)
        if rules_result["rule_codes"]:
            existing_rules = db.query(Rule).filter(Rule.code.in_(rules_result["rule_codes"])).all()
        else:
            if rules_result["error"]:
                logger.warning(f"[Coordinator] Rules fetch failed: {rules_result['error']}, loading all active rules")
            from app.services.rule_engine import RuleEngine
            engine = RuleEngine(db)
            seen, existing_rules = set(), []
            for r in engine.get_active_rules("table") + engine.get_active_rules("column"):
                if r.code not in seen:
                    seen.add(r.code)
                    existing_rules.append(r)

        # ── Rule Intelligence Agent ───────────────────────────────────────────
        intel_task = self._get_task(db, "rule_intelligence_agent")
        self._start_task(db, intel_task)
        intel_result = None
        try:
            from app.services.agents.rule_intelligence_agent import RuleIntelligenceAgent
            agent = RuleIntelligenceAgent(db)
            intel_result = agent.run(table_asset, column_assets, existing_rules, run.id)

            classification = intel_result["classification"]
            ai_rules       = intel_result["ai_rules"]
            ai_violations  = intel_result["ai_violations"]

            run.ai_rules_count = len(ai_rules)
            db.commit()

            # Build readable output for UI log
            selected = agent.get_selected_codes(classification)
            skipped  = agent.get_skipped_codes(classification)
            existing_rules_raw = classification.get("existing_rules", {})

            self._complete_task(db, intel_task, output={
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
            self._fail_task(db, intel_task, str(e))
            # Non-fatal — run all existing rules with no AI enhancement
            intel_result = {
                "classification": {"table_type": "unknown", "existing_rules": {}},
                "ai_rules":       [],
                "ai_violations":  [],
            }
            # Build a minimal agent stub for FindingsAgent
            from app.services.agents.rule_intelligence_agent import RuleIntelligenceAgent
            agent = RuleIntelligenceAgent(db)

        # ── Findings Agent ────────────────────────────────────────────────────
        findings_task = self._get_task(db, "findings_agent")
        self._start_task(db, findings_task)
        try:
            from app.services.agents.findings_agent import FindingsAgent
            from app.services.agents.rule_intelligence_agent import RuleIntelligenceAgent as _RIA
            _agent = agent if intel_result else _RIA(db)
            findings = FindingsAgent(db).run(
                scan, table_asset, column_assets,
                intel_result["classification"],
                intel_result["ai_violations"],
                _agent,
            )
            run.findings_count = len(findings)
            db.commit()

            sev_breakdown = {}
            for f in findings:
                sev_breakdown[f.severity] = sev_breakdown.get(f.severity, 0) + 1

            self._complete_task(db, findings_task, output={
                "findings_count":    len(findings),
                "ai_rule_findings":  len(intel_result["ai_violations"]),
                "severity_breakdown": sev_breakdown,
                "scan_id":           scan.id,
            })
        except Exception as e:
            self._fail_task(db, findings_task, str(e))
            self._skip_tasks(db, ["verification_agent"])
            self._mark_run_failed(db, str(e))
            return

        # Pipeline complete — awaiting developer to fix issues
        run.status = AgentRunStatus.AWAITING_FIXES
        db.commit()
        logger.info(
            f"[Coordinator] Run {self.run_id} — pipeline complete. "
            f"{run.findings_count} findings, {run.ai_rules_count} AI rules. "
            f"Awaiting fixes."
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _get_task(self, db: Session, name: str) -> AgentTask:
        return db.query(AgentTask).filter(
            AgentTask.run_id == self.run_id,
            AgentTask.agent_name == name,
        ).first()

    def _start_task(self, db: Session, task: AgentTask) -> None:
        task.status = AgentTaskStatus.RUNNING
        task.started_at = datetime.utcnow()
        db.commit()

    def _complete_task(self, db: Session, task: AgentTask, output: dict = None) -> None:
        task.status = AgentTaskStatus.COMPLETED
        task.completed_at = datetime.utcnow()
        task.output = output or {}
        db.commit()

    def _fail_task(self, db: Session, task: AgentTask, error: str) -> None:
        task.status = AgentTaskStatus.FAILED
        task.completed_at = datetime.utcnow()
        task.error_message = error[:1024]
        db.commit()

    def _skip_task(self, db: Session, task: AgentTask) -> None:
        task.status = AgentTaskStatus.SKIPPED
        db.commit()

    def _skip_tasks(self, db: Session, names: List[str]) -> None:
        for name in names:
            t = self._get_task(db, name)
            if t:
                self._skip_task(db, t)

    # Thread-safe task updates — use task ID to avoid detached instance issues
    def _complete_task_in(self, dbt: Session, task_id: str, output: dict) -> None:
        t = dbt.query(AgentTask).filter(AgentTask.id == task_id).first()
        if t:
            t.status = AgentTaskStatus.COMPLETED
            t.completed_at = datetime.utcnow()
            t.output = output
            dbt.commit()

    def _fail_task_in(self, dbt: Session, task_id: str, error: str) -> None:
        t = dbt.query(AgentTask).filter(AgentTask.id == task_id).first()
        if t:
            t.status = AgentTaskStatus.FAILED
            t.completed_at = datetime.utcnow()
            t.error_message = error[:1024]
            dbt.commit()

    def _mark_run_failed(self, db: Session, error: str) -> None:
        run = db.query(AgentRun).filter(AgentRun.id == self.run_id).first()
        if run:
            run.status = AgentRunStatus.FAILED
            run.completed_at = datetime.utcnow()
            run.error_message = error[:1024]
            db.commit()


def _severity_counts(findings) -> dict:
    counts: dict = {}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    return counts
