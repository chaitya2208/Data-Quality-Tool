"""
Shared pytest fixtures for the DQ Tool test suite.

Two test tiers:

  * UNIT tests — no Snowflake, no Bedrock. Every collaborator is mocked
    (storage, sf_session, ask_claude_*, data source adapters). Fast and
    deterministic. Run with: pytest tests/unit/ or pytest -m "not integration".

  * INTEGRATION tests — hit real Snowflake (PLAYGROUND_DB) and real Bedrock.
    Slow, require SSO login, marked with @pytest.mark.integration. Skipped by
    default unless the RUN_INTEGRATION env var is set to a truthy value.

Fixtures live here so both tiers can share table-asset factories, fake
sources, and reset helpers.
"""
from __future__ import annotations

import os
import sys
import types
import uuid
import datetime
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest


# ── pytest config ─────────────────────────────────────────────────────────

def pytest_collection_modifyitems(config, items):
    """Skip @integration tests unless RUN_INTEGRATION is set."""
    if os.environ.get("RUN_INTEGRATION", "").lower() in ("1", "true", "yes"):
        return
    skip_marker = pytest.mark.skip(reason="Set RUN_INTEGRATION=1 to run Snowflake/Bedrock tests")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_marker)


# ── factories ─────────────────────────────────────────────────────────────

def make_table_asset(
    fqn: str = "PLAYGROUND_DB.TEST_DQ.ORDERS",
    row_count: int = 50000,
    asset_id: Optional[str] = None,
) -> SimpleNamespace:
    parts = fqn.split(".")
    return SimpleNamespace(
        id=asset_id or f"asset-{uuid.uuid4().hex[:12]}",
        fqn=fqn,
        database_name=parts[0],
        schema_name=parts[1],
        table_name=parts[2],
        row_count=row_count,
        owner="test-owner",
        raw_metadata={},
    )


def make_definition(
    def_id: Optional[str] = None,
    name: str = "Non-Null Check",
    description: str = "Column must not be null.",
    template_shape: Optional[str] = "not_null",
    check_kind: str = "sql_template",
    status: str = "active",
    default_severity: str = "medium",
    handler_key: Optional[str] = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=def_id or f"def-{uuid.uuid4().hex[:12]}",
        name=name,
        description=description,
        template_shape=template_shape,
        check_kind=check_kind,
        status=status,
        default_severity=default_severity,
        default_threshold_config={},
        allowed_scopes=["column"],
        source="seed",
        owner="seed",
        created_by="seed",
        handler_key=handler_key,
    )


def make_instance(
    inst_id: Optional[str] = None,
    definition_id: str = "def-abc",
    scope: str = "column",
    database_name: str = "PLAYGROUND_DB",
    schema_name: str = "TEST_DQ",
    table_name: str = "ORDERS",
    fingerprint: str = "fp-abc",
    target_config: Optional[Dict] = None,
    threshold_config: Optional[Dict] = None,
    rule_sql: str = "SELECT 0 AS FAILED_COUNT, 1 AS TOTAL_COUNT",
    status: str = "active",
    severity: str = "medium",
    rationale: str = "",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=inst_id or f"inst-{uuid.uuid4().hex[:12]}",
        definition_id=definition_id,
        scope=scope,
        database_name=database_name,
        schema_name=schema_name,
        table_name=table_name,
        fingerprint=fingerprint,
        severity=severity,
        target_config=target_config or {"column": "ORDER_ID"},
        threshold_config=threshold_config or {},
        rule_sql=rule_sql,
        rationale=rationale,
        status=status,
        is_active=(status == "active"),
        rejection_reason=None,
    )


def make_finding(
    finding_id: Optional[str] = None,
    instance_id: str = "inst-abc",
    asset_id: str = "asset-abc",
    scan_id: str = "scan-abc",
    status: str = "open",
    severity: str = "medium",
    fail_count: int = 5,
    total_count: int = 100,
    first_detected_at: Optional[datetime.datetime] = None,
    resolved_at: Optional[datetime.datetime] = None,
    fail_history: Optional[List[Dict]] = None,
    reopened_count: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=finding_id or f"find-{uuid.uuid4().hex[:12]}",
        instance_id=instance_id,
        asset_id=asset_id,
        scan_id=scan_id,
        last_scan_id=scan_id,
        status=status,
        severity=severity,
        title="Test finding",
        description="Testing",
        context={},
        evidence={"fail_count": fail_count, "total_count": total_count, "sample_rows": []},
        first_detected_at=first_detected_at or datetime.datetime.utcnow(),
        detected_at=first_detected_at or datetime.datetime.utcnow(),
        last_seen_at=datetime.datetime.utcnow(),
        resolved_at=resolved_at,
        closed_at=None,
        current_fail_count=fail_count,
        current_total_count=total_count,
        fail_history=fail_history or [],
        reopened_count=reopened_count,
        assigned_to=None,
        resolution_notes=None,
        updated_at=datetime.datetime.utcnow(),
    )


