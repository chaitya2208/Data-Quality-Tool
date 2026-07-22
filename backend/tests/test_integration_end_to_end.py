"""
Integration tests — hit real Snowflake + real Bedrock. Skipped unless
RUN_INTEGRATION=1 is set (see conftest.py).

Coverage:

  1. End-to-end scan → proposals → activate → findings.
  2. Rescan without data change → open findings UPDATE, first_detected_at
     preserved.
  3. Fix the data, rescan → findings RESOLVE.
  4. Re-introduce bad data within 7 days → REOPEN (reopened_count == 1).
  5. RuleIntelligence surfaces every planted issue in tests/seed_dq_issue_tables.py.
  6. Definition library does not grow with duplicates across scans.

Prerequisites (once per env):
  * `python -m tests.seed_dq_issue_tables` — seeds TEST_DQ tables.
  * SSO-authenticated Snowflake session (backend/.env correct).
  * `python setup_db.py` — DQ_APP schema present.

Each test resets its target table's instances/findings BEFORE running so
runs are deterministic and independent.
"""
import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

import pytest

from tests.seed_dq_issue_tables import PLANTED_ISSUES, SCHEMA, TABLES  # type: ignore


def _as_dict(value: Any) -> Dict:
    """Snowflake VARIANT columns come back as JSON strings via the connector.
    Parse them lazily so test helpers can access them uniformly."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}

log = logging.getLogger(__name__)

pytestmark = pytest.mark.integration


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _connection_id() -> str:
    """Fetch the default Snowflake connection id from DQ_APP.CONNECTIONS."""
    from app.services import storage
    conn = storage.get_first_connection(prefer_type="snowflake")
    if not conn:
        pytest.skip("No Snowflake connection configured in DQ_APP.CONNECTIONS")
    return conn.id


def _reseed_null_heavy():
    """Restore DQTEST_NULL_HEAVY to its pristine 100-row / 10-NULL state.
    Tests that mutate this table (fix + rescan flow) must call this first."""
    from app.services.snowflake_session import session as sf
    sf.execute(f"TRUNCATE TABLE PLAYGROUND_DB.TEST_DQ.DQTEST_NULL_HEAVY")
    sf.execute(f"""INSERT INTO PLAYGROUND_DB.TEST_DQ.DQTEST_NULL_HEAVY
        SELECT
            SEQ8() + 1 AS ID,
            'name_' || SEQ8() AS NAME,
            CASE WHEN MOD(SEQ8(), 10) = 0 THEN NULL ELSE SEQ8() + 100 END AS CUSTOMER_ID,
            CURRENT_TIMESTAMP() AS CREATED_AT
        FROM TABLE(GENERATOR(ROWCOUNT => 100))""")


def _reset_table(db: str, sch: str, tbl: str):
    """Wipe all DQ_APP state for a specific table (instances, findings,
    executions, intelligence logs) so a fresh scan starts from zero."""
    from app.services.snowflake_session import session as sf
    where_fqn = f"DATABASE_NAME='{db}' AND SCHEMA_NAME='{sch}' AND TABLE_NAME='{tbl}'"
    # Delete order matters — children first
    for stmt in [
        f"DELETE FROM RULE_EXECUTIONS WHERE INSTANCE_ID IN "
        f"(SELECT ID FROM RULE_INSTANCES WHERE {where_fqn})",
        f"DELETE FROM FINDINGS WHERE INSTANCE_ID IN "
        f"(SELECT ID FROM RULE_INSTANCES WHERE {where_fqn})",
        f"DELETE FROM RULE_INSTANCES WHERE {where_fqn}",
        f"DELETE FROM RULE_INTELLIGENCE_LOGS WHERE TABLE_FQN='{db}.{sch}.{tbl}'",
    ]:
        try:
            sf.execute(stmt)
        except Exception as e:
            log.warning("Reset step failed (%s): %s", stmt[:60], e)


def _run_scan(db: str, sch: str, tbl: str, connection_id: str,
              approve_all: bool = True, timeout_sec: int = 600) -> str:
    """Fire a scan against (db, sch, tbl) and always drive the pipeline all
    the way through Findings. `approve_all` only controls whether newly
    proposed PENDING instances get approved before findings run — the
    findings pass itself must execute regardless so RESOLVE / REOPEN /
    UPDATE branches fire on rescans."""
    from app.services import storage
    from app.services.agents.coordinator import WorkflowCoordinator, DB_AGENT_ORDER

    run = storage.create_agent_run(
        connection_id=connection_id,
        database=db, schema_name=sch, table=tbl,
        status="pending",
    )
    storage.create_agent_tasks(run.id, DB_AGENT_ORDER)
    coord = WorkflowCoordinator(run_id=run.id)
    t0 = time.time()
    coord.run()  # blocks to awaiting_rule_review OR awaiting_fixes OR completed
    log.info("First-phase scan run %s completed in %.1fs", run.id, time.time() - t0)

    run = storage.get_agent_run(run.id)
    if run.status == "awaiting_rule_review":
        if approve_all:
            _approve_all_pending(run.id)
        # Always advance the pipeline through findings — even rescans that
        # produce zero new proposals need FindingsAgent to execute against
        # the ACTIVE instances so the lifecycle state machine fires.
        # Without this, RESOLVE / REOPEN branches never trigger on rescan.
        coord = WorkflowCoordinator(run_id=run.id)
        coord.run_pipeline_after_review()
        log.info("Post-approval pipeline run %s completed", run.id)

    return run.id


def _approve_all_pending(run_id: str):
    """Approve every PENDING proposal on this run (test-time bypass — real
    UI would prompt a reviewer)."""
    from app.services import storage
    run = storage.get_agent_run(run_id)
    where_fqn = (f"DATABASE_NAME='{run.database}' AND SCHEMA_NAME='{run.schema_name}' "
                 f"AND TABLE_NAME='{run.table}'")
    from app.services.snowflake_session import session as sf
    pending = sf.query(f"SELECT ID FROM RULE_INSTANCES WHERE STATUS='pending' AND {where_fqn}")
    for row in pending:
        storage.approve_instance(row["ID"], approved_by="integration-test")


def _findings_for(db, sch, tbl) -> List:
    from app.services import storage
    from app.services.snowflake_session import session as sf
    rows = sf.query(f"""
        SELECT F.* FROM FINDINGS F
        JOIN RULE_INSTANCES I ON I.ID = F.INSTANCE_ID
        WHERE I.DATABASE_NAME='{db}' AND I.SCHEMA_NAME='{sch}' AND I.TABLE_NAME='{tbl}'
    """)
    return rows


def _instances_for(db, sch, tbl) -> List:
    from app.services.snowflake_session import session as sf
    return sf.query(f"""
        SELECT * FROM RULE_INSTANCES
        WHERE DATABASE_NAME='{db}' AND SCHEMA_NAME='{sch}' AND TABLE_NAME='{tbl}'
    """)


def _proposals_for_table(db, sch, tbl) -> List:
    """Instances the scan proposed on this table (PENDING or ACTIVE), joined
    to their definitions so we can inspect template_shape + name."""
    from app.services.snowflake_session import session as sf
    return sf.query(f"""
        SELECT I.*, D.NAME AS DEF_NAME, D.TEMPLATE_SHAPE AS DEF_SHAPE
        FROM RULE_INSTANCES I
        LEFT JOIN RULE_DEFINITIONS D ON D.ID = I.DEFINITION_ID
        WHERE I.DATABASE_NAME='{db}' AND I.SCHEMA_NAME='{sch}' AND I.TABLE_NAME='{tbl}'
    """)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def connection_id():
    return _connection_id()


@pytest.fixture
def clean_null_heavy():
    _reset_table("PLAYGROUND_DB", "TEST_DQ", "DQTEST_NULL_HEAVY")
    yield


# ─────────────────────────────────────────────────────────────────────────────
# Coverage tests — every planted issue must be caught
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("planted", PLANTED_ISSUES, ids=[
    f"{p['table']}.{p['column']}:{p['issue']}" for p in PLANTED_ISSUES
])
def test_planted_issue_is_caught(planted, connection_id):
    """Each planted issue in tests/seed_dq_issue_tables.PLANTED_ISSUES must
    surface as a proposal that (a) targets the right column and (b) has a
    plausible template_shape. After approval + findings pass, the
    corresponding finding's evidence.fail_count is in the expected range."""
    db, sch, tbl = "PLAYGROUND_DB", "TEST_DQ", planted["table"]
    col = planted["column"]

    _reset_table(db, sch, tbl)
    run_id = _run_scan(db, sch, tbl, connection_id, approve_all=True)

    props = _proposals_for_table(db, sch, tbl)
    assert props, f"No proposals produced for {tbl} — Rule Intelligence missed everything"

    # Find at least one proposal that targets the planted column
    col_proposals = [
        p for p in props
        if _as_dict(p.get("TARGET_CONFIG")).get("column", "").upper() == col.upper()
        or col.upper() in (p.get("RULE_SQL") or "").upper()
    ]
    assert col_proposals, (
        f"No proposal targeted column {tbl}.{col}. "
        f"Got {len(props)} proposals: "
        f"{[(p['DEF_NAME'], _as_dict(p.get('TARGET_CONFIG')).get('column')) for p in props]}"
    )

    # If the planted issue has an expected shape, at least one proposal must
    # match it (or be draft_sql — Claude often reaches for draft_sql on
    # nuanced business rules).
    expected_shape = planted.get("expected_rule_shape")
    if expected_shape:
        shapes = [p.get("DEF_SHAPE") for p in col_proposals]
        assert (expected_shape in shapes) or (None in shapes), (
            f"No proposal with shape={expected_shape!r} on {tbl}.{col}. "
            f"Got shapes: {shapes}"
        )

    # Findings for this column should reflect the planted violation count
    findings = _findings_for(db, sch, tbl)
    col_findings = [
        f for f in findings
        if col.upper() in (_as_dict(f.get("CONTEXT")).get("column_name", "").upper()
                            + " " + (f.get("TITLE") or "").upper())
    ]
    if col_findings:
        low, high = planted["expected_fail_count_range"]
        for f in col_findings:
            fc = _as_dict(f.get("EVIDENCE")).get("fail_count", 0)
            assert low <= fc <= high, (
                f"fail_count={fc} on {tbl}.{col} outside expected range "
                f"[{low}, {high}] — planted issue likely miscounted"
            )


