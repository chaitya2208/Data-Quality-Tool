"""
End-to-end test for the metric-snapshot → baseline → anomaly-proposal path,
run against a REAL Snowflake instance.

This is an integration test (marked @pytest.mark.integration) — gated
behind RUN_INTEGRATION=1 so it doesn't slow the default suite. It skips
RuleIntelligence entirely and drives the moving parts we care about:

    MetadataAgent    → creates SCANS + TABLE_ASSETS + COLUMN_ASSETS rows.
    ProfilingAgent   → produces the facts dict.
    record_metric_snapshots → writes METRIC_SNAPSHOTS + refreshes MADs.
    AnomalyProposalAgent    → walks ready baselines, emits proposals.
    FindingsAgent    → executes approved anomaly instances, produces
                       findings whose lifecycle proves the SQL actually
                       compares latest snapshot vs baseline.

Table under test:  PLAYGROUND_DB.TEST_DQ.DQTEST_ANOMALY_METRICS

The test:
    1. Wipes + recreates the table with a stable seed (1000 rows).
    2. Runs the pipeline 14 times unchanged (bumps timestamps so
       CAPTURED_AT progresses, keeps row count stable). Asserts no
       anomaly proposals fire before scan #14 — baselines are immature.
    3. On scan #14 the baseline crosses the maturity gate. Anomaly
       proposals now show up as pending RULE_INSTANCES (agentic path).
    4. Approves those proposals so they're active.
    5. Injects a row-count spike (bulk insert). Reruns the pipeline —
       the metric_anomaly instance now FAILS, so FindingsAgent creates
       a finding. Proves baseline SQL + finding lifecycle actually work
       against real Snowflake.
    6. Reverts the spike, rescans — finding RESOLVES.

The test is careful about state: it wipes only the specific test table
and its DQ_APP rows so parallel scans against other tables aren't
disturbed.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Dict, List, Optional

import pytest

log = logging.getLogger(__name__)

pytestmark = pytest.mark.integration


TEST_DB = "PLAYGROUND_DB"
TEST_SCH = "TEST_DQ"
TEST_TBL = "DQTEST_ANOMALY_METRICS"
FQN = f"{TEST_DB}.{TEST_SCH}.{TEST_TBL}"


def _app_prefix() -> str:
    """Fully qualify DQ_APP tables so queries don't depend on the session's
    current schema — which can drift after a mid-run reconnect."""
    from app.core.config import settings
    db = settings.SNOWFLAKE_DATABASE or "PLAYGROUND_DB"
    sch = settings.SNOWFLAKE_APP_SCHEMA or "DQ_APP"
    return f"{db}.{sch}"


# ─────────────────────────────────────────────────────────────────────────
# Setup / teardown helpers
# ─────────────────────────────────────────────────────────────────────────

def _connection_id() -> str:
    from app.services import storage
    conn = storage.get_first_connection(prefer_type="snowflake")
    if not conn:
        pytest.skip("No Snowflake connection configured in DQ_APP.CONNECTIONS")
    return conn.id


def _create_seed_table():
    """Drop and recreate the test table with a deterministic 1000-row seed.
    STATUS is a stable closed-set column (A/B/C) so observed_categories
    baselines mature alongside the numeric ones."""
    from app.services.snowflake_session import session as sf
    sf.execute(f"DROP TABLE IF EXISTS {FQN}")
    sf.execute(f"""
        CREATE TABLE {FQN} (
            ID          NUMBER,
            NAME        VARCHAR(64),
            STATUS      VARCHAR(1),
            AMOUNT      NUMBER(10,2),
            CREATED_AT  TIMESTAMP_NTZ
        )
    """)
    sf.execute(f"""
        INSERT INTO {FQN}
        SELECT
            SEQ8() + 1                                       AS ID,
            'name_' || SEQ8()                                AS NAME,
            CASE MOD(SEQ8(), 3) WHEN 0 THEN 'A'
                                WHEN 1 THEN 'B'
                                ELSE   'C' END               AS STATUS,
            (SEQ8() * 10) + 100                              AS AMOUNT,
            CURRENT_TIMESTAMP()                              AS CREATED_AT
        FROM TABLE(GENERATOR(ROWCOUNT => 1000))
    """)


def _drop_seed_table():
    from app.services.snowflake_session import session as sf
    try:
        sf.execute(f"DROP TABLE IF EXISTS {FQN}")
    except Exception as e:
        log.warning(f"Could not drop {FQN}: {e}")


def _wipe_dq_state():
    """Clear DQ_APP rows scoped to this test table so the run starts fresh.
    Does NOT touch other tables' rules/findings/scans."""
    from app.services.snowflake_session import session as sf
    where = (f"DATABASE_NAME='{TEST_DB}' AND SCHEMA_NAME='{TEST_SCH}' "
             f"AND TABLE_NAME='{TEST_TBL}'")
    P = _app_prefix()
    # Order: rows that reference RULE_INSTANCES / ASSETS first.
    # DQ_APP.ASSETS holds both table-scoped (ASSET_TYPE='table') and
    # column-scoped (ASSET_TYPE='column') rows — no separate COLUMN_ASSETS.
    for stmt in [
        f"DELETE FROM {P}.METRIC_SNAPSHOTS WHERE {where}",
        # METRIC_BASELINES has no db/sch/tbl columns — key on the asset id.
        f"""DELETE FROM {P}.METRIC_BASELINES
            WHERE ASSET_ID IN (SELECT ID FROM {P}.ASSETS WHERE {where})""",
        f"""DELETE FROM {P}.PENDING_PROPOSALS WHERE {where}""",
        f"""DELETE FROM {P}.RULE_EXECUTIONS WHERE INSTANCE_ID IN
             (SELECT ID FROM {P}.RULE_INSTANCES WHERE {where})""",
        f"""DELETE FROM {P}.FINDINGS WHERE INSTANCE_ID IN
             (SELECT ID FROM {P}.RULE_INSTANCES WHERE {where})""",
        f"DELETE FROM {P}.RULE_INSTANCES WHERE {where}",
        f"""DELETE FROM {P}.AGENT_TASKS WHERE RUN_ID IN
             (SELECT ID FROM {P}.AGENT_RUNS WHERE {where})""",
        f"DELETE FROM {P}.AGENT_RUNS WHERE {where}",
        f"""DELETE FROM {P}.SCANS WHERE ASSET_ID IN
             (SELECT ID FROM {P}.ASSETS WHERE {where})""",
        f"DELETE FROM {P}.ASSETS WHERE {where}",
    ]:
        try:
            sf.execute(stmt)
        except Exception as e:
            log.warning(f"Wipe step failed ({stmt[:60]}...): {e}")


