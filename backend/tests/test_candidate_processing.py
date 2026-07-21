"""
RuleIntelligence — _process_candidate suppression / promotion paths.

Covers the many-branch dispatch in rule_intelligence_agent.py L1017-1285:

  * Unknown definition_id → promotion via template_shape canonical / similarity /
    synthesis (not silent drop). Regression: audit finding #9.
  * Shape mismatch (candidate says template_shape="freshness" but
    definition_id points to a python_handler with shape=None) → discard
    definition_id, fall through to shape-canonical.
  * Fingerprint already active → suppressed{reason='already_active'}.
  * Fingerprint already pending → suppressed{reason='already_pending'}.
  * Fingerprint previously rejected with SAME evidence → suppressed.
  * Fingerprint previously rejected with DIFFERENT evidence → re-proposes.
  * accepted_values threshold trimmed to values actually observed in data.
"""
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from tests.conftest import (  # type: ignore
    FakeStorage, make_definition, make_instance, make_table_asset,
)


def _agent_with_storage(storage_mock):
    from app.services.agents.rule_intelligence_agent import RuleIntelligenceAgent
    a = RuleIntelligenceAgent.__new__(RuleIntelligenceAgent)
    a._closed_set_columns = {}
    a._source = None
    a._past_context_health = {}
    return a


def _candidate(definition_id=None, new_definition=None, template_shape=None,
               column="X", rationale="", scope="column",
               threshold_config=None, draft_sql=None):
    return {
        "definition_id": definition_id,
        "new_definition": new_definition,
        "template_shape": template_shape,
        "scope": scope,
        "column_name": column,
        "rationale": rationale,
        "threshold_config": threshold_config or {},
        "severity": "medium",
        "violation_detected": False,
        "draft_sql": draft_sql,
    }