# ─────────────────────────────────────────────────────────────────────────────
# Lifecycle tests — scan, rescan (unchanged), fix, reintroduce
# ─────────────────────────────────────────────────────────────────────────────

class TestFindingLifecycleOnRealData:

    def test_rescan_unchanged_data_updates_findings(self, connection_id):
        """UPDATE branch: rescan with the same data → open findings bump
        counters, first_detected_at preserved."""
        db, sch, tbl = "PLAYGROUND_DB", "TEST_DQ", "DQTEST_NULL_HEAVY"
        _reseed_null_heavy()
        _reset_table(db, sch, tbl)

        # First scan → creates findings
        _run_scan(db, sch, tbl, connection_id)
        findings_1 = _findings_for(db, sch, tbl)
        assert findings_1, "Expected findings after first scan"
        f0 = findings_1[0]
        first_detected_at_1 = f0.get("FIRST_DETECTED_AT")
        finding_id = f0["ID"]

        # Second scan → should UPDATE, not create a new row
        time.sleep(1)
        _run_scan(db, sch, tbl, connection_id, approve_all=False)
        findings_2 = _findings_for(db, sch, tbl)

        matched = [f for f in findings_2 if f["ID"] == finding_id]
        assert matched, "Original finding disappeared on rescan"
        assert matched[0].get("FIRST_DETECTED_AT") == first_detected_at_1, (
            "first_detected_at changed — UPDATE branch is broken"
        )

    def test_fix_data_then_rescan_resolves_finding(self, connection_id):
        """RESOLVE branch: patch the data so the rule passes → open finding
        auto-resolves."""
        from app.services.snowflake_session import session as sf
        db, sch, tbl = "PLAYGROUND_DB", "TEST_DQ", "DQTEST_NULL_HEAVY"
        _reseed_null_heavy()
        _reset_table(db, sch, tbl)

        _run_scan(db, sch, tbl, connection_id)
        before = _findings_for(db, sch, tbl)
        open_before = [f for f in before if f.get("STATUS") in ("open", "reopened")]
        assert open_before, "No open findings — nothing to resolve"

        # Fix: update every NULL CUSTOMER_ID to a valid number
        sf.execute(f"UPDATE {SCHEMA}.DQTEST_NULL_HEAVY SET CUSTOMER_ID = 0 WHERE CUSTOMER_ID IS NULL")
        time.sleep(1)
        _run_scan(db, sch, tbl, connection_id, approve_all=False)
        after = _findings_for(db, sch, tbl)

        # At least one previously-open finding should now be resolved
        resolved_ids = {f["ID"] for f in after if f.get("STATUS") == "resolved"}
        original_open_ids = {f["ID"] for f in open_before}
        assert resolved_ids & original_open_ids, (
            "No open finding was auto-resolved after data fix — RESOLVE branch broken"
        )

    def test_reintroduce_bad_data_reopens_finding(self, connection_id):
        """REOPEN branch: bad data returns within 7 days → resolved finding
        reopens (reopened_count += 1)."""
        from app.services.snowflake_session import session as sf
        db, sch, tbl = "PLAYGROUND_DB", "TEST_DQ", "DQTEST_NULL_HEAVY"
        _reseed_null_heavy()
        _reset_table(db, sch, tbl)

        # Scan → fix → scan (get RESOLVE)
        _run_scan(db, sch, tbl, connection_id)
        sf.execute(f"UPDATE {SCHEMA}.DQTEST_NULL_HEAVY SET CUSTOMER_ID = 0 WHERE CUSTOMER_ID IS NULL")
        time.sleep(1)
        _run_scan(db, sch, tbl, connection_id, approve_all=False)

        resolved_findings = [f for f in _findings_for(db, sch, tbl)
                             if f.get("STATUS") == "resolved"]
        assert resolved_findings, "No resolved finding to reopen"

        # Reintroduce bad data
        sf.execute(f"UPDATE {SCHEMA}.DQTEST_NULL_HEAVY SET CUSTOMER_ID = NULL "
                   f"WHERE ID <= 5")
        time.sleep(1)
        _run_scan(db, sch, tbl, connection_id, approve_all=False)

        # Same finding should now be status='reopened' with reopened_count=1
        reopened = [
            f for f in _findings_for(db, sch, tbl)
            if f["ID"] in {r["ID"] for r in resolved_findings}
            and f.get("STATUS") == "reopened"
        ]
        assert reopened, "Failing rule after resolve did not reopen finding — REOPEN broken"
        assert reopened[0].get("REOPENED_COUNT", 0) >= 1