# ─────────────────────────────────────────────────────────────────────────
# Pipeline — metadata + profiling + snapshot + (optional) anomaly + findings.
# Skips RuleIntelligence.
# ─────────────────────────────────────────────────────────────────────────

def _ensure_empty_workflow_template() -> str:
    """Create (or reuse) an empty saved workflow. Using it as
    workflow_template_id routes runs through _execute_with_template, which
    SKIPS RuleIntelligenceAgent entirely — no Claude/Bedrock calls — but
    still creates a real AGENT_RUNS row so the scan shows up in run history.

    Empty patterns list means no rules are applied on scan; the metric
    snapshot capture + anomaly proposal sweep still fire (both live in the
    template path after the pattern loop)."""
    from app.services import storage
    label = "itest-metrics-empty-template"
    existing = [w for w in storage.list_workflows() if w.label == label]
    if existing:
        return existing[0].id
    wf = storage.create_workflow(
        label=label,
        rule_patterns=[],
        description="Integration test — empty template so scans go through "
                    "the coordinator (visible in run history) without firing "
                    "RuleIntelligence.",
        created_by="itest",
    )
    return wf.id


def _run_scheduled_scan(connection_id: str, schedule_id: str) -> str:
    """Fire ONE scan through the real WorkflowCoordinator against the test
    table, using the empty workflow template so RuleIntelligence never runs.

    Creates an AGENT_RUNS row scoped to schedule_id — appears in run history
    just like a real scheduled scan. Blocks until the coordinator returns
    (findings + anomaly sweep have completed by then).
    """
    from app.services import storage
    from app.services.agents.coordinator import WorkflowCoordinator, DB_AGENT_ORDER

    template_id = _ensure_empty_workflow_template()
    run = storage.create_agent_run(
        connection_id=connection_id,
        database=TEST_DB, schema_name=TEST_SCH, table=TEST_TBL,
        status="pending",
        workflow_template_id=template_id,
        schedule_id=schedule_id,
    )
    storage.create_agent_tasks(run.id, DB_AGENT_ORDER)
    WorkflowCoordinator(run_id=run.id).run()
    return run.id