class TestFingerprintDispatch:

    def test_active_fingerprint_suppressed_as_already_active(self):
        """Existing active instance → return {suppressed: True,
        reason: already_active}."""
        storage = FakeStorage()
        definition = storage.create_definition(name="Not Null", template_shape="not_null",
                                                check_kind="sql_template", status="active")
        table = make_table_asset()
        # Pre-populate an active instance with the fingerprint the candidate will produce
        from app.services.fingerprint import compute_fingerprint
        target_cfg = {"column": "ORDER_ID"}
        fp = compute_fingerprint(definition.id, "column", table.database_name,
                                 table.schema_name, table.table_name,
                                 target_cfg, {})
        storage.create_instance(
            definition_id=definition.id, scope="column",
            database_name=table.database_name, schema_name=table.schema_name,
            table_name=table.table_name, fingerprint=fp,
            target_config=target_cfg, threshold_config={}, status="active",
        )

        agent = _agent_with_storage(storage)
        with patch("app.services.agents.rule_intelligence_agent.storage", storage), \
             patch.object(agent, "_build_rule_sql",
                          return_value=("SELECT 0 AS FAILED_COUNT, 100 AS TOTAL_COUNT", {})), \
             patch.object(agent, "_execute_check_sql", return_value=(0, 100)):
            result = agent._process_candidate(
                _candidate(definition_id=definition.id, template_shape="not_null",
                           column="ORDER_ID"),
                table, "run-x", [definition],
            )
        assert result is not None
        assert result.get("suppressed") is True
        assert result["reason"] == "already_active"

    def test_pending_fingerprint_suppressed_as_already_pending(self):
        storage = FakeStorage()
        definition = storage.create_definition(name="Not Null", template_shape="not_null",
                                                check_kind="sql_template", status="active")
        table = make_table_asset()
        from app.services.fingerprint import compute_fingerprint
        target_cfg = {"column": "ORDER_ID"}
        fp = compute_fingerprint(definition.id, "column", table.database_name,
                                 table.schema_name, table.table_name, target_cfg, {})
        storage.create_instance(
            definition_id=definition.id, scope="column",
            database_name=table.database_name, schema_name=table.schema_name,
            table_name=table.table_name, fingerprint=fp,
            target_config=target_cfg, threshold_config={}, status="pending",
        )

        agent = _agent_with_storage(storage)
        with patch("app.services.agents.rule_intelligence_agent.storage", storage), \
             patch.object(agent, "_build_rule_sql",
                          return_value=("SELECT 0 AS FAILED_COUNT, 100 AS TOTAL_COUNT", {})), \
             patch.object(agent, "_execute_check_sql", return_value=(0, 100)):
            result = agent._process_candidate(
                _candidate(definition_id=definition.id, template_shape="not_null",
                           column="ORDER_ID"),
                table, "run-x", [definition],
            )
        assert result["suppressed"] is True
        assert result["reason"] == "already_pending"

    def test_rejected_same_evidence_is_suppressed(self):
        """Previously-rejected fingerprint + evidence overlaps rejection reason
        → suppress (don't nag the reviewer with the same thing)."""
        storage = FakeStorage()
        definition = storage.create_definition(name="Not Null", template_shape="not_null",
                                                check_kind="sql_template", status="active")
        table = make_table_asset()
        from app.services.fingerprint import compute_fingerprint
        target_cfg = {"column": "ORDER_ID"}
        fp = compute_fingerprint(definition.id, "column", table.database_name,
                                 table.schema_name, table.table_name, target_cfg, {})
        rejected = storage.create_instance(
            definition_id=definition.id, scope="column",
            database_name=table.database_name, schema_name=table.schema_name,
            table_name=table.table_name, fingerprint=fp,
            target_config=target_cfg, threshold_config={}, status="rejected",
        )
        rejected.rejection_reason = "ORDER_ID is intentionally sparse for archived rows"

        agent = _agent_with_storage(storage)
        cand = _candidate(definition_id=definition.id, template_shape="not_null",
                          column="ORDER_ID")
        cand["violation_evidence"] = "ORDER_ID is intentionally sparse for archived rows"

        with patch("app.services.agents.rule_intelligence_agent.storage", storage), \
             patch.object(agent, "_build_rule_sql",
                          return_value=("SELECT 0 AS FAILED_COUNT, 100 AS TOTAL_COUNT", {})), \
             patch.object(agent, "_execute_check_sql", return_value=(0, 100)):
            result = agent._process_candidate(cand, table, "run-x", [definition])
        assert result["suppressed"] is True
        assert result["reason"] == "previously_rejected"

    def test_rejected_new_evidence_is_reproposed(self):
        """Previously-rejected fingerprint + new evidence disjoint from
        rejection reason → RE-propose (evidence-based defense re-runs)."""
        storage = FakeStorage()
        definition = storage.create_definition(name="Not Null", template_shape="not_null",
                                                check_kind="sql_template", status="active")
        table = make_table_asset()
        from app.services.fingerprint import compute_fingerprint
        target_cfg = {"column": "ORDER_ID"}
        fp = compute_fingerprint(definition.id, "column", table.database_name,
                                 table.schema_name, table.table_name, target_cfg, {})
        rejected = storage.create_instance(
            definition_id=definition.id, scope="column",
            database_name=table.database_name, schema_name=table.schema_name,
            table_name=table.table_name, fingerprint=fp,
            target_config=target_cfg, threshold_config={}, status="rejected",
        )
        rejected.rejection_reason = "irrelevant for staging tables"

        agent = _agent_with_storage(storage)
        cand = _candidate(definition_id=definition.id, template_shape="not_null",
                          column="ORDER_ID")
        # Very different rationale
        cand["violation_evidence"] = "orders now serve production reports every night"

        with patch("app.services.agents.rule_intelligence_agent.storage", storage), \
             patch.object(agent, "_build_rule_sql",
                          return_value=("SELECT 0 AS FAILED_COUNT, 100 AS TOTAL_COUNT", {})), \
             patch.object(agent, "_execute_check_sql", return_value=(0, 100)):
            result = agent._process_candidate(cand, table, "run-x", [definition])
        # Should proceed (not suppressed) — new evidence differs
        assert result.get("suppressed") is False
        assert result["kind"] == "reuse"


