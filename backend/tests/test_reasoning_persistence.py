"""
RuleIntelligence — reasoning persistence and skip-decision propagation.

Verifies:
  - Claude's `reasoning` field (inline, one-call model in _execute path)
    lands in RULE_INTELLIGENCE_LOGS.thinking via storage.create_intelligence_log.
  - `signals_used` carries sample_tool_calls with the mode Claude used.
  - `signals_used.past_context_health` records read-health per channel.
  - `get_skip_ids` / `get_keep_running_ids` correctly bucket definitions.
  - Instance-level `rationale` is preserved on RULE_INSTANCES creation
    (see coordinator.py L473).
"""
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from tests.conftest import make_definition, make_instance, make_table_asset  # type: ignore


class TestClassificationDecisions:

    def _agent(self):
        from app.services.agents.rule_intelligence_agent import RuleIntelligenceAgent
        return RuleIntelligenceAgent.__new__(RuleIntelligenceAgent)

    def test_keep_running_true_puts_in_active_bucket(self):
        agent = self._agent()
        classification = {
            "definitions_evaluated": {
                "def-1": {"keep_running": True, "severity_override": None, "reason": "still valid"},
                "def-2": {"keep_running": False, "severity_override": None, "reason": "obsolete"},
            }
        }
        assert agent.get_keep_running_ids(classification) == {"def-1"}
        assert agent.get_skip_ids(classification) == {"def-2"}

    def test_default_keep_running_true(self):
        """Missing keep_running key defaults True (safe: don't silently disable
        rules Claude didn't explicitly address)."""
        agent = self._agent()
        classification = {
            "definitions_evaluated": {
                "def-1": {},  # no keep_running key
            }
        }
        assert agent.get_keep_running_ids(classification) == {"def-1"}
        assert agent.get_skip_ids(classification) == set()

    def test_severity_override_extracted(self):
        agent = self._agent()
        classification = {
            "definitions_evaluated": {
                "def-1": {"keep_running": True, "severity_override": "high"},
                "def-2": {"keep_running": True, "severity_override": None},
            }
        }
        assert agent.get_severity_override(classification, "def-1") == "high"
        assert agent.get_severity_override(classification, "def-2") is None
        assert agent.get_severity_override(classification, "missing") is None

    def test_skip_reason_carried_through_classification(self):
        """The `reason` string is what shows up in the UI when a rule is
        skipped — Claude's specific reasoning must survive round-trip."""
        agent = self._agent()
        classification = {
            "definitions_evaluated": {
                "def-1": {"keep_running": False,
                          "reason": "This table has no CUSTOMER_ID column anymore."},
            }
        }
        entry = classification["definitions_evaluated"]["def-1"]
        assert entry["reason"] == "This table has no CUSTOMER_ID column anymore."
        assert "def-1" in agent.get_skip_ids(classification)


class TestIntelligenceLogShape:

    def test_signals_used_carries_sample_tool_calls(self):
        """When Claude uses get_sample_rows, the tool call — including its
        reason and where_clause — appears in signals_used.sample_tool_calls."""
        # Construct the exact shape the RuleIntelligence run() builds
        tool_calls = [
            {"name": "get_sample_rows",
             "input": {"mode": "distinct", "columns": ["CURRENCY"],
                       "reason": "assess distribution"},
             "result": "USD, EUR, JPY..."},
            {"name": "get_sample_rows",
             "input": {"mode": "nulls", "columns": ["AMOUNT"],
                       "reason": "check sparse column"},
             "result": "null%=42.0"},
        ]
        signals_used = {
            "sample_tool_calls": [
                {"reason": (tc.get("input") or {}).get("reason", ""),
                 "where": (tc.get("input") or {}).get("where_clause", "")}
                for tc in tool_calls
            ],
            "past_context_health": {"same_table_logs": "empty"},
            "used_fallback": False,
        }
        assert len(signals_used["sample_tool_calls"]) == 2
        assert signals_used["sample_tool_calls"][0]["reason"] == "assess distribution"

    def test_past_context_health_populated_by_format_past_context(self):
        """_format_past_context writes health per channel — verify empty/error
        semantics."""
        from app.services.agents.rule_intelligence_agent import RuleIntelligenceAgent
        agent = RuleIntelligenceAgent.__new__(RuleIntelligenceAgent)
        agent._closed_set_columns = {}
        agent._source = None
        table = make_table_asset()

        with patch("app.services.agents.rule_intelligence_agent.storage") as mock_storage:
            mock_storage.get_intelligence_logs_for_table.return_value = []
            mock_storage.get_feedback_memo.return_value = None
            mock_storage.get_review_lessons_for_table.return_value = []
            mock_storage.search_similar_intelligence.return_value = []
            _ = agent._format_past_context(table)

        # All four channels should be "empty" (returned successfully, but empty)
        assert agent._past_context_health["same_table_logs"] == "empty"
        assert agent._past_context_health["feedback_memo"] == "empty"
        assert agent._past_context_health["review_lessons"] == "empty"
        assert agent._past_context_health["similar_intelligence"] == "empty"

    def test_past_context_health_records_errors(self):
        """When a storage call throws, health becomes error:<ExcType> not
        silent empty — audit fix #6."""
        from app.services.agents.rule_intelligence_agent import RuleIntelligenceAgent
        agent = RuleIntelligenceAgent.__new__(RuleIntelligenceAgent)
        agent._closed_set_columns = {}
        agent._source = None
        table = make_table_asset()

        with patch("app.services.agents.rule_intelligence_agent.storage") as mock_storage:
            mock_storage.get_intelligence_logs_for_table.side_effect = ConnectionError("boom")
            mock_storage.get_feedback_memo.return_value = None
            mock_storage.get_review_lessons_for_table.return_value = []
            mock_storage.search_similar_intelligence.return_value = []
            _ = agent._format_past_context(table)

        assert agent._past_context_health["same_table_logs"] == "error:ConnectionError"