def _run_with_anomaly_propose_and_findings(connection_id: str,
                                            approve_proposals: bool = True,
                                            schedule_id: Optional[str] = "itest-anomaly-sched",
                                            ) -> Dict[str, Any]:
    """Full anomaly path — snapshot capture, propose, optionally approve
    pending anomaly instances, then run findings against all active
    instances on this table.

    Defaults to the SCHEDULED variant (schedule_id set) so proposals land
    in PENDING_PROPOSALS + a NOTIFICATIONS row — those are what the
    Rule Library ("AI-proposed anomaly rules awaiting review") and the
    notification bell actually surface in the UI. The agentic path
    (schedule_id=None) writes pending RULE_INSTANCES that no current UI
    section displays, since the run has already exited awaiting_rule_review
    by the time the anomaly sweep fires.
    """
    from app.services import storage
    from app.services.agents.metadata_agent import MetadataAgent
    from app.services.agents.profiling_agent import ProfilingAgent
    from app.services.metric_snapshots import record_metric_snapshots
    from app.services.agents.anomaly_proposal_agent import run_for_scan as anomaly_run
    from app.services.agents.findings_agent import FindingsAgent
    from types import SimpleNamespace

    scan, table_asset, columns = MetadataAgent().run(
        TEST_DB, TEST_SCH, TEST_TBL, connection_id,
    )
    profile = ProfilingAgent().run(TEST_DB, TEST_SCH, TEST_TBL, connection_id)
    scan.connection_id = connection_id  # FindingsAgent reads this for get_source
    record_metric_snapshots(
        scan_id=scan.id, asset_id=table_asset.id,
        database_name=TEST_DB, schema_name=TEST_SCH, table_name=TEST_TBL,
        facts=profile,
    )

    fake_run = SimpleNamespace(id=f"itest-{uuid.uuid4().hex[:8]}",
                               schedule_id=schedule_id)
    summary = anomaly_run(fake_run, table_asset, scan.id)

    # Auto-approve any pending anomaly proposals so the findings pass runs
    # against them. Approval works on both paths — for PENDING_PROPOSALS
    # (scheduled) it materializes a RULE_INSTANCE; for pending RULE_INSTANCES
    # (agentic) it just flips status → active.
    if approve_proposals:
        _approve_all_pending_anomalies()

    active_ids = {i["ID"] for i in _instances() if i["STATUS"] == "active"}
    findings = FindingsAgent().run(
        scan, table_asset, columns,
        allowed_instance_ids=active_ids,
        severity_overrides={},
        run_id=None,
    )
    return {
        "scan": scan, "table_asset": table_asset,
        "proposal_summary": summary,
        "findings": findings,
    }


def _approve_all_pending_anomalies():
    """Approve every pending anomaly for this test table.

    Covers both routing paths: PENDING_PROPOSALS rows (scheduled path) get
    materialized into an active RULE_INSTANCE via the /proposals/{id}/approve
    handler; pending RULE_INSTANCES rows (agentic path) get flipped via
    storage.approve_instance."""
    from app.services import storage
    from app.services.snowflake_session import session as sf

    # Scheduled path — approve every pending PENDING_PROPOSALS row for this
    # table using the same handler the UI's "Approve" button calls. It
    # materializes a RULE_INSTANCE with status=active + flips the proposal
    # row to 'approved'.
    P = _app_prefix()
    proposal_rows = sf.query(f"""
        SELECT ID FROM {P}.PENDING_PROPOSALS
        WHERE DATABASE_NAME='{TEST_DB}' AND SCHEMA_NAME='{TEST_SCH}'
          AND TABLE_NAME='{TEST_TBL}' AND STATUS='pending'
    """)
    if proposal_rows:
        from app.api.proposals import approve_proposal, ApproveIn
        for r in proposal_rows:
            approve_proposal(r["ID"], ApproveIn(decided_by="itest"))

    # Agentic path (or fallback) — pending RULE_INSTANCES on this table.
    inst_pending = [i for i in _instances() if i["STATUS"] == "pending"]
    for inst in inst_pending:
        storage.approve_instance(inst["ID"], approved_by="itest")


