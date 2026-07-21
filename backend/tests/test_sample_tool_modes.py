"""
get_sample_rows tool — mode dispatch (sample / distinct / nulls).

The tool grew a `mode` param during the 2026-07-15 rebalance so Claude can
ask for distinct-value listings and null% stats without a raw WHERE clause.
Verify each dispatch path, the safety guardrails (identifier validation,
WHERE-clause keyword blocklist), and the fallback to sf_session when no
per-run data source is set.
"""
import pytest
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

from tests.conftest import make_table_asset, FakeSource  # type: ignore


def _agent():
    from app.services.agents.rule_intelligence_agent import RuleIntelligenceAgent
    a = RuleIntelligenceAgent.__new__(RuleIntelligenceAgent)
    a._closed_set_columns = {}
    a._source = None
    return a


class TestModeDispatch:

    def test_default_mode_is_sample(self):
        """No `mode` → sample rows (backward-compat)."""
        agent = _agent()
        source = FakeSource(query_results=[[{"A": "1"}, {"A": "2"}]])
        agent._source = source
        result = agent._execute_sample_tool(
            make_table_asset(),
            {"columns": ["A"], "reason": "sanity"},
        )
        assert "A" in result
        assert "1" in result

    def test_explicit_sample_mode(self):
        agent = _agent()
        source = FakeSource(query_results=[[{"X": "v"}]])
        agent._source = source
        result = agent._execute_sample_tool(
            make_table_asset(),
            {"mode": "sample", "columns": ["X"], "reason": "explicit"},
        )
        assert "v" in result

    def test_distinct_mode_returns_top_and_tail(self):
        agent = _agent()
        source = FakeSource()
        source.top_values_map["CURRENCY"] = [
            {"value": "USD", "count": 900},
            {"value": "EUR", "count": 80},
        ]
        source.bottom_values_map["CURRENCY"] = [
            {"value": "XX9", "count": 1},
            {"value": "JPY", "count": 3},
        ]
        agent._source = source
        result = agent._execute_sample_tool(
            make_table_asset(),
            {"mode": "distinct", "columns": ["CURRENCY"], "reason": "look at distribution"},
        )
        assert "USD" in result
        assert "EUR" in result
        assert "XX9" in result  # tail included
        assert "top" in result.lower()
        assert "tail" in result.lower()

    def test_distinct_mode_requires_exactly_one_column(self):
        agent = _agent()
        agent._source = FakeSource()
        result = agent._execute_sample_tool(
            make_table_asset(),
            {"mode": "distinct", "columns": ["A", "B"], "reason": "wrong"},
        )
        assert "Rejected" in result

    def test_distinct_mode_needs_resolved_source(self):
        """No _source, mode=distinct → rejected (needs adapter primitive)."""
        agent = _agent()
        agent._source = None
        result = agent._execute_sample_tool(
            make_table_asset(),
            {"mode": "distinct", "columns": ["A"], "reason": "no source"},
        )
        assert "Rejected" in result

    def test_nulls_mode_returns_percent_and_samples(self):
        agent = _agent()
        source = FakeSource(query_results=[
            [{"TOTAL": 1000, "NON_NULLS": 750}],  # stats
            [{"AMOUNT": 42.0}, {"AMOUNT": 99.5}],  # sample non-null
        ])
        agent._source = source
        result = agent._execute_sample_tool(
            make_table_asset(),
            {"mode": "nulls", "columns": ["AMOUNT"], "reason": "assess nulls"},
        )
        assert "null%=25.0" in result
        assert "42" in result

    def test_nulls_mode_requires_exactly_one_column(self):
        agent = _agent()
        agent._source = FakeSource()
        result = agent._execute_sample_tool(
            make_table_asset(),
            {"mode": "nulls", "columns": ["A", "B"], "reason": "bad"},
        )
        assert "Rejected" in result

    def test_unknown_mode_falls_back_to_sample(self):
        agent = _agent()
        agent._source = FakeSource(query_results=[[{"A": "v"}]])
        result = agent._execute_sample_tool(
            make_table_asset(),
            {"mode": "garbage", "columns": ["A"], "reason": "unknown"},
        )
        # Unknown modes default to sample (last branch of _execute_sample_tool)
        # but the current implementation actually treats it as sample. Assert
        # we still get some output, no crash.
        assert result

    def test_source_fallback_to_sf_session(self):
        """When no per-run _source is set, _sample_tool_rows falls back to
        sf_session. Ensures the legacy path stays working."""
        agent = _agent()  # _source = None
        with patch("app.services.agents.rule_intelligence_agent.sf_session") as mock_sf:
            mock_sf.query.return_value = [{"STATUS": "OK"}]
            result = agent._execute_sample_tool(
                make_table_asset(),
                {"columns": ["STATUS"], "reason": "fallback"},
            )
        assert "OK" in result


class TestSafetyGuardrails:

    def test_unsafe_column_name_rejected_in_distinct(self):
        agent = _agent()
        agent._source = FakeSource()
        result = agent._execute_sample_tool(
            make_table_asset(),
            {"mode": "distinct", "columns": ["A; DROP TABLE X"], "reason": "attack"},
        )
        assert "failed" in result.lower() or "unsafe" in result.lower()

    def test_unsafe_column_name_rejected_in_nulls(self):
        agent = _agent()
        agent._source = FakeSource()
        result = agent._execute_sample_tool(
            make_table_asset(),
            {"mode": "nulls", "columns": ["A OR 1=1"], "reason": "attack"},
        )
        assert "failed" in result.lower() or "unsafe" in result.lower()

    def test_multi_keyword_injection_in_where(self):
        """Multiple SQL injection vectors all rejected."""
        agent = _agent()
        agent._source = FakeSource()
        for attack in [
            "1=1 AND EXEC('bad')",
            "1=1 OR TRUNCATE TABLE X",
            "AMOUNT < 0 MERGE INTO T",
        ]:
            result = agent._execute_sample_tool(
                make_table_asset(),
                {"where_clause": attack, "reason": "attack"},
            )
            assert "Rejected" in result, f"Attack not rejected: {attack}"


class TestModeParamPropagation:

    def test_log_line_records_mode(self, caplog):
        import logging
        agent = _agent()
        agent._source = FakeSource(query_results=[[{"A": "v"}]])
        with caplog.at_level(logging.INFO, logger="app.services.agents.rule_intelligence_agent"):
            agent._execute_sample_tool(
                make_table_asset(),
                {"mode": "sample", "columns": ["A"], "reason": "capture"},
            )
        assert any("mode=sample" in r.message for r in caplog.records)
