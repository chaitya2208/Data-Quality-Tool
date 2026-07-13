"""
WorkflowCoordinator — pipeline:

  Coordinator
      ↓
  [Metadata Agent ∥ Rules Fetch Agent ∥ Relationship Discovery Agent]   ← parallel threads
      ↓
  Profiler Agent                         ← deterministic stats (needs column_assets)
      ↓
  Rule Intelligence Agent                ← deterministic candidates + Claude proposals
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
    "relationship_discovery_agent",
    "profiling_agent",
    "profiler_agent",
    "rule_intelligence_agent",
    "findings_agent",
    "fix_issues",          # UI-only node
    "verification_agent",
]

DB_AGENT_ORDER = [
    "coordinator",
    "metadata_agent",
    "rules_fetch_agent",
    "relationship_discovery_agent",
    "profiling_agent",
    "profiler_agent",
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
            "metadata_agent", "rules_fetch_agent", "relationship_discovery_agent", "profiler_agent",
            "rule_intelligence_agent", "findings_agent", "verification_agent",
        ]

        # ── Coordinator: validate target ──────────────────────────────────────
        coord_task = self._get_task("coordinator")
        self._start_task(coord_task)
        try:
            from app.services.datasources import get_source
            source = get_source(run.connection_id)
            info = source.table_info(run.database, run.schema_name, run.table)
            tables = source.list_tables(run.database, run.schema_name)
            exists = any((t.get("name") or "").upper() == run.table.upper() for t in tables)
            if not exists:
                raise ValueError(
                    f"Table {run.database}.{run.schema_name}.{run.table} not found in the data source"
                )
            self._complete_task(coord_task, output={
                "target":    f"{run.database}.{run.schema_name}.{run.table}",
                "row_count": info.get("row_count"),
            })
        except Exception as e:
            self._fail_task(coord_task, str(e))
            self._skip_tasks(all_downstream)
            self._mark_run_failed(str(e))
            return

        # ── Parallel: Metadata ∥ Rules Fetch ∥ Relationship Discovery ∥ Profiling ──
        # Threads store only IDs / plain dicts — never DB row objects. The shared
        # Snowflake session is thread-safe (serialized), so no per-thread session.
        meta_task    = self._get_task("metadata_agent")
        rules_task   = self._get_task("rules_fetch_agent")
        reldisc_task = self._get_task("relationship_discovery_agent")
        profile_task = self._get_task("profiling_agent")
        self._start_task(meta_task)
        self._start_task(rules_task)
        self._start_task(reldisc_task)
        if profile_task:
            self._start_task(profile_task)

        meta_result    = {"scan_id": None, "table_asset_id": None, "column_ids": None, "error": None}
        rules_result   = {"rule_codes": None, "error": None}
        reldisc_result = {"catalog": None, "error": None}
        profile_result = {"profile": None, "error": None}

        def _run_metadata():
            try:
                from app.services.agents.metadata_agent import MetadataAgent
                scan, table_asset, column_assets = MetadataAgent().run(
                    run.database, run.schema_name, run.table, run.connection_id
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
                definitions = RulesFetchAgent().run()
                rules_result["rule_codes"] = [d.id for d in definitions]
                self._complete_task(rules_task, output={
                    "definitions_loaded": len(definitions),
                    "definition_ids": rules_result["rule_codes"],
                })
            except Exception as e:
                rules_result["error"] = str(e)
                self._fail_task(rules_task, str(e))

        def _run_relationship_discovery():
            # Only needs database/schema — known before MetadataAgent even
            # runs — so this belongs in the parallel group, not sequential.
            # Cached per (database, schema) with a 24h TTL, so a schema-scope
            # batch only pays discovery cost on its first table.
            try:
                from app.services.relationship_discovery import get_or_refresh_catalog
                catalog = get_or_refresh_catalog(run.database, run.schema_name)
                reldisc_result["catalog"] = catalog
                self._complete_task(reldisc_task, output={
                    "relationships_found": len(catalog),
                    "confirmed": len([r for r in catalog if r.status == "confirmed"]),
                })
            except Exception as e:
                reldisc_result["error"] = str(e)
                self._fail_task(reldisc_task, str(e))

        def _run_profiling():
            # UI-facing profiling (anomaly surfacing / Data Explorer) — best-
            # effort, and NOT consumed by RuleIntelligenceAgent (that agent
            # gets its own deterministic column stats from ProfilerAgent,
            # run sequentially below since it needs column_assets).
            if not profile_task:
                return
            try:
                from app.services.agents.profiling_agent import ProfilingAgent
                prof = ProfilingAgent(None).run(run.database, run.schema_name, run.table, run.connection_id)
                profile_result["profile"] = prof
                anomalies = prof.get("anomalies", [])
                self._complete_task(profile_task, output={
                    "columns_profiled": len(prof.get("columns", [])),
                    "anomalies_found":  len(anomalies),
                    "anomalies":        anomalies[:20],
                    "row_count":        prof.get("table", {}).get("row_count"),
                    "sampled":          prof.get("table", {}).get("is_sampled", False),
                })
            except Exception as e:
                profile_result["error"] = str(e)
                self._fail_task(profile_task, str(e))

        t1 = threading.Thread(target=_run_metadata,               daemon=True)
        t2 = threading.Thread(target=_run_rules_fetch,             daemon=True)
        t3 = threading.Thread(target=_run_relationship_discovery,  daemon=True)
        t4 = threading.Thread(target=_run_profiling,               daemon=True)
        t1.start(); t2.start(); t3.start(); t4.start()
        t1.join();  t2.join();  t3.join();  t4.join()

        run = storage.get_agent_run(self.run_id)

        if meta_result["error"]:
            self._skip_tasks(["profiler_agent", "rule_intelligence_agent", "findings_agent", "verification_agent"])
            self._mark_run_failed(meta_result["error"])
            return

        scan          = storage.get_scan(meta_result["scan_id"])
        table_asset   = storage.get_asset(meta_result["table_asset_id"])
        column_assets = [storage.get_asset(cid) for cid in meta_result["column_ids"]]

        # Re-fetch the definition library (or load all active if fetch failed)
        if rules_result["rule_codes"] is not None:
            existing_definitions = [storage.get_definition(d_id) for d_id in rules_result["rule_codes"]]
            existing_definitions = [d for d in existing_definitions if d]
        else:
            if rules_result["error"]:
                logger.warning(f"[Coordinator] Rules fetch failed: {rules_result['error']}, loading all active definitions")
            _, existing_definitions = storage.list_definitions(status="active", limit=1000)

        if reldisc_result["error"]:
            logger.warning(f"[Coordinator] Relationship discovery failed: {reldisc_result['error']}, continuing with no catalog")
        relationship_catalog = reldisc_result["catalog"] or []

        # ── Profiler Agent — needs column_assets, so runs after the join ──────
        profiler_task = self._get_task("profiler_agent")
        self._start_task(profiler_task)
        try:
            from app.services.agents.profiler_agent import DeterministicProfilerAgent
            profiler_result = DeterministicProfilerAgent().run(table_asset, column_assets)
            self._complete_task(profiler_task, output={
                "columns_profiled": len(profiler_result.get("column_stats", {})),
                "pk_shaped_candidates": len(profiler_result.get("pk_shaped_candidates", [])),
                "freshness_signals": len(profiler_result.get("freshness_signals", [])),
                "closed_set_columns": len(profiler_result.get("closed_set_columns", {})),
            })
        except Exception as e:
            logger.error(f"[Coordinator] ProfilerAgent failed: {e}")
            self._fail_task(profiler_task, str(e))
            profiler_result = {}

        # ── Rule Intelligence Agent ───────────────────────────────────────────
        intel_task = self._get_task("rule_intelligence_agent")
        self._start_task(intel_task)
        intel_result = None
        try:
            from app.services.agents.rule_intelligence_agent import RuleIntelligenceAgent
            agent = RuleIntelligenceAgent()
            intel_result = agent.run(
                table_asset, column_assets, existing_definitions, run.id,
                profiler_result=profiler_result, relationship_catalog=relationship_catalog,
            )

            classification       = intel_result["classification"]
            proposed_instances   = intel_result["proposed_instances"]
            suppressed           = intel_result["suppressed"]

            self._complete_task(intel_task, output={
                "table_type":            classification["table_type"],
                "table_type_confidence": classification["table_type_confidence"],
                "table_type_reason":     classification["table_type_reason"],
                "existing_instances_evaluated": len(intel_result["existing_instances"]),
                "new_instances_proposed": len(proposed_instances),
                "deterministic_proposals": len([p for p in proposed_instances if p.get("source") == "deterministic"]),
                "signals_missed": intel_result.get("signals_missed", []),
                "parse_failed": intel_result.get("parse_failed", False),
                "suppressed_duplicates": [
                    {"reason": s["reason"], "fingerprint": s["fingerprint"][:12]}
                    for s in suppressed
                ],
            })
        except Exception as e:
            logger.error(f"[Coordinator] RuleIntelligenceAgent failed: {e}")
            self._fail_task(intel_task, str(e))
            intel_result = {
                "classification": {"table_type": "unknown", "definitions_evaluated": {}},
                "existing_instances": [],
                "proposed_instances": [],
                "suppressed": [],
            }

        # ── PAUSE: persist proposals as PENDING, build instance_review_state ────
        classification      = intel_result["classification"]
        existing_instances   = intel_result["existing_instances"]
        proposed_instances   = intel_result["proposed_instances"]

        active_entries = []
        skipped_entries = []

        # Existing instances — keep_running decision only, never re-approved
        for inst in existing_instances:
            decision = classification.get("definitions_evaluated", {}).get(inst.definition_id, {})
            definition = storage.get_definition(inst.definition_id)
            entry = {
                "instance_id":       inst.id,
                "definition_id":     inst.definition_id,
                "name":              definition.name if definition else inst.definition_id,
                "description":       definition.description if definition else "",
                "severity":          decision.get("severity_override") or inst.severity,
                "original_severity": inst.severity,
                "reason":            decision.get("reason", ""),
                "is_new_instance":   False,
                "is_new_definition": False,
                "source":            "existing",
                "scope":             inst.scope,
                "target_config":     inst.target_config,
            }
            if decision.get("keep_running", True):
                active_entries.append(entry)
            else:
                skipped_entries.append(entry)

        # Newly proposed instances — persist as PENDING (never auto-active),
        # new definitions persist as PROPOSED (never auto-active either).
        # This is the explicit approval gate: nothing here runs until a
        # human approves it via review-rules + run-pipeline. "deterministic"
        # proposals (uniqueness on a PK-shaped column, a confirmed cross-
        # table orphan relationship) go through this exact same gate —
        # bypassing the LLM for the objective fact never bypasses human
        # review of whether to activate the check.
        for proposal in proposed_instances:
            if proposal["kind"] == "new":
                nd = proposal["new_definition_data"] or {}
                category = nd.get("category") if nd.get("category") in {
                    "naming", "documentation", "ownership", "schema", "data_quality", "security", "performance"
                } else "data_quality"
                # rule_sql is always set for a "new" proposal (_process_candidate
                # discards any candidate that doesn't resolve to validated,
                # executable SQL) — check_kind is always sql_template here,
                # never python_handler, since we have no Python function to
                # bind to a Claude-authored concept. template_shape is set
                # whenever the candidate named one of the 9 known shapes (all
                # pre-seeded as canonical, see 06_seed_canonical_shape_
                # definitions.sql) so a shape not yet seeded still becomes
                # canonical from this point forward, closing the definition-
                # library-explosion loop for future shapes too.
                definition = storage.create_definition(
                    name=nd.get("name", "Untitled Check"),
                    category=category,
                    description=nd.get("description", ""),
                    check_kind="sql_template",
                    sql_template=proposal["rule_sql"],
                    template_shape=proposal.get("template_shape"),
                    default_threshold_config=proposal["threshold_config"],
                    default_severity=proposal["severity"],
                    allowed_scopes=[proposal["scope"]],
                    source="claude",
                    status="proposed",
                    owner="rule_intelligence_agent",
                    created_by="rule_intelligence_agent",
                )
            else:
                definition = proposal["definition"]

            instance = storage.create_instance(
                definition_id=definition.id,
                scope=proposal["scope"],
                database_name=table_asset.database_name,
                schema_name=table_asset.schema_name,
                table_name=table_asset.table_name,
                fingerprint=proposal["fingerprint"],
                severity=proposal["severity"],
                target_config=proposal["target_config"],
                threshold_config=proposal["threshold_config"],
                rule_sql=proposal["rule_sql"],
                rationale=proposal["rationale"],
                status="pending",
                is_active=False,
                owner="rule_intelligence_agent",
                created_by="rule_intelligence_agent",
                source_run_id=run.id,
            )

            active_entries.append({
                "instance_id":       instance.id,
                "definition_id":     definition.id,
                "name":              definition.name,
                "description":       definition.description,
                "severity":          proposal["severity"],
                "original_severity": proposal["severity"],
                "reason":            proposal["rationale"],
                "is_new_instance":   True,
                "is_new_definition": proposal["kind"] == "new",
                "source":            proposal.get("source", "llm"),
                "scope":             proposal["scope"],
                "target_config":     proposal["target_config"],
                "violated":          proposal["violated"],
                "violation_evidence": proposal["evidence"],
            })

        # signals_missed = deterministic signals Claude never addressed at all.
        # Freshness is the exposed flank: it has no deterministic backstop (a
        # staleness threshold is a judgment call, so it's never auto-proposed),
        # so an omitted freshness signal means NO freshness check is proposed
        # and, without this, nobody would know. Surface it into the review
        # state so the reviewer sees "N signals unaddressed" instead of a clean
        # screen that hides the gap.
        signals_missed = intel_result.get("signals_missed", [])
        storage.update_agent_run(
            self.run_id,
            ai_rules_count=len([p for p in proposed_instances if p["kind"] == "new"]),
            instance_review_state={
                "active": active_entries,
                "skipped": skipped_entries,
                "signals_missed": signals_missed,
                # True when the model's response couldn't be parsed even after a
                # retry — the reviewer should treat "0 proposals" with suspicion
                # rather than as confirmation the table is fully covered.
                "parse_failed": intel_result.get("parse_failed", False),
            },
            status="awaiting_rule_review",
        )
        logger.info(
            f"[Coordinator] Run {self.run_id} paused for rule review — "
            f"{len(active_entries)} active, {len(skipped_entries)} skipped."
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

        review_state = run.instance_review_state or {"active": [], "skipped": []}
        active_entries = review_state.get("active", [])
        skipped_entries = review_state.get("skipped", [])

        # ── Approve reviewed instances (explicit gate — this is the only ────
        # place a pending instance becomes active). Newly-approved definitions
        # flip PROPOSED -> ACTIVE on their first approval.
        new_instances_approved = 0
        for entry in active_entries:
            if not entry.get("is_new_instance"):
                continue
            instance = storage.get_instance(entry["instance_id"])
            if not instance:
                continue
            storage.update_instance(
                instance.id,
                severity=entry.get("severity", instance.severity),
                owner=snowflake_user,
            )
            storage.approve_instance(instance.id)
            definition = storage.get_definition(instance.definition_id)
            if definition and definition.status == "proposed":
                storage.update_definition(definition.id, status="active")
            new_instances_approved += 1

        # Explicitly rejected instances (skipped by the human) get marked so
        # the fingerprint-rejection path suppresses them on future scans.
        for entry in skipped_entries:
            if not entry.get("is_new_instance"):
                continue
            instance = storage.get_instance(entry["instance_id"])
            if instance and instance.status == "pending":
                storage.reject_instance(instance.id, reason=entry.get("reason") or "Skipped at review")

        storage.update_agent_run(self.run_id, ai_rules_count=new_instances_approved)

        # ── Re-fetch assets ───────────────────────────────────────────────────
        scan = storage.get_scan(run.scan_id)
        if not scan:
            raise ValueError(f"Scan {run.scan_id} not found")

        table_asset = storage.get_table_asset(run.database, run.schema_name, run.table)
        if not table_asset:
            raise ValueError(f"Table asset not found for {run.database}.{run.schema_name}.{run.table}")

        column_assets = storage.list_column_assets(run.database, run.schema_name, run.table)

        # ── Build approved instance set for FindingsAgent ────────────────────
        # Existing (non-new) instances carry their instance_id directly.
        # Newly-approved instances are now active with the same instance_id.
        approved_instance_ids = {e["instance_id"] for e in active_entries}

        # Severity overrides for existing instances (human/Claude changed the
        # severity from what's currently stored)
        severity_overrides = {
            e["instance_id"]: e["severity"]
            for e in active_entries
            if not e.get("is_new_instance") and e["severity"] != e.get("original_severity")
        }

        # ── Findings Agent ────────────────────────────────────────────────────
        # Newly-approved instances (Claude-authored, sql_template) run through
        # RuleEngine exactly like every other instance — they have real,
        # validated SQL (see rule_intelligence_agent.py), so there is no
        # separate "one-time AI violation" path anymore.
        findings_task = self._get_task("findings_agent")
        self._start_task(findings_task)
        try:
            from app.services.agents.findings_agent import FindingsAgent

            findings = FindingsAgent().run(
                scan, table_asset, column_assets,
                allowed_instance_ids=approved_instance_ids,
                severity_overrides=severity_overrides,
                run_id=run.id,
            )
            storage.update_agent_run(self.run_id, findings_count=len(findings))

            sev_breakdown = {}
            fired_instance_ids = set()  # instances that produced at least one finding
            findings_by_instance = {}
            for f in findings:
                sev_breakdown[f.severity] = sev_breakdown.get(f.severity, 0) + 1
                iid = f.instance_id
                if iid:
                    fired_instance_ids.add(iid)
                    findings_by_instance[iid] = findings_by_instance.get(iid, 0) + 1

            # Split the executed (approved) instance set into "used" (produced a
            # finding) vs "unused" (ran clean) — mirrors the active/skipped panel
            # from Rule Intelligence, so the user sees what fired and what came
            # back clean. Keyed by instance_id (harsh's rule-instance model).
            name_by_instance = {e["instance_id"]: e.get("name", e["instance_id"]) for e in active_entries}
            rules_used = [
                {"instance_id": iid, "name": name_by_instance.get(iid, iid),
                 "findings": findings_by_instance.get(iid, 0)}
                for iid in approved_instance_ids if iid in fired_instance_ids
            ]
            rules_unused = [
                {"instance_id": iid, "name": name_by_instance.get(iid, iid)}
                for iid in approved_instance_ids if iid not in fired_instance_ids
            ]

            self._complete_task(findings_task, output={
                "findings_count":     len(findings),
                "severity_breakdown": sev_breakdown,
                "scan_id":            scan.id,
                "new_instances_approved": new_instances_approved,
                "rules_executed":     len(approved_instance_ids),
                "rules_used_count":   len(rules_used),
                "rules_unused_count": len(rules_unused),
                "rules_used":         rules_used,
                "rules_unused":       rules_unused,
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
