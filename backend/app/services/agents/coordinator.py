"""
WorkflowCoordinator — pipeline:

  Coordinator
      ↓
  [Metadata Agent ∥ Rules Fetch Agent ∥ Relationship Discovery Agent ∥ Profiling Agent]   ← parallel threads
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
from types import SimpleNamespace
from typing import List, Any, Dict

from app.services import storage

logger = logging.getLogger(__name__)

AGENT_ORDER = [
    "coordinator",
    "metadata_agent",
    "rules_fetch_agent",
    "relationship_discovery_agent",
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
    "relationship_discovery_agent",
    "profiling_agent",
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
            # Structured error handlers inside _execute already own state
            # transitions for every anticipated failure — a bare fail() here is
            # for the truly unexpected (bug in the coordinator itself). Log the
            # full traceback so it's actually debuggable, and only mark failed
            # if the run isn't already in a terminal state, else this
            # double-invokes _advance_batch and can skip the next table.
            import traceback
            logger.error(
                f"[Coordinator] Unhandled error in run {self.run_id}: "
                f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
            )
            run = storage.get_agent_run(self.run_id)
            if run and run.status not in ("completed", "failed", "awaiting_fixes", "awaiting_rule_review"):
                self._mark_run_failed(f"Unhandled coordinator error: {type(e).__name__}: {e}")

    def _execute(self) -> None:
        run = storage.get_agent_run(self.run_id)
        if not run:
            raise ValueError(f"AgentRun {self.run_id} not found")

        if run.workflow_template_id:
            self._execute_with_template(run)
            return

        storage.update_agent_run(self.run_id, status="running", started_at=datetime.utcnow())

        all_downstream = [
            "metadata_agent", "rules_fetch_agent", "relationship_discovery_agent",
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
        self._start_task(profile_task)

        meta_result    = {"scan_id": None, "table_asset_id": None, "column_ids": None, "error": None}
        rules_result   = {"rule_codes": None, "error": None}
        reldisc_result = {"catalog": None, "error": None}
        # profiling now yields BOTH the UI profile and the deterministic facts
        # (column_stats/pk_shaped_candidates/freshness_signals/closed_set_columns)
        # consumed by RuleIntelligenceAgent — computed in parallel, no longer a
        # separate sequential Profiler step.
        profile_result = {"profile": None, "facts": {}, "error": None}

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
            # One profiling pass produces BOTH the UI profile (anomaly surfacing
            # / Data Explorer) AND the deterministic facts consumed by
            # RuleIntelligenceAgent (column_stats / pk_shaped_candidates /
            # freshness_signals / closed_set_columns). Runs in parallel — the
            # facts are derived from (db, schema, table, connection_id), so this
            # no longer waits for column metadata to be persisted.
            try:
                from app.services.agents.profiling_agent import ProfilingAgent
                prof = ProfilingAgent(None).run(run.database, run.schema_name, run.table, run.connection_id)
                profile_result["profile"] = prof
                # Deterministic facts for RuleIntelligenceAgent (the 4 keys it reads).
                profile_result["facts"] = {
                    "column_stats":         prof.get("column_stats", {}),
                    "pk_shaped_candidates": prof.get("pk_shaped_candidates", []),
                    "freshness_signals":    prof.get("freshness_signals", []),
                    "closed_set_columns":   prof.get("closed_set_columns", {}),
                }
                anomalies = prof.get("anomalies", [])
                self._complete_task(profile_task, output={
                    "columns_profiled":     len(prof.get("columns", [])),
                    "anomalies_found":      len(anomalies),
                    "anomalies":            anomalies[:20],
                    "pk_shaped_candidates": len(profile_result["facts"]["pk_shaped_candidates"]),
                    "freshness_signals":    len(profile_result["facts"]["freshness_signals"]),
                    "closed_set_columns":   len(profile_result["facts"]["closed_set_columns"]),
                    "row_count":            prof.get("table", {}).get("row_count"),
                    "sampled":              prof.get("table", {}).get("is_sampled", False),
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
            self._skip_tasks(["rule_intelligence_agent", "findings_agent", "verification_agent"])
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

        # Deterministic facts came from the parallel profiling agent (above) —
        # no separate sequential profiler step. Empty dict if profiling failed
        # (best-effort: RuleIntelligenceAgent handles a missing profiler_result).
        if profile_result["error"]:
            logger.warning(f"[Coordinator] Profiling failed: {profile_result['error']}, continuing with no deterministic facts")
        profiler_result = profile_result["facts"] or {}

        # ── Sweep stale pending proposals from prior runs ─────────────────────
        # A pending instance from an earlier run that was never approved
        # clutters this scan's review UI and confuses Claude (he sees the
        # same target proposed again as "already pending" but hasn't seen the
        # user reject it). Reject them with a clear reason so fingerprint
        # dedup still catches a genuine re-proposal, but they no longer appear
        # in the new review as unaddressed backlog.
        stale_pending = storage.list_stale_pending_instances(
            database_name=run.database,
            schema_name=run.schema_name,
            table_name=run.table,
            except_run_id=run.id,
        )
        for inst in stale_pending:
            storage.update_instance(
                inst.id,
                status="rejected",
                rejection_reason="Superseded by a new scan before the prior review was completed.",
            )
        if stale_pending:
            logger.info(
                f"[Coordinator] Swept {len(stale_pending)} stale pending proposal(s) "
                f"from prior runs on {run.database}.{run.schema_name}.{run.table}"
            )

        # ── Rule Intelligence Agent ───────────────────────────────────────────
        intel_task = self._get_task("rule_intelligence_agent")
        self._start_task(intel_task)
        intel_result = None
        try:
            from app.services.agents.rule_intelligence_agent import RuleIntelligenceAgent
            from app.services.datasources import get_source
            # Pass the run's own source so proposal-time SQL execution and the
            # get_sample_rows tool run against the analysed database, not the
            # app's shared Snowflake session (audit finding #4).
            source = None
            try:
                source = get_source(run.connection_id)
            except Exception as _src_err:
                logger.warning(f"[Coordinator] Could not resolve source for rule_intelligence: {_src_err}")
            agent = RuleIntelligenceAgent()
            intel_result = agent.run(
                table_asset, column_assets, existing_definitions, run.id,
                profiler_result=profiler_result, relationship_catalog=relationship_catalog,
                source=source,
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
                "sample_tool_calls": len(intel_result.get("tool_calls", [])),
                "sample_reasons": [
                    (tc.get("input") or {}).get("reason", "")
                    for tc in intel_result.get("tool_calls", [])
                ],
            })
        except Exception as e:
            # Rule Intelligence is a core node — if it throws, the run has no
            # rules to review and must NOT quietly proceed to awaiting_fixes with
            # zero proposals (that reads as a clean run in Run History). Fail the
            # whole run with this node's error + time, skip the remaining stages,
            # and let the batch advance to the next table.
            import traceback
            logger.error(
                f"[Coordinator] RuleIntelligenceAgent failed: {type(e).__name__}: {e}\n"
                f"{traceback.format_exc()}"
            )
            self._fail_task(intel_task, str(e))
            self._skip_tasks(["findings_agent", "verification_agent"])
            self._mark_run_failed(f"Rule Intelligence failed: {e}")
            return

        # ── PAUSE: persist proposals as PENDING, build instance_review_state ────
        classification      = intel_result["classification"]
        existing_instances   = intel_result["existing_instances"]
        proposed_instances   = intel_result["proposed_instances"]

        active_entries = []
        skipped_entries = []

        # Existing instances — keep_running decision only, never re-approved.
        # Decisions are keyed per-instance (instances_evaluated) so two
        # instances of the same definition (e.g. accepted_values on STATUS
        # AND on CURRENCY_CODE) get INDEPENDENT keep_running verdicts.
        # Falls back to the legacy definition-level key if the model still
        # returned that shape.
        instances_evaluated = classification.get("instances_evaluated") or {}
        legacy_defs_evaluated = classification.get("definitions_evaluated") or {}
        for inst in existing_instances:
            decision = instances_evaluated.get(inst.id)
            if decision is None:
                decision = legacy_defs_evaluated.get(inst.definition_id, {})
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
        #
        # Same-run dedup: Claude sometimes proposes the identical new concept
        # for several column pairs in ONE response (e.g. "Cross-Column
        # Numeric Ordering (Min <= Max)" for HIGH/LOW, OUTRIGHT_HIGH/LOW, and
        # PREMIUM_HIGH/LOW all at once). The agent's storage-based similarity
        # check can't catch this — none of these candidates are persisted yet
        # when the others are checked. Collapse by new_definition_key here so
        # only ONE definition row is created per run, and every matching
        # proposal's instance points at that shared definition.
        new_definitions_by_key: Dict[str, Any] = {}
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
                dedup_key = proposal.get("new_definition_key")
                definition = new_definitions_by_key.get(dedup_key) if dedup_key else None
                if definition is None:
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
                    if dedup_key:
                        new_definitions_by_key[dedup_key] = definition
                    logger.info(
                        f"[Coordinator] Created new definition '{definition.name}' "
                        f"(id={definition.id}) for run {self.run_id}"
                    )
                else:
                    logger.info(
                        f"[Coordinator] Reusing same-run definition '{definition.name}' "
                        f"(id={definition.id}) for another target instead of duplicating"
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
        ai_rules_proposed = len([p for p in proposed_instances if p["kind"] == "new"])

        # ── Unused library bucket ─────────────────────────────────────────────
        # Definitions the agent knew about but that ended up with NO instance
        # on this table (neither existing nor newly proposed). Surfaced so the
        # reviewer can manually activate one — without this bucket the ~20+
        # library definitions Claude ignored are invisible in the UI, and the
        # reviewer has no way to discover "we have an SLA breach detector, I
        # could turn that on for this table" short of clicking through the
        # rule library page. See instance_review_state schema.
        used_def_ids = {
            e["definition_id"] for e in active_entries + skipped_entries if e.get("definition_id")
        }
        unused_library = []
        for d in existing_definitions:
            if d.id in used_def_ids:
                continue
            unused_library.append({
                "definition_id":  d.id,
                "name":           d.name,
                "description":    d.description or "",
                "category":       getattr(d, "category", "data_quality"),
                "template_shape": getattr(d, "template_shape", None),
                "check_kind":     getattr(d, "check_kind", None),
                "default_severity": getattr(d, "default_severity", "medium"),
            })

        storage.update_agent_run(
            self.run_id,
            ai_rules_count=ai_rules_proposed,
            instance_review_state={
                "active": active_entries,
                "skipped": skipped_entries,
                "unused_library": unused_library,
                "signals_missed": signals_missed,
                "ai_rules_proposed": ai_rules_proposed,
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

        # Back-fill outcome counts on the intelligence log so the vector store
        # reflects whether Claude's proposals were accepted or rejected.
        # Each best-effort step below has its own try/except so a failure in
        # one doesn't get logged as a failure of another (audit finding #15).
        new_instances_rejected = sum(
            1 for e in skipped_entries if e.get("is_new_instance")
        )

        try:
            log = storage.get_intelligence_log_for_run(self.run_id)
            if log:
                storage.update_intelligence_log_outcomes(
                    log.id,
                    approved_count=new_instances_approved,
                    rejected_count=new_instances_rejected,
                )
        except Exception as e:
            logger.warning(f"[Coordinator] Could not update intelligence log outcomes: {e}")

        # Persist structured review lessons so future runs on similar tables
        # learn what humans accept or reject here.
        try:
            run_obj = storage.get_agent_run(self.run_id)
            review_state = (run_obj.instance_review_state or {}) if run_obj else {}
            lessons = self._build_review_lessons(
                active_entries=review_state.get("active", []),
                skipped_entries=review_state.get("skipped", []),
            )
            if lessons:
                storage.append_intelligence_log_lessons(
                    run_id=self.run_id,
                    table_fqn=f"{run.database}.{run.schema_name}.{run.table}",
                    lessons=lessons,
                )
        except Exception as e:
            logger.warning(f"[Coordinator] Could not append review lessons: {e}")

        # Synthesise accumulated lessons into a reusable memo — best-effort,
        # background thread so it never blocks the findings pipeline.
        try:
            table_type = (
                (storage.get_intelligence_log_for_run(self.run_id) or SimpleNamespace(table_type="unknown"))
                .table_type or "unknown"
            )
            _fqn = f"{run.database}.{run.schema_name}.{run.table}"
            threading.Thread(
                target=self._run_feedback_synthesis,
                args=(_fqn, table_type),
                daemon=True,
            ).start()
        except Exception as e:
            logger.warning(f"[Coordinator] Could not kick feedback synthesis: {e}")

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
            # len(findings) is CREATED-only under the new lifecycle
            # (list_findings_by_scan filters on SCAN_ID, which for UPDATE /
            # REOPEN branches still points at the original scan). Read the
            # authoritative count FindingsAgent just wrote onto SCANS instead.
            active_count = getattr(storage.get_scan(scan.id), "findings_count", None) or len(findings)
            storage.update_agent_run(self.run_id, findings_count=active_count)

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

        # Transition to awaiting_fixes immediately — before explanation agent so
        # the UI banner appears without waiting for Claude explanation calls.
        run = storage.update_agent_run(self.run_id, status="awaiting_fixes")
        logger.info(
            f"[Coordinator] Run {self.run_id} — pipeline complete. "
            f"{run.findings_count} findings, {run.ai_rules_count} AI rules persisted."
        )

        # ── Findings Explanation Agent (background) ───────────────────────────
        # Runs in a daemon thread so it never delays the awaiting_fixes status.
        # Best-effort — a failure here never affects the run.
        if findings:
            _findings_snap = list(findings)
            _table_snap = table_asset
            _run_id_snap = run.id
            _connection_id_snap = run.connection_id
            def _run_explanation():
                try:
                    from app.services.agents.findings_explanation_agent import FindingsExplanationAgent
                    FindingsExplanationAgent().run(
                        findings=_findings_snap,
                        table_asset=_table_snap,
                        run_id=_run_id_snap,
                        connection_id=_connection_id_snap,
                    )
                except Exception as _exp_err:
                    logger.warning(f"[Coordinator] FindingsExplanationAgent failed (non-fatal): {_exp_err}")
            threading.Thread(target=_run_explanation, daemon=True).start()

        # Start background auto-verification (every 5 min while awaiting fixes)
        from app.services.agents.auto_verify_scheduler import schedule as schedule_verify
        schedule_verify(run.id)
        # Note: batch already advanced when this run reached rule review — do not
        # advance again here, or a table could be skipped.

    # ── Template-based execution (saved workflow) ─────────────────────────────

    def _execute_with_template(self, run: Any) -> None:
        """
        Run a scan using a saved workflow template — no rule intelligence, no
        review pause. Each rule pattern is re-instantiated for the target table,
        skipping patterns whose target column doesn't exist on this table or
        whose scope is cross_table (those are specific to original relationships).
        Findings run immediately after instantiation.
        """
        storage.update_agent_run(self.run_id, status="running", started_at=datetime.utcnow())

        all_tasks = [
            "metadata_agent", "rules_fetch_agent", "relationship_discovery_agent",
            "profiling_agent", "rule_intelligence_agent", "findings_agent", "verification_agent",
        ]

        template = storage.get_workflow(run.workflow_template_id)
        if not template:
            self._skip_tasks(all_tasks)
            self._mark_run_failed(f"Workflow template {run.workflow_template_id} not found")
            return

        coord_task = self._get_task("coordinator")
        self._start_task(coord_task)
        try:
            from app.services.datasources import get_source
            source = get_source(run.connection_id)
            tables = source.list_tables(run.database, run.schema_name)
            exists = any((t.get("name") or "").upper() == run.table.upper() for t in tables)
            if not exists:
                raise ValueError(f"Table {run.database}.{run.schema_name}.{run.table} not found")
            self._complete_task(coord_task, output={"target": f"{run.database}.{run.schema_name}.{run.table}"})
        except Exception as e:
            self._fail_task(coord_task, str(e))
            self._skip_tasks(all_tasks)
            self._mark_run_failed(str(e))
            return

        # Metadata — need column list to filter patterns
        meta_task = self._get_task("metadata_agent")
        self._start_task(meta_task)
        try:
            from app.services.agents.metadata_agent import MetadataAgent
            scan, table_asset, column_assets = MetadataAgent().run(
                run.database, run.schema_name, run.table, run.connection_id
            )
            storage.update_agent_run(self.run_id, scan_id=scan.id)
            self._complete_task(meta_task, output={
                "scan_id": scan.id, "columns_found": len(column_assets),
            })
        except Exception as e:
            self._fail_task(meta_task, str(e))
            self._skip_tasks(["rules_fetch_agent", "relationship_discovery_agent",
                               "profiling_agent", "rule_intelligence_agent",
                               "findings_agent", "verification_agent"])
            self._mark_run_failed(str(e))
            return

        # Skip unused pipeline stages gracefully
        self._skip_tasks([
            "rules_fetch_agent", "relationship_discovery_agent",
            "profiling_agent", "rule_intelligence_agent",
        ])

        existing_column_names = {c.column_name.upper() for c in column_assets if c.column_name}

        # Re-instantiate each pattern for this target table
        from app.services.rule_sql_templates import render_template
        from app.services.sql_validation import validate_sql
        from app.services.fingerprint import compute_fingerprint

        approved_instance_ids = set()
        skipped_patterns = []

        for pattern in template.rule_patterns:
            scope = pattern.get("scope", "table")

            # Skip cross-table patterns — referential integrity rules are
            # specific to the original table's relationships
            if scope == "cross_table":
                skipped_patterns.append({**pattern, "skip_reason": "cross_table scope not portable"})
                continue

            # Skip column-scoped patterns if the column doesn't exist here
            target_config = pattern.get("target_config") or {}
            column = target_config.get("column", "")
            if scope == "column" and column and column.upper() not in existing_column_names:
                skipped_patterns.append({**pattern, "skip_reason": f"column {column} not found"})
                continue

            definition_id = pattern.get("definition_id")
            definition = storage.get_definition(definition_id) if definition_id else None
            if not definition:
                skipped_patterns.append({**pattern, "skip_reason": "definition not found"})
                continue

            threshold_config = pattern.get("threshold_config") or {}
            template_shape = pattern.get("template_shape") or definition.template_shape

            try:
                rule_sql = render_template(
                    template_shape,
                    table_asset.database_name, table_asset.schema_name, table_asset.table_name,
                    target_config, threshold_config,
                )
                result = validate_sql(rule_sql, allowed_tables=[
                    f"{table_asset.database_name}.{table_asset.schema_name}.{table_asset.table_name}".upper()
                ])
                if not result.is_valid:
                    skipped_patterns.append({**pattern, "skip_reason": f"SQL invalid: {result.errors}"})
                    continue
            except Exception as e:
                skipped_patterns.append({**pattern, "skip_reason": str(e)})
                continue

            fingerprint = compute_fingerprint(
                definition_id=definition_id,
                scope=scope,
                database_name=table_asset.database_name,
                schema_name=table_asset.schema_name,
                table_name=table_asset.table_name,
                target_config=target_config,
                threshold_config=threshold_config,
            )

            # Reuse existing active instance if fingerprint already exists
            existing = storage.get_instance_by_fingerprint(fingerprint)
            if existing and existing.status == "active":
                approved_instance_ids.add(existing.id)
                continue

            instance = storage.create_instance(
                definition_id=definition_id,
                scope=scope,
                database_name=table_asset.database_name,
                schema_name=table_asset.schema_name,
                table_name=table_asset.table_name,
                fingerprint=fingerprint,
                severity=pattern.get("severity", definition.default_severity or "medium"),
                target_config=target_config,
                threshold_config=threshold_config,
                rule_sql=rule_sql,
                rationale=f"Applied from workflow template: {template.label}",
                status="active",
                is_active=True,
                owner="workflow_template",
                created_by="workflow_template",
                source_run_id=run.id,
            )
            storage.approve_instance(instance.id)
            approved_instance_ids.add(instance.id)

        logger.info(
            f"[Coordinator] Template run — {len(approved_instance_ids)} patterns applied, "
            f"{len(skipped_patterns)} skipped on {table_asset.fqn}"
        )

        # Findings
        findings_task = self._get_task("findings_agent")
        self._start_task(findings_task)
        try:
            from app.services.agents.findings_agent import FindingsAgent
            findings = FindingsAgent().run(
                scan, table_asset, column_assets,
                allowed_instance_ids=approved_instance_ids,
                severity_overrides={},
                run_id=run.id,
            )
            # See sibling comment above: len(findings) is CREATED-only under
            # the incident lifecycle — use SCANS.findings_count (created +
            # reopened + updated) for the true active-incident count.
            active_count = getattr(storage.get_scan(scan.id), "findings_count", None) or len(findings)
            storage.update_agent_run(self.run_id, findings_count=active_count)
            self._complete_task(findings_task, output={
                "findings_count": active_count,
                "rules_applied": len(approved_instance_ids),
                "patterns_skipped": len(skipped_patterns),
                "skipped_reasons": skipped_patterns,
            })
        except Exception as e:
            self._fail_task(findings_task, str(e))
            self._skip_tasks(["verification_agent"])
            self._mark_run_failed(str(e), advance=False)
            return

        storage.update_agent_run(
            self.run_id, status="awaiting_fixes", completed_at=datetime.utcnow()
        )
        self._advance_batch()

        from app.services.agents.auto_verify_scheduler import schedule as schedule_verify
        schedule_verify(run.id)

    # ── Feedback synthesis (background) ──────────────────────────────────────

    @staticmethod
    def _run_feedback_synthesis(table_fqn: str, table_type: str) -> None:
        """Called in a daemon thread — synthesise accumulated lessons into a
        reusable memo. Any exception here is swallowed; this is best-effort."""
        try:
            from app.services.agents.feedback_synthesis_agent import FeedbackSynthesisAgent
            FeedbackSynthesisAgent().run(table_fqn=table_fqn, table_type=table_type)
        except Exception as e:
            logger.warning(f"[Coordinator] FeedbackSynthesisAgent failed (non-fatal): {e}")

    # ── Review lesson builder ─────────────────────────────────────────────────

    @staticmethod
    def _build_review_lessons(active_entries: list, skipped_entries: list) -> list:
        """Convert the human's approve/reject decisions into structured lesson
        dicts that future runs on similar tables can read as "lessons learned".

        Each lesson captures: verdict, check concept, column pattern, and the
        human's reason — enough for the prompt to say "last time a human
        rejected an accepted_values check on a STATUS column because the
        column is intentionally nullable for draft records."
        """
        lessons = []
        for entry in active_entries:
            if not entry.get("is_new_instance"):
                continue
            lessons.append({
                "verdict": "approved",
                "check_concept": entry.get("definition_id") or entry.get("name", ""),
                "column": entry.get("column_name") or (entry.get("target_config") or {}).get("column"),
                "severity": entry.get("severity"),
                "reason": entry.get("reason") or "Approved at review",
            })
        for entry in skipped_entries:
            if not entry.get("is_new_instance"):
                continue
            lessons.append({
                "verdict": "rejected",
                "check_concept": entry.get("definition_id") or entry.get("name", ""),
                "column": entry.get("column_name") or (entry.get("target_config") or {}).get("column"),
                "severity": entry.get("severity"),
                "reason": entry.get("reason") or "Skipped at review",
            })
        return lessons

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _get_task(self, name: str) -> Any:
        """Return the AGENT_TASKS row for this agent, or self-heal one into
        existence if AGENT_ORDER has drifted since this run was seeded.
        Missing rows used to silently no-op every subsequent transition (audit
        finding #3) — a whole agent could execute invisibly. Loud is better."""
        task = storage.get_agent_task(self.run_id, name)
        if task is None:
            logger.warning(
                f"[Coordinator] AGENT_TASKS row missing for run={self.run_id} agent={name} — "
                "self-healing (AGENT_ORDER drift?)"
            )
            task = storage.create_agent_task(self.run_id, name)
        return task

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