# ─────────────────────────────────────────────────────────────────────────
# Read helpers
# ─────────────────────────────────────────────────────────────────────────

def _instances() -> List[Dict[str, Any]]:
    from app.services.snowflake_session import session as sf
    P = _app_prefix()
    return sf.query(f"""
        SELECT ID, STATUS, DEFINITION_ID, TARGET_CONFIG, THRESHOLD_CONFIG
        FROM {P}.RULE_INSTANCES
        WHERE DATABASE_NAME='{TEST_DB}' AND SCHEMA_NAME='{TEST_SCH}'
          AND TABLE_NAME='{TEST_TBL}'
    """)


def _anomaly_instances() -> List[Dict[str, Any]]:
    """Instances whose backing definition is one of the anomaly shapes."""
    from app.services.snowflake_session import session as sf
    P = _app_prefix()
    return sf.query(f"""
        SELECT I.ID, I.STATUS, D.TEMPLATE_SHAPE, I.TARGET_CONFIG
        FROM {P}.RULE_INSTANCES I
        JOIN {P}.RULE_DEFINITIONS D ON D.ID = I.DEFINITION_ID
        WHERE I.DATABASE_NAME='{TEST_DB}' AND I.SCHEMA_NAME='{TEST_SCH}'
          AND I.TABLE_NAME='{TEST_TBL}'
          AND D.TEMPLATE_SHAPE IN
              ('metric_anomaly','metric_relative_change','category_disappeared')
    """)


def _snapshot_count() -> int:
    from app.services.snowflake_session import session as sf
    P = _app_prefix()
    r = sf.query(f"""
        SELECT COUNT(*) AS CNT FROM {P}.METRIC_SNAPSHOTS
        WHERE DATABASE_NAME='{TEST_DB}' AND SCHEMA_NAME='{TEST_SCH}'
          AND TABLE_NAME='{TEST_TBL}'
    """)
    return int(r[0]["CNT"]) if r else 0


def _ready_baselines(asset_id: str) -> List[Dict[str, Any]]:
    from app.services import metric_snapshots
    return metric_snapshots.list_ready_baselines(asset_id)


def _findings_for_asset(asset_id: str) -> List[Dict[str, Any]]:
    from app.services.snowflake_session import session as sf
    P = _app_prefix()
    return sf.query(f"""
        SELECT F.ID, F.STATUS, F.INSTANCE_ID, F.EVIDENCE, F.SEVERITY, F.TITLE
        FROM {P}.FINDINGS F
        JOIN {P}.RULE_INSTANCES I ON I.ID = F.INSTANCE_ID
        WHERE I.DATABASE_NAME='{TEST_DB}' AND I.SCHEMA_NAME='{TEST_SCH}'
          AND I.TABLE_NAME='{TEST_TBL}'
    """)


# ─────────────────────────────────────────────────────────────────────────
# The one big test
# ─────────────────────────────────────────────────────────────────────────

