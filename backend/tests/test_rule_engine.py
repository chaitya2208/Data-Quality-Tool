"""
Rule engine — sample-row / fail_count reconciliation + evidence contract.

The reconciliation guard (rule_engine.py L272-277) defends against buggy
Claude-authored count SQL: if the check SQL reports FAILED_COUNT=3 but we
actually fetched 7 failing sample rows, trust the samples and use 7 as the
failing count. Log a warning. This shielded the tool during the
uniqueness_sql regression (rule_sql_templates counted groups instead of rows).

Also verifies:
  - Every finding emits {fail_count, total_count, sample_rows}.
  - Zero-fail results (rule passed) return None (no finding).
  - Aggregate rules (freshness, row_count_*) are exempted from reconciliation.
"""
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from tests.conftest import (  # type: ignore
    make_definition, make_instance, make_table_asset,
)


def _asset_shape(table_asset):
    """Table asset needs asset_type + comment for RuleEngine."""
    table_asset.asset_type = "table"
    table_asset.comment = None
    return table_asset


def _make_engine():
    from app.services.rule_engine import RuleEngine
    return RuleEngine()


class TestDriftHandlerExclusion:
    """Drift handler_keys (column_added, column_removed, etc.) are executed by
    scan_service — NOT by RuleEngine._HANDLERS dispatch. Prior bug: RuleEngine
    was including drift instances in execute_rules, then logging a "No handler
    found" warning for each on every scan (5+ warnings per table per scan).
    Fix: filter DRIFT_HANDLER_KEYS out at execute_rules() alongside
    DYNAMIC_RULE_HANDLER_KEYS."""

    def test_drift_instances_filtered_out_of_execute_rules(self, caplog):
        import logging
        from app.services.rule_engine import RuleEngine
        engine = RuleEngine()

        drift_instance = make_instance()
        drift_instance.check_kind = "python_handler"
        drift_instance.handler_key = "column_added"
        drift_instance.code = "COLUMN_ADDED"

        table = _asset_shape(make_table_asset())

        with patch.object(engine, "get_active_instances", return_value=[drift_instance]), \
             caplog.at_level(logging.WARNING, logger="app.services.rule_engine"):
            findings = engine.execute_rules(table, "scan-1")

        # Zero findings (drift is filtered out), and zero "No handler found" warnings
        assert findings == []
        no_handler = [r for r in caplog.records if "No handler found" in r.message]
        assert not no_handler, (
            f"Expected drift instances to be silently skipped, "
            f"but got warnings: {[r.message for r in no_handler]}"
        )

    def test_all_five_drift_keys_filtered(self):
        from app.services.rule_engine import RuleEngine
        from app.services.schema_drift import DRIFT_HANDLER_KEYS
        engine = RuleEngine()

        drift_instances = []
        for handler_key in DRIFT_HANDLER_KEYS:
            inst = make_instance()
            inst.check_kind = "python_handler"
            inst.handler_key = handler_key
            inst.code = handler_key.upper()
            drift_instances.append(inst)

        table = _asset_shape(make_table_asset())

        with patch.object(engine, "get_active_instances", return_value=drift_instances):
            findings = engine.execute_rules(table, "scan-1")
        assert findings == []