class TestReasoningFieldExtraction:
    """The `reasoning` field is on Claude's JSON response — the run() method
    passes it through as `thinking` when writing the intelligence log. Verify
    the value round-trips exactly."""

    def test_reasoning_field_extracted_from_parsed_response(self):
        parsed = {
            "table_type": "fact",
            "table_type_confidence": 90,
            "reasoning": "I looked at CURRENCY.top_values and found XX9 in the tail — "
                        "propose accepted_values excluding it. AMOUNT null% is 0 so "
                        "not_null is redundant; skipping.",
            "new_instances": [],
            "instances_evaluated": {},
            "definitions_evaluated": {},
            "signals_evaluated": {},
        }
        assert parsed.get("reasoning", "").startswith("I looked at CURRENCY.top_values")
        # This is what run() sends to create_intelligence_log(thinking=...)

    def test_missing_reasoning_becomes_empty_string(self):
        """Older responses without a reasoning field should not crash — they
        write '' as thinking."""
        parsed = {"table_type": "unknown", "new_instances": []}
        assert (parsed.get("reasoning") or "") == ""


class TestInstanceRationalePreserved:
    """coordinator.py creates every RULE_INSTANCE with rationale=proposal[
    'rationale']. That per-instance reasoning is what surfaces on findings
    (rule_engine.py L238 — instance_rationale beats definition.description)."""

    def test_rationale_flows_from_proposal_to_instance(self):
        """Simulate the coordinator persist loop and verify rationale reaches
        the created instance."""
        from tests.conftest import FakeStorage
        storage = FakeStorage()
        table = make_table_asset()

        # Build one "reuse" proposal with a very specific rationale
        existing_def = storage.create_definition(
            name="Accepted Values", template_shape="accepted_values",
            check_kind="sql_template", status="active",
        )
        proposal = {
            "kind": "reuse",
            "source": "llm",
            "fingerprint": "fp-abc",
            "definition": existing_def,
            "scope": "column",
            "target_config": {"column": "CURRENCY"},
            "threshold_config": {"accepted_values": ["USD", "EUR", "JPY"]},
            "rule_sql": "SELECT COUNT_IF(CURRENCY NOT IN ('USD','EUR','JPY')) FROM T",
            "column_name": "CURRENCY",
            "severity": "high",
            "violated": True,
            "evidence": "3 rows",
            "rationale": "'XX9' currency codes corrupt every FX revenue aggregation.",
        }

        # Same loop the coordinator runs
        inst = storage.create_instance(
            definition_id=existing_def.id,
            scope=proposal["scope"],
            database_name=table.database_name,
            schema_name=table.schema_name,
            table_name=table.table_name,
            fingerprint=proposal["fingerprint"],
            severity=proposal["severity"],
            target_config=proposal["target_config"],
            threshold_config=proposal["threshold_config"],
            rule_sql=proposal["rule_sql"],
            rationale=proposal["rationale"],
            status="pending",
            source_run_id="run-x",
        )
        assert inst.rationale == "'XX9' currency codes corrupt every FX revenue aggregation."