@pytest.fixture
def table_asset():
    return make_table_asset()


@pytest.fixture
def make_asset():
    return make_table_asset


@pytest.fixture
def make_def():
    return make_definition


@pytest.fixture
def make_inst():
    return make_instance


@pytest.fixture
def make_find():
    return make_finding


# ── in-memory fake storage ────────────────────────────────────────────────

class FakeStorage:
    """Drop-in replacement for `app.services.storage` used in unit tests.

    Reproduces just the surface RuleIntelligence + scan_finalizer touch:
    definitions, instances, findings, mutes, critique drops, intelligence
    logs. In-memory dicts keyed by uuid.
    """

    def __init__(self):
        self.definitions: Dict[str, Any] = {}
        self.instances: Dict[str, Any] = {}
        self.findings: Dict[str, Any] = {}
        self.mutes: Dict[str, Any] = {}
        self.critique_drops: List[Dict[str, Any]] = []
        self.intelligence_logs: List[Dict[str, Any]] = []
        self._fingerprint_index: Dict[str, str] = {}

    # ── definitions
    def create_definition(self, **kwargs) -> SimpleNamespace:
        d = make_definition(
            def_id=kwargs.get("def_id"),
            name=kwargs.get("name", "Untitled"),
            description=kwargs.get("description", ""),
            template_shape=kwargs.get("template_shape"),
            check_kind=kwargs.get("check_kind", "sql_template"),
            status=kwargs.get("status", "proposed"),
            default_severity=kwargs.get("default_severity", "medium"),
        )
        d.allowed_scopes = kwargs.get("allowed_scopes", ["column"])
        self.definitions[d.id] = d
        return d

    def get_definition(self, def_id):
        return self.definitions.get(def_id)

    def get_definition_by_template_shape(self, shape):
        for d in self.definitions.values():
            if d.template_shape == shape and d.status != "disabled":
                return d
        return None

    def list_definitions(self):
        return [d for d in self.definitions.values() if d.status == "active"]

    def list_all_definitions(self):
        return list(self.definitions.values())

    def ensure_definition(self, handler_key, name, description, category, severity, allowed_scopes):
        for d in self.definitions.values():
            if getattr(d, "handler_key", None) == handler_key:
                return d
        d = self.create_definition(name=name, description=description, template_shape=None,
                                   check_kind="python_handler", status="active",
                                   default_severity=severity)
        d.handler_key = handler_key
        return d

    # ── instances
    def create_instance(self, **kwargs) -> SimpleNamespace:
        inst = make_instance(
            definition_id=kwargs["definition_id"],
            scope=kwargs.get("scope", "table"),
            database_name=kwargs["database_name"],
            schema_name=kwargs["schema_name"],
            table_name=kwargs["table_name"],
            fingerprint=kwargs["fingerprint"],
            target_config=kwargs.get("target_config"),
            threshold_config=kwargs.get("threshold_config"),
            rule_sql=kwargs.get("rule_sql", ""),
            status=kwargs.get("status", "pending"),
            severity=kwargs.get("severity", "medium"),
            rationale=kwargs.get("rationale", ""),
        )
        inst.is_active = kwargs.get("is_active", False)
        inst.owner = kwargs.get("owner", "test")
        inst.created_by = kwargs.get("created_by", "test")
        inst.source_run_id = kwargs.get("source_run_id")
        self.instances[inst.id] = inst
        self._fingerprint_index[inst.fingerprint] = inst.id
        return inst

    def get_instance(self, inst_id):
        return self.instances.get(inst_id)

    def get_instance_by_fingerprint(self, fingerprint):
        iid = self._fingerprint_index.get(fingerprint)
        return self.instances.get(iid) if iid else None

    def list_column_assets(self, db, sch, tbl):
        return []

    # ── findings + lifecycle
    def find_open_finding(self, instance_id, asset_id):
        for f in self.findings.values():
            if (f.instance_id == instance_id and f.asset_id == asset_id
                    and f.status in ("open", "reopened")):
                return f
        return None

    def find_recently_resolved_finding(self, instance_id, asset_id, within_days=7):
        window = datetime.datetime.utcnow() - datetime.timedelta(days=within_days)
        candidates = [
            f for f in self.findings.values()
            if f.instance_id == instance_id and f.asset_id == asset_id
            and f.status in ("resolved",)
            and (f.resolved_at or datetime.datetime.utcnow()) >= window
        ]
        candidates.sort(key=lambda f: f.resolved_at or datetime.datetime.min, reverse=True)
        return candidates[0] if candidates else None

    def get_finding(self, finding_id):
        return self.findings.get(finding_id)

    def apply_finding_update(self, finding_id, scan_id, fail_count, total_count,
                              severity=None, evidence=None):
        f = self.findings[finding_id]
        f.last_seen_at = datetime.datetime.utcnow()
        f.last_scan_id = scan_id
        f.current_fail_count = fail_count
        f.current_total_count = total_count
        f.fail_history = list(f.fail_history or []) + [{
            "scan_id": scan_id, "at": datetime.datetime.utcnow().isoformat(),
            "fail_count": fail_count, "total_count": total_count,
        }]
        if len(f.fail_history) > 50:
            f.fail_history = f.fail_history[-50:]
        if severity:
            f.severity = severity
        if evidence is not None:
            f.evidence = evidence
        return f

    def auto_resolve_finding(self, finding_id, scan_id):
        f = self.findings[finding_id]
        f.status = "resolved"
        f.resolved_at = datetime.datetime.utcnow()
        f.last_scan_id = scan_id
        f.resolution_notes = f.resolution_notes or "Auto-resolved by rescan"
        return f

    def reopen_finding(self, finding_id, scan_id, fail_count, total_count,
                       severity=None, evidence=None):
        f = self.findings[finding_id]
        f.status = "reopened"
        f.reopened_count = (f.reopened_count or 0) + 1
        f.last_seen_at = datetime.datetime.utcnow()
        f.last_scan_id = scan_id
        f.current_fail_count = fail_count
        f.current_total_count = total_count
        f.resolved_at = None
        f.closed_at = None
        f.resolution_notes = None
        f.fail_history = list(f.fail_history or []) + [{
            "scan_id": scan_id, "at": datetime.datetime.utcnow().isoformat(),
            "fail_count": fail_count, "total_count": total_count, "event": "reopened",
        }]
        if severity:
            f.severity = severity
        if evidence is not None:
            f.evidence = evidence
        return f

    def create_finding_with_lifecycle(self, asset_id, scan_id, instance_id,
                                       title, description, severity,
                                       context, evidence, fail_count, total_count):
        f = make_finding(
            instance_id=instance_id, asset_id=asset_id, scan_id=scan_id,
            severity=severity, fail_count=fail_count, total_count=total_count,
        )
        f.title = title
        f.description = description
        f.context = context or {}
        f.evidence = evidence or {}
        f.fail_history = [{
            "scan_id": scan_id, "at": datetime.datetime.utcnow().isoformat(),
            "fail_count": fail_count, "total_count": total_count,
        }]
        self.findings[f.id] = f
        return f

    # ── mutes
    def is_muted(self, instance_id, asset_id):
        key = f"{instance_id}|{asset_id}"
        m = self.mutes.get(key)
        if not m:
            return False
        return m["muted_until"] > datetime.datetime.utcnow()

    def create_mute(self, instance_id, asset_id, muted_until,
                     reason=None, muted_by=None):
        key = f"{instance_id}|{asset_id}"
        self.mutes[key] = {
            "instance_id": instance_id, "asset_id": asset_id,
            "muted_until": muted_until, "reason": reason, "muted_by": muted_by,
        }
        return SimpleNamespace(**self.mutes[key], id=key)

    def delete_mute(self, mute_id):
        self.mutes.pop(mute_id, None)

    # ── misc
    def log_critique_drop(self, run_id, table_fqn, proposal, scores,
                          mean_score, drop_reason):
        self.critique_drops.append({
            "run_id": run_id, "table_fqn": table_fqn, "proposal": proposal,
            "scores": scores, "mean_score": mean_score, "drop_reason": drop_reason,
        })

    def get_intelligence_logs_for_table(self, fqn, limit=3):
        return [l for l in self.intelligence_logs if l.get("table_fqn") == fqn][:limit]

    def get_feedback_memo(self, table, ttype):
        return None

    def get_review_lessons_for_table(self, fqn, limit=20):
        return []

    def search_similar_intelligence(self, fqn, limit=3):
        return []

    def _sha256(self, s):
        import hashlib
        return hashlib.sha256(s.encode()).hexdigest()


@pytest.fixture
def fake_storage():
    return FakeStorage()


# ── fake data source ──────────────────────────────────────────────────────

class FakeSource:
    """Stand-in for the Postgres/Snowflake adapters in the datasource
    registry. Configurable query results."""

    def __init__(self, query_results: Optional[List[List[Dict]]] = None):
        self.query_results = list(query_results) if query_results else []
        self.queries_executed: List[str] = []
        self.top_values_map: Dict[str, List[Dict]] = {}
        self.bottom_values_map: Dict[str, List[Dict]] = {}

    def query(self, sql: str, *args, **kwargs) -> List[Dict[str, Any]]:
        self.queries_executed.append(sql)
        if self.query_results:
            return self.query_results.pop(0)
        return []

    def top_values(self, db, sch, tbl, col, limit=10):
        return self.top_values_map.get(col, [])

    def bottom_values(self, db, sch, tbl, col, limit=10):
        return self.bottom_values_map.get(col, [])

    def execute(self, sql, *args, **kwargs):
        self.queries_executed.append(sql)


@pytest.fixture
def fake_source():
    return FakeSource()