class TestSqlInstanceExecution:

    def test_zero_fail_returns_none(self):
        engine = _make_engine()
        table = _asset_shape(make_table_asset())
        inst = make_instance(rule_sql="SELECT 0 AS FAILED_COUNT, 100 AS TOTAL_COUNT")
        definition = make_definition(check_kind="sql_template", template_shape="not_null",
                                     name="Non-Null")
        source = MagicMock()
        source.query.return_value = [{"FAILED_COUNT": 0, "TOTAL_COUNT": 100}]

        with patch("app.services.rule_engine.storage") as mock_storage:
            mock_storage.get_asset_by_fqn.return_value = None
            result = engine._execute_sql_instance(inst, definition, table, "scan-1",
                                                   source=source)
        assert result is None

    def test_nonzero_fail_returns_finding_with_evidence(self):
        engine = _make_engine()
        table = _asset_shape(make_table_asset())
        inst = make_instance()
        definition = make_definition(check_kind="sql_template", template_shape="not_null",
                                     name="Non-Null Check")
        source = MagicMock()
        source.query.side_effect = [
            [{"FAILED_COUNT": 3, "TOTAL_COUNT": 100}],
            [{"ORDER_ID": None}, {"ORDER_ID": None}, {"ORDER_ID": None}],
        ]

        with patch("app.services.rule_engine.storage") as mock_storage, \
             patch("app.services.rule_sql_templates.failing_rows_sample_sql") as mock_templates:
            mock_storage.get_asset_by_fqn.return_value = None
            mock_templates.return_value = "SELECT ORDER_ID FROM T"
            result = engine._execute_sql_instance(inst, definition, table, "scan-1",
                                                   source=source)

        assert result is not None
        ev = result["evidence"]
        assert ev["fail_count"] == 3
        assert ev["total_count"] == 100
        assert isinstance(ev["sample_rows"], list)
        assert result["asset_id"]
        assert result["instance_id"] == inst.id

    def test_reconciliation_trusts_samples_when_larger_than_count(self):
        """The regression case: check SQL returns FAILED_COUNT=2 but we fetched
        7 actual failing rows via failing_rows_sample_sql. The engine
        overrides fail_count with 7 and logs a warning."""
        engine = _make_engine()
        table = _asset_shape(make_table_asset())
        inst = make_instance()
        definition = make_definition(check_kind="sql_template",
                                     template_shape="uniqueness",  # not aggregate
                                     name="Uniqueness")
        source = MagicMock()
        source.query.side_effect = [
            [{"FAILED_COUNT": 2, "TOTAL_COUNT": 100}],
            [{"X": i} for i in range(7)],  # 7 rows > 2
        ]

        with patch("app.services.rule_engine.storage") as mock_storage, \
             patch("app.services.rule_sql_templates.failing_rows_sample_sql") as mock_templates:
            mock_storage.get_asset_by_fqn.return_value = None
            mock_templates.return_value = "SELECT X FROM T"
            result = engine._execute_sql_instance(inst, definition, table, "scan-1",
                                                   source=source)
        assert result["evidence"]["fail_count"] == 7

    def test_reconciliation_skipped_for_aggregate_rules(self):
        """Aggregate rules (freshness, row_count_*) report FAILED_COUNT=1 as a
        sentinel and produce no per-row samples. Don't override."""
        engine = _make_engine()
        table = _asset_shape(make_table_asset())
        inst = make_instance()
        definition = make_definition(check_kind="sql_template",
                                     template_shape="freshness",
                                     name="Freshness")
        source = MagicMock()
        # is_aggregate := (total==1 AND template_shape in aggregate list). Have
        # to satisfy total=1 too.
        source.query.side_effect = [
            [{"FAILED_COUNT": 1, "TOTAL_COUNT": 1}],
            [{"ANY": 1}, {"ANY": 2}, {"ANY": 3}],  # 3 samples > 1
        ]

        with patch("app.services.rule_engine.storage") as mock_storage, \
             patch("app.services.rule_sql_templates.failing_rows_sample_sql") as mock_templates:
            mock_storage.get_asset_by_fqn.return_value = None
            mock_templates.return_value = "SELECT ANY FROM T"
            result = engine._execute_sql_instance(inst, definition, table, "scan-1",
                                                   source=source)
        # NOT reconciled — aggregate rule
        assert result["evidence"]["fail_count"] == 1

    def test_sample_fetch_failure_does_not_lose_finding(self):
        """If sample_sql throws, we still return the finding with fail_count,
        just without samples. Sample fetch is best-effort."""
        engine = _make_engine()
        table = _asset_shape(make_table_asset())
        inst = make_instance()
        definition = make_definition(check_kind="sql_template",
                                     template_shape="not_null",
                                     name="Non-Null")
        source = MagicMock()
        source.query.side_effect = [
            [{"FAILED_COUNT": 5, "TOTAL_COUNT": 100}],
            Exception("sample-query blew up"),
        ]

        with patch("app.services.rule_engine.storage") as mock_storage, \
             patch("app.services.rule_sql_templates.failing_rows_sample_sql") as mock_templates:
            mock_storage.get_asset_by_fqn.return_value = None
            mock_templates.return_value = "SELECT X FROM T"
            result = engine._execute_sql_instance(inst, definition, table, "scan-1",
                                                   source=source)
        # Finding still produced even though samples failed
        assert result is not None
        assert result["evidence"]["fail_count"] == 5
        assert result["evidence"]["sample_rows"] == []

    def test_missing_failed_count_key_returns_none(self):
        engine = _make_engine()
        table = _asset_shape(make_table_asset())
        inst = make_instance()
        definition = make_definition()
        source = MagicMock()
        source.query.return_value = [{"BOGUS": 1}]

        with patch("app.services.rule_engine.storage"):
            result = engine._execute_sql_instance(inst, definition, table, "scan-1",
                                                   source=source)
        assert result is None

    def test_lowercase_keys_from_postgres(self):
        """Postgres returns lowercase column names — the engine must accept
        both 'FAILED_COUNT' (Snowflake) and 'failed_count' (Postgres)."""
        engine = _make_engine()
        table = _asset_shape(make_table_asset())
        inst = make_instance()
        definition = make_definition(check_kind="sql_template",
                                     template_shape="not_null",
                                     name="Non-Null")
        source = MagicMock()
        source.query.side_effect = [
            [{"failed_count": 5, "total_count": 100}],
            [{"x": None}],
        ]

        with patch("app.services.rule_engine.storage") as mock_storage, \
             patch("app.services.rule_sql_templates.failing_rows_sample_sql") as mock_templates:
            mock_storage.get_asset_by_fqn.return_value = None
            mock_templates.return_value = "SELECT x FROM T"
            result = engine._execute_sql_instance(inst, definition, table, "scan-1",
                                                   source=source)
        assert result is not None
        assert result["evidence"]["fail_count"] == 5

    def test_rationale_prefers_instance_over_definition(self):
        """Per-instance rationale (specific: "'XX9' corrupts revenue") beats
        the generic library description on the finding description."""
        engine = _make_engine()
        table = _asset_shape(make_table_asset())
        inst = make_instance(rationale="'XX9' currency codes are unmapped")
        definition = make_definition(
            check_kind="sql_template", template_shape="accepted_values",
            name="Accepted Values", description="Generic library description.")
        source = MagicMock()
        source.query.side_effect = [
            [{"FAILED_COUNT": 2, "TOTAL_COUNT": 100}],
            [{"CURRENCY": "XX9"}],
        ]

        with patch("app.services.rule_engine.storage") as mock_storage, \
             patch("app.services.rule_sql_templates.failing_rows_sample_sql") as mock_templates:
            mock_storage.get_asset_by_fqn.return_value = None
            mock_templates.return_value = "SELECT CURRENCY FROM T"
            result = engine._execute_sql_instance(inst, definition, table, "scan-1",
                                                   source=source)
        assert "XX9" in result["description"]
        assert "Generic library description" not in result["description"]

    def test_evidence_contract_shape_always_present(self):
        """Every returned finding must carry all three evidence keys."""
        engine = _make_engine()
        table = _asset_shape(make_table_asset())
        inst = make_instance()
        definition = make_definition(check_kind="sql_template",
                                     template_shape="not_null", name="X")
        source = MagicMock()
        source.query.side_effect = [
            [{"FAILED_COUNT": 1, "TOTAL_COUNT": 10}],
            [],
        ]
        with patch("app.services.rule_engine.storage") as mock_storage, \
             patch("app.services.rule_sql_templates.failing_rows_sample_sql") as mock_templates:
            mock_storage.get_asset_by_fqn.return_value = None
            mock_templates.return_value = "SELECT 1"
            result = engine._execute_sql_instance(inst, definition, table, "scan-1",
                                                   source=source)
        assert set(["fail_count", "total_count", "sample_rows"]).issubset(result["evidence"].keys())