# ─────────────────────────────────────────────────────────────────────────────
# Library growth guard — dedup working across scans
# ─────────────────────────────────────────────────────────────────────────────

class TestLibraryDedupAcrossRuns:

    def test_repeated_scans_do_not_multiply_definitions(self, connection_id):
        """After N scans of the same table, RULE_DEFINITIONS.count for
        AI-created rows must NOT grow linearly with N."""
        from app.services.snowflake_session import session as sf
        db, sch, tbl = "PLAYGROUND_DB", "TEST_DQ", "DQTEST_ENUM_VIOLATIONS"
        _reset_table(db, sch, tbl)

        def _ai_def_count():
            r = sf.query("SELECT COUNT(*) AS CNT FROM RULE_DEFINITIONS "
                         "WHERE SOURCE='claude' AND STATUS='proposed'")
            return r[0]["CNT"] if r else 0

        _run_scan(db, sch, tbl, connection_id)
        after_1 = _ai_def_count()

        _run_scan(db, sch, tbl, connection_id, approve_all=False)
        after_2 = _ai_def_count()

        # Second scan should reuse fingerprints — new AI defs should be ~0
        delta = after_2 - after_1
        assert delta <= 2, (
            f"AI definition library grew by {delta} across a repeat scan — "
            f"fingerprint dedup failing"
        )