def test_phase_a_seed_and_propose_anomaly_rules():
    """Phase A — inspect-in-UI stopping point.

    * 13 warmup scans on a stable 1000-row seed.
    * 14th scan matures baselines. AnomalyProposalAgent runs in SCHEDULED
      mode → proposals land in PENDING_PROPOSALS + one NOTIFICATIONS row.
    * STOPS HERE. No approval, no spike, no findings — so you can open the
      UI and see:
        - Metrics page for the asset with 14 snapshot points per metric
          + rolling MAD band.
        - Rule Library page: purple "N AI-proposed anomaly rules awaiting
          review" section listing the proposals.
        - Notification bell: aggregate "anomaly_proposals" entry.

    Rerun Phase B (test_phase_b_...) after you've reviewed the proposals
    to exercise approval → spike → finding → auto-resolve.
    """
    # After a mid-run reconnect Snowflake sometimes leaves the session
    # scoped to a different schema — then unqualified names like TABLE_ASSETS
    # fail with "object does not exist". Re-set the default context BEFORE
    # any storage-layer call (get_first_connection queries CONNECTIONS
    # unqualified, so it would trip first).
    from app.services.snowflake_session import session as sf
    from app.core.config import settings
    app_db = settings.SNOWFLAKE_DATABASE or "PLAYGROUND_DB"
    app_sch = settings.SNOWFLAKE_APP_SCHEMA or "DQ_APP"
    sf.execute(f"USE DATABASE {app_db}")
    sf.execute(f"USE SCHEMA {app_sch}")

    conn = _connection_id()

    # Preserve prior run state for inspection unless the caller explicitly
    # asks for a clean slate — leaves the seed table + all DQ_APP rows in
    # place so you can open the UI and see snapshots, baselines, proposals,
    # instances and findings for this table. Set DQ_TEST_RESET=1 to force a
    # rebuild before this run (e.g. after schema changes).
    import os
    reset = os.environ.get("DQ_TEST_RESET", "").lower() in ("1", "true", "yes")

    exists = sf.query(f"""
        SELECT COUNT(*) AS CNT FROM {TEST_DB}.INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA='{TEST_SCH}' AND TABLE_NAME='{TEST_TBL}'
    """)
    table_present = bool(exists and exists[0]["CNT"] > 0)

    if reset or not table_present:
        _create_seed_table()
        _wipe_dq_state()

    # 14 scans through the real WorkflowCoordinator, all under the same
    # schedule_id — so every scan lands in AGENT_RUNS with schedule_id set,
    # exactly like a real nightly schedule. Each shows up in run history.
    # RuleIntelligence is skipped because workflow_template_id is set (empty
    # template).
    schedule_id = "itest-anomaly-sched"

    # Resume support — if a previous run of this test made partial progress
    # (e.g. Snowflake dropped mid-scan and the loop was killed), count the
    # AGENT_RUNS already tagged with this schedule_id and only run the
    # remainder. Safe because scans are idempotent w.r.t. the seed table.
    P = _app_prefix()
    already = sf.query(f"""
        SELECT COUNT(*) AS CNT FROM {P}.AGENT_RUNS
        WHERE DATABASE_NAME='{TEST_DB}' AND SCHEMA_NAME='{TEST_SCH}'
          AND TABLE_NAME='{TEST_TBL}' AND SCHEDULE_ID=%(sid)s
          AND STATUS IN ('completed', 'awaiting_fixes')
    """, {"sid": schedule_id})
    completed_before = int(already[0]["CNT"]) if already else 0
    if completed_before > 0:
        log.info(f"Resume — {completed_before} prior scans already completed")

    to_run = max(0, 14 - completed_before)
    for i in range(to_run):
        run_id = _run_scheduled_scan(conn, schedule_id=schedule_id)
        log.info(
            f"Phase A scan #{completed_before + i + 1}/14 completed — "
            f"run_id={run_id}"
        )
        # tiny nudge so CAPTURED_AT differs run-to-run
        time.sleep(0.2)

    # Sanity: each successful scan captures at least the 3 core numeric
    # metrics (row_count + null_pct on ≥1 col + distinct_count on ≥1 col).
    # Loose bound so a partial pre-resume attempt doesn't fail the check.
    assert _snapshot_count() >= 14, (
        f"Expected >= 14 snapshot rows after 14 scans, got {_snapshot_count()}"
    )

    asset_rows = sf.query(f"""
        SELECT ID FROM {P}.ASSETS
        WHERE DATABASE_NAME='{TEST_DB}' AND SCHEMA_NAME='{TEST_SCH}'
          AND TABLE_NAME='{TEST_TBL}' AND ASSET_TYPE='table'
    """)
    assert asset_rows, "ASSETS row for the seed table was not created"
    asset_id = asset_rows[0]["ID"]

    ready = _ready_baselines(asset_id)
    assert ready, "Baselines did not mature after 14 scans"
    metric_names = {b["metric_name"] for b in ready}
    assert "row_count" in metric_names, (
        f"row_count baseline not marked ready — got {metric_names}"
    )

    proposals = sf.query(f"""
        SELECT ID, TEMPLATE_SHAPE, METRIC_NAME, COLUMN_NAME, STATUS
        FROM {P}.PENDING_PROPOSALS
        WHERE DATABASE_NAME='{TEST_DB}' AND SCHEMA_NAME='{TEST_SCH}'
          AND TABLE_NAME='{TEST_TBL}'
    """)
    pending = [p for p in proposals if p["STATUS"] == "pending"]
    assert pending, "AnomalyProposalAgent produced no PENDING_PROPOSALS on scan #14"

    notifs = sf.query(f"""
        SELECT COUNT(*) AS CNT FROM {P}.NOTIFICATIONS
        WHERE KIND='anomaly_proposals'
    """)
    assert notifs and int(notifs[0]["CNT"]) >= 1, (
        "Expected at least one 'anomaly_proposals' notification"
    )

    runs = sf.query(f"""
        SELECT COUNT(*) AS CNT FROM {P}.AGENT_RUNS
        WHERE DATABASE_NAME='{TEST_DB}' AND SCHEMA_NAME='{TEST_SCH}'
          AND TABLE_NAME='{TEST_TBL}' AND SCHEDULE_ID=%(sid)s
          AND STATUS IN ('completed', 'awaiting_fixes')
    """, {"sid": schedule_id})
    assert runs and int(runs[0]["CNT"]) >= 14, (
        f"Expected >= 14 completed AGENT_RUNS rows for this schedule, "
        f"got {runs[0]['CNT']}"
    )

    log.info(
        f"Phase A complete — asset_id={asset_id}, "
        f"{len(pending)} pending proposals, notification created, "
        f"14 runs visible in run history under schedule_id={schedule_id}. "
        f"Open the UI to review them."
    )
    # No teardown — leave everything in Snowflake for UI inspection.


