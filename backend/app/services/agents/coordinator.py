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
    "profiling_agent",
    "rule_intelligence_agent",
    "findings_agent",
    "fix_issues",          # UI-only node
    "verification_agent",
]

DB_AGENT_ORDER = [
    "coordinator",
    "metadata_agent",
    "rules_fetch_agent",
    "profiling_agent",
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

        # ── Parallel: Metadata Agent + Rules Fetch Agent + Profiling Agent ─────
        meta_task    = self._get_task(db, "metadata_agent")
        rules_task   = self._get_task(db, "rules_fetch_agent")
        profile_task = self._get_task(db, "profiling_agent")
        self._start_task(db, meta_task)
        self._start_task(db, rules_task)
        if profile_task:
            self._start_task(db, profile_task)

        # Threads store only IDs — never ORM objects — to avoid detached-instance errors.
        # The main session re-fetches everything after threads complete.
        meta_result    = {"scan_id": None, "table_asset_id": None, "column_ids": None, "error": None}
        rules_result   = {"rule_codes": None, "error": None}
        profile_result = {"profile": None, "error": None}

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

        def _run_profiling():
            # Profiling is best-effort: if it fails, the pipeline still runs
            # rules on metadata alone — we just lose the data-informed boost.
            if not profile_task:
                return
            dbt = SessionLocal()
            try:
                from app.services.agents.profiling_agent import ProfilingAgent
                prof = ProfilingAgent(dbt).run(run.database, run.schema_name, run.table)
                profile_result["profile"] = prof
                anomalies = prof.get("anomalies", [])
                self._complete_task_in(dbt, profile_task.id, output={
                    "columns_profiled": len(prof.get("columns", [])),
                    "anomalies_found":  len(anomalies),
                    "anomalies":        anomalies[:20],
                    "row_count":        prof.get("table", {}).get("row_count"),
                    "sampled":          prof.get("table", {}).get("is_sampled", False),
                })
            except Exception as e:
                profile_result["error"] = str(e)
                self._fail_task_in(dbt, profile_task.id, str(e))
            finally:
                dbt.close()

        t1 = threading.Thread(target=_run_metadata,    daemon=True)
        t2 = threading.Thread(target=_run_rules_fetch, daemon=True)
        t3 = threading.Thread(target=_run_profiling,   daemon=True)
        t1.start(); t2.start(); t3.start()
        t1.join();  t2.join();  t3.join()

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
            intel_result = agent.run(
                table_asset, column_assets, existing_rules, run.id,
                profile=profile_result.get("profile"),
            )

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
                "parse_warning":         classification.get("parse_warning"),
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
                "severity":         decision.get("severity_override") or (
                    rule.severity.value if hasattr(rule.severity, "value") else str(rule.severity)
                ),
                "original_severity": rule.severity.value if hasattr(rule.severity, "value") else str(rule.severity),
                "reason":           decision.get("reason", ""),
                "is_ai_generated":  False,
                "category":         rule.category.value if hasattr(rule.category, "value") else str(rule.category),
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
                "severity":         ai_rule.severity.value if hasattr(ai_rule.severity, "value") else str(ai_rule.severity),
                "original_severity": ai_rule.severity.value if hasattr(ai_rule.severity, "value") else str(ai_rule.severity),
                "reason":           "AI-generated rule for this table type",
                "is_ai_generated":  True,
                "category":         ai_rule.category.value if hasattr(ai_rule.category, "value") else str(ai_rule.category),
                "applies_to":       ai_rule.applies_to or [],
                "violated":         violated,
                "ai_violation_evidence": next(
                    (v.get("evidence", {}).get("ai_evidence", "") for v in ai_violations
                     if v.get("context", {}).get("rule_code") == ai_rule.code),
                    ""
                ),
            })

        run.rule_review_state = {"active": active_rules, "skipped": skipped_rules}
        run.status = AgentRunStatus.AWAITING_RULE_REVIEW
        db.commit()
        logger.info(
            f"[Coordinator] Run {self.run_id} paused for rule review — "
            f"{len(active_rules)} active, {len(skipped_rules)} skipped."
        )

        # Rules are ready — advance the batch now so the next table computes its
        # rules while the user reviews this one at their own pace.
        self._advance_batch(db)

    # ── Phase 2: triggered by POST /runs/{id}/run-pipeline ───────────────────

    def run_pipeline_after_review(self, snowflake_user: str = "data-governance-team") -> None:
        """Run FindingsAgent using the user-approved rule set from rule_review_state."""
        db = SessionLocal()
        try:
            self._run_findings(db, snowflake_user)
        except Exception as e:
            logger.error(f"[Coordinator] run_pipeline error in run {self.run_id}: {e}")
            db2 = SessionLocal()
            try:
                self._mark_run_failed(db2, str(e), advance=False)  # already advanced at review
            finally:
                db2.close()
        finally:
            db.close()

    def _run_findings(self, db: Session, snowflake_user: str) -> None:
        run = db.query(AgentRun).filter(AgentRun.id == self.run_id).first()
        if not run:
            raise ValueError(f"AgentRun {self.run_id} not found")

        run.status = AgentRunStatus.RUNNING
        db.commit()

        review_state = run.rule_review_state or {"active": [], "skipped": []}
        active_entries = review_state.get("active", [])

        # ── Persist approved AI rules to the permanent rules library ─────────
        from app.models.rule import Rule, RuleStatus, RuleCategory, RuleSeverity
        ai_rules_persisted = []
        for entry in active_entries:
            if not entry.get("is_ai_generated"):
                continue
            code = entry["code"]
            existing = db.query(Rule).filter(Rule.code == code).first()
            if existing:
                # Update name/description/severity with user's edits
                existing.name        = entry["name"]
                existing.description = entry["description"]
                try:
                    existing.severity = RuleSeverity(entry["severity"])
                except ValueError:
                    pass
                existing.is_active = True
                existing.status    = RuleStatus.ACTIVE
                existing.owner     = snowflake_user
                db.flush()
                ai_rules_persisted.append(existing)
            else:
                # New AI rule — create permanently
                try:
                    category = RuleCategory(entry.get("category", "data_quality"))
                except ValueError:
                    category = RuleCategory.DATA_QUALITY
                try:
                    severity = RuleSeverity(entry["severity"])
                except ValueError:
                    severity = RuleSeverity.MEDIUM

                new_rule = Rule(
                    code=code,
                    name=entry["name"],
                    description=entry["description"],
                    category=category,
                    severity=severity,
                    applies_to=entry.get("applies_to") or ["table"],
                    rule_config={"source_run_id": run.id, "ai_generated": True},
                    is_active=True,
                    status=RuleStatus.ACTIVE,
                    owner=snowflake_user,
                    created_by="rule_intelligence_agent",
                    version=1,
                )
                db.add(new_rule)
                db.flush()
                ai_rules_persisted.append(new_rule)

        db.commit()
        run.ai_rules_count = len(ai_rules_persisted)
        db.commit()

        # ── Re-fetch assets in this session ──────────────────────────────────
        from app.models.asset import Asset
        from app.models.scan import Scan

        scan = db.query(Scan).filter(Scan.id == run.scan_id).first()
        if not scan:
            raise ValueError(f"Scan {run.scan_id} not found")

        table_asset = db.query(Asset).filter(
            Asset.database_name == run.database,
            Asset.schema_name   == run.schema_name,
            Asset.table_name    == run.table,
            Asset.asset_type    == "table",
        ).first()
        if not table_asset:
            raise ValueError(f"Table asset not found for {run.database}.{run.schema_name}.{run.table}")

        column_assets = db.query(Asset).filter(
            Asset.database_name == run.database,
            Asset.schema_name   == run.schema_name,
            Asset.table_name    == run.table,
            Asset.asset_type    == "column",
        ).all()

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
                rule_obj = db.query(Rule).filter(Rule.code == entry["code"]).first()
                if rule_obj and table_asset:
                    col_name = ""
                    if "column" in (entry.get("applies_to") or []):
                        col_name = entry.get("column_name", "")
                    asset = table_asset
                    if col_name:
                        col_fqn = f"{table_asset.fqn}.{col_name}"
                        asset = db.query(Asset).filter(Asset.fqn == col_fqn).first() or table_asset

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
        from app.models.finding import FindingStatus
        findings_task = self._get_task(db, "findings_agent")
        self._start_task(db, findings_task)
        try:
            from app.services.agents.findings_agent import FindingsAgent
            from app.services.agents.rule_intelligence_agent import RuleIntelligenceAgent

            _ria = RuleIntelligenceAgent(db)
            findings = FindingsAgent(db).run(
                scan, table_asset, column_assets,
                classification, ai_violations, _ria,
                allowed_codes=approved_codes,
            )
            run.findings_count = len(findings)
            db.commit()

            sev_breakdown = {}
            fired_codes = set()  # rules that produced at least one finding
            for f in findings:
                sev_breakdown[f.severity] = sev_breakdown.get(f.severity, 0) + 1
                code = (f.context or {}).get("rule_code")
                if code:
                    fired_codes.add(code)

            # Split the executed (approved) rule set into "used" (produced a finding)
            # vs "unused" (ran clean) — mirrors the active/skipped panel from Rule
            # Intelligence, so the user can see what fired and what came back clean.
            name_by_code = {e["code"]: e.get("name", e["code"]) for e in active_entries}
            findings_by_code = {}
            for f in findings:
                c = (f.context or {}).get("rule_code")
                if c:
                    findings_by_code[c] = findings_by_code.get(c, 0) + 1

            rules_used = [
                {"code": c, "name": name_by_code.get(c, c), "findings": findings_by_code.get(c, 0)}
                for c in sorted(approved_codes) if c in fired_codes
            ]
            rules_unused = [
                {"code": c, "name": name_by_code.get(c, c)}
                for c in sorted(approved_codes) if c not in fired_codes
            ]

            self._complete_task(db, findings_task, output={
                "findings_count":     len(findings),
                "ai_rule_findings":   len(ai_violations),
                "severity_breakdown": sev_breakdown,
                "scan_id":            scan.id,
                "ai_rules_persisted": len(ai_rules_persisted),
                "rules_executed":     len(approved_codes),
                "rules_used_count":   len(rules_used),
                "rules_unused_count": len(rules_unused),
                "rules_used":         rules_used,
                "rules_unused":       rules_unused,
            })
        except Exception as e:
            self._fail_task(db, findings_task, str(e))
            self._skip_tasks(db, ["verification_agent"])
            self._mark_run_failed(db, str(e), advance=False)  # already advanced at review
            return

        run.status = AgentRunStatus.AWAITING_FIXES
        db.commit()
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

    def _mark_run_failed(self, db: Session, error: str, advance: bool = True) -> None:
        run = db.query(AgentRun).filter(AgentRun.id == self.run_id).first()
        if run:
            run.status = AgentRunStatus.FAILED
            run.completed_at = datetime.utcnow()
            run.error_message = error[:1024]
            db.commit()
            # Don't let one bad table stall the rest of the batch. Only advance for
            # pre-review failures — post-review failures already advanced at the
            # rule-review point, so advancing again would skip a table.
            if advance:
                self._advance_batch(db)

    def _advance_batch(self, db: Session) -> None:
        """
        Sequential batch processing: once this run has reached rule review /
        awaiting fixes / failed, kick off the next pending run in the same batch.
        No-op for single-table runs (batch_id is None or only one member).
        """
        run = db.query(AgentRun).filter(AgentRun.id == self.run_id).first()
        if not run or not run.batch_id:
            return

        next_run = (
            db.query(AgentRun)
            .filter(
                AgentRun.batch_id == run.batch_id,
                AgentRun.status == AgentRunStatus.PENDING,
            )
            .order_by(AgentRun.batch_index.asc())
            .first()
        )
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