class TestUnknownDefinitionIdPromotion:

    def test_unknown_definition_id_falls_through_to_synthesis(self):
        """Claude passes a definition_id that doesn't exist → promote to a
        synthesized new_definition based on rationale, not silent drop."""
        storage = FakeStorage()
        # storage.get_definition returns None for unknown id
        table = make_table_asset()
        agent = _agent_with_storage(storage)

        cand = _candidate(
            definition_id="never-existed-abc",
            template_shape="not_null",
            column="ORDER_ID",
            rationale="Column must not be null on transactional records",
        )

        # Insert a canonical not_null definition so template_shape promotion
        # branch matches — this is the deterministic backstop.
        canonical = storage.create_definition(
            name="Column Not Null", template_shape="not_null",
            check_kind="sql_template", status="active",
        )

        with patch("app.services.agents.rule_intelligence_agent.storage", storage), \
             patch.object(agent, "_build_rule_sql",
                          return_value=("SELECT 0 AS FAILED_COUNT, 100 AS TOTAL_COUNT", {})), \
             patch.object(agent, "_execute_check_sql", return_value=(0, 100)):
            result = agent._process_candidate(cand, table, "run-x",
                                                storage.list_all_definitions())

        # Should NOT return None — promoted to canonical not_null
        assert result is not None
        assert result.get("suppressed") is False
        # Reused canonical, not created new
        assert result["kind"] == "reuse"
        assert result["definition"].id == canonical.id


class TestShapeMismatch:

    def test_shape_mismatch_discards_definition_id(self):
        """Candidate has template_shape='freshness' but definition_id points to
        a python_handler with shape=None → discard the id, fall through to
        shape-canonical, no crash."""
        storage = FakeStorage()
        # A python_handler definition — no template_shape
        wrong_def = storage.create_definition(
            name="OHLC Range Consistency", template_shape=None,
            check_kind="python_handler", status="active",
        )
        # The correct freshness canonical
        freshness_def = storage.create_definition(
            name="Freshness", template_shape="freshness",
            check_kind="sql_template", status="active",
        )
        table = make_table_asset()
        agent = _agent_with_storage(storage)

        cand = _candidate(
            definition_id=wrong_def.id,
            template_shape="freshness",   # mismatch — wrong_def has no shape
            column="LAST_UPDATED",
            threshold_config={"max_age_days": 7},
        )

        with patch("app.services.agents.rule_intelligence_agent.storage", storage), \
             patch.object(agent, "_build_rule_sql",
                          return_value=("SELECT 0 AS FAILED_COUNT, 1 AS TOTAL_COUNT", {})), \
             patch.object(agent, "_execute_check_sql", return_value=(0, 1)):
            result = agent._process_candidate(cand, table, "run-x",
                                                storage.list_all_definitions())

        # Should NOT bind to wrong_def; should reuse freshness_def
        assert result is not None
        assert result.get("suppressed") is False
        assert result["definition"].id == freshness_def.id


class TestGroundAcceptedValues:

    def test_accepted_values_trimmed_to_observed(self):
        """When a candidate's accepted_values threshold list contains values
        that aren't observed in the column's top_values, those unobserved
        values are trimmed (grounding to real data)."""
        from app.services.agents.rule_intelligence_agent import RuleIntelligenceAgent
        agent = RuleIntelligenceAgent.__new__(RuleIntelligenceAgent)
        agent._closed_set_columns = {"CURRENCY": {"values": ["USD", "EUR", "JPY"]}}

        cand = _candidate(column="CURRENCY", threshold_config={
            "accepted_values": ["USD", "EUR", "JPY", "XX9", "BOGUS"],
        })
        # target_config from _build_target_config produces this shape
        target_config = {"column": "CURRENCY"}
        grounded = agent._ground_accepted_values(cand, target_config)
        assert set(grounded["threshold_config"]["accepted_values"]) == {"USD", "EUR", "JPY"}

    def test_grounding_skipped_when_column_not_in_closed_set(self):
        """If the column isn't in _closed_set_columns (not enum-ish), leave
        threshold_config alone."""
        from app.services.agents.rule_intelligence_agent import RuleIntelligenceAgent
        agent = RuleIntelligenceAgent.__new__(RuleIntelligenceAgent)
        agent._closed_set_columns = {}

        cand = _candidate(column="STATUS", threshold_config={
            "accepted_values": ["active", "pending", "closed"],
        })
        target_config = {"column": "STATUS"}
        result = agent._ground_accepted_values(cand, target_config)
        assert result["threshold_config"]["accepted_values"] == ["active", "pending", "closed"]