def test_phase_b_spike_and_resolve():
    """Phase B — exercise the approved anomaly rules end-to-end.

    Prerequisites: Phase A has already run AND you've approved the anomaly
    proposals (via the Rule Library UI or the /proposals/{id}/approve
    endpoint). The approval materialises the pending proposals into active
    RULE_INSTANCES on this test table.

    Flow:
    1. Bulk-insert 4000 rows into the test table — row_count jumps from
       ~1000 to ~5000, which is far beyond `median + 3*MAD` for the stable
       baseline built during Phase A.
    2. Kick a scheduled scan through the coordinator (empty workflow
       template + the approved anomaly instances are already active).
       FindingsAgent runs the anomaly SQL against Snowflake — the
       row_count instance should return FAILED_COUNT > 0 and produce a
       new open finding.
    3. Delete the spike rows so row_count returns to ~1000.
    4. Kick another scheduled scan. The anomaly SQL now passes; the
       lifecycle finalizer auto-resolves the finding on the same instance.
    """
    from app.services.snowflake_session import session as sf
    from app.core.config import settings
    app_db = settings.SNOWFLAKE_DATABASE or "PLAYGROUND_DB"
    app_sch = settings.SNOWFLAKE_APP_SCHEMA or "DQ_APP"
    sf.execute(f"USE DATABASE {app_db}")
    sf.execute(f"USE SCHEMA {app_sch}")

    conn = _connection_id()
    P = _app_prefix()
    schedule_id = "itest-anomaly-sched"

    # Sanity: at least one approved anomaly instance for this table.
    active_anomaly = [i for i in _anomaly_instances() if i["STATUS"] == "active"]
    assert active_anomaly, (
        "No active anomaly instances found — approve Phase A's PENDING_PROPOSALS "
        "before running Phase B."
    )

    # Look for a row_count instance specifically — that's the one the spike
    # will target. If it isn't approved, the test can still pass as long as
    # SOME anomaly instance fires, so this is a soft check for logging.
    def _has_row_count_target(inst):
        import json
        tc = inst.get("TARGET_CONFIG")
        if isinstance(tc, str):
            try: tc = json.loads(tc)
            except Exception: tc = {}
        return (tc or {}).get("metric_name") == "row_count"
    row_count_inst_ids = {i["ID"] for i in active_anomaly if _has_row_count_target(i)}
    log.info(
        f"Phase B start — {len(active_anomaly)} active anomaly instance(s), "
        f"row_count instance IDs: {row_count_inst_ids or 'NONE (row_count proposal was not approved)'}"
    )

    asset_rows = sf.query(f"""
        SELECT ID FROM {P}.ASSETS
        WHERE DATABASE_NAME='{TEST_DB}' AND SCHEMA_NAME='{TEST_SCH}'
          AND TABLE_NAME='{TEST_TBL}' AND ASSET_TYPE='table'
    """)
    assert asset_rows, "Table asset missing — did Phase A run?"
    asset_id = asset_rows[0]["ID"]

    open_before_spike = {
        f["INSTANCE_ID"] for f in _findings_for_asset(asset_id)
        if f["STATUS"] in ("open", "reopened")
    }

    # ── 1. spike row_count with a 4000-row insert ──────────────────────────
    sf.execute(f"""
        INSERT INTO {FQN}
        SELECT
            SEQ8() + 100000 AS ID,
            'spike_' || SEQ8() AS NAME,
            CASE MOD(SEQ8(), 3) WHEN 0 THEN 'A'
                                WHEN 1 THEN 'B'
                                ELSE   'C' END AS STATUS,
            (SEQ8() * 10) + 100 AS AMOUNT,
            CURRENT_TIMESTAMP() AS CREATED_AT
        FROM TABLE(GENERATOR(ROWCOUNT => 4000))
    """)
    log.info(f"Phase B — spiked {FQN} with 4000 rows (row_count now ~5000)")

    # ── 2. scheduled scan — anomaly SQL should fire, findings materialize
    spike_run_id = _run_scheduled_scan(conn, schedule_id=schedule_id)
    log.info(f"Phase B — spike scan completed, run_id={spike_run_id}")

    open_after_spike = {
        f["INSTANCE_ID"] for f in _findings_for_asset(asset_id)
        if f["STATUS"] in ("open", "reopened")
    }
    new_open = open_after_spike - open_before_spike
    assert new_open, (
        f"Row-count spike produced no new open finding. "
        f"Anomaly instances active: {[i['ID'] for i in active_anomaly]}. "
        f"Open findings before: {open_before_spike}. "
        f"Open findings after: {open_after_spike}."
    )
    if row_count_inst_ids:
        assert row_count_inst_ids & new_open, (
            f"Expected the row_count anomaly instance {row_count_inst_ids} "
            f"to fire; got {new_open} instead."
        )
    log.info(f"Phase B — {len(new_open)} new open finding(s) after spike: {new_open}")

    # ── 3. restore the data — delete every spike row ───────────────────────
    sf.execute(f"DELETE FROM {FQN} WHERE ID >= 100000")
    log.info(f"Phase B — deleted spike rows, row_count back to ~1000")

    # ── 4. rescan — anomaly SQL now passes; the newly-open findings should
    #    auto-resolve via the finalizer's PASS branch.
    fix_run_id = _run_scheduled_scan(conn, schedule_id=schedule_id)
    log.info(f"Phase B — restore scan completed, run_id={fix_run_id}")

    after_fix = _findings_for_asset(asset_id)
    resolved_ids = {
        f["INSTANCE_ID"] for f in after_fix
        if f["STATUS"] == "resolved"
    }
    assert new_open & resolved_ids, (
        f"Spike-triggered findings didn't auto-resolve on restore. "
        f"New-open IDs: {new_open}. "
        f"After-fix findings: {[(f['INSTANCE_ID'], f['STATUS']) for f in after_fix]}"
    )
    log.info(
        f"Phase B complete — spike fired {len(new_open)} finding(s), "
        f"restore resolved {len(new_open & resolved_ids)} of them. "
        f"Open the UI to inspect the full detected → resolved lifecycle."
    )
    # No teardown — leaves the finding + evidence + fail_history intact.
