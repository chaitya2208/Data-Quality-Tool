"""
Tests for the four agentic features added to RuleIntelligenceAgent:
  1. ask_claude_agentic — tool-use loop (multi-round, tool results fed back)
  2. _execute_sample_tool — SQL safety guardrails
  3. _self_critique_proposals — weak proposals dropped by score
  4. _repair_draft_sql — invalid SQL gets a repair call

All tests are fully offline — no Snowflake connection, no AWS/Bedrock call.
Real collaborators (sf_session, ask_claude, ask_claude_agentic) are patched.
"""
import json
import types
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, call


# ── helpers ──────────────────────────────────────────────────────────────────

def _fake_table(fqn="DB.SCH.ORDERS", row_count=50000):
    parts = fqn.split(".")
    t = SimpleNamespace()
    t.fqn = fqn
    t.database_name = parts[0]
    t.schema_name = parts[1]
    t.table_name = parts[2]
    t.row_count = row_count
    t.owner = "test_owner"
    return t


def _make_block(**kwargs):
    """Minimal object that mimics an Anthropic content block."""
    b = SimpleNamespace(**kwargs)
    return b


def _make_message(stop_reason, content):
    m = SimpleNamespace()
    m.stop_reason = stop_reason
    m.content = content
    return m


# ─────────────────────────────────────────────────────────────────────────────
# 1.  ask_claude_agentic — tool-use loop
# ─────────────────────────────────────────────────────────────────────────────

class TestAskClaudeAgentic(unittest.TestCase):
    """White-box tests for the agentic loop in claude_client.py."""

    def _make_stream_ctx(self, message):
        """Return a context-manager mock that yields `message` from get_final_message()."""
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=cm)
        cm.__exit__ = MagicMock(return_value=False)
        cm.get_final_message = MagicMock(return_value=message)
        return cm

    def test_no_tool_use_returns_text_immediately(self):
        """When stop_reason is end_turn, the loop exits after one round."""
        from app.services.claude_client import ask_claude_agentic

        final_msg = _make_message(
            stop_reason="end_turn",
            content=[_make_block(type="text", text="Here is my analysis.")],
        )
        mock_client = MagicMock()
        mock_client.messages.stream.return_value = self._make_stream_ctx(final_msg)

        with patch("app.services.claude_client.get_claude_client", return_value=mock_client):
            result = ask_claude_agentic(
                prompt="Analyse this table.",
                tools=[{"name": "get_sample_rows", "input_schema": {}}],
                tool_executor=MagicMock(),
                max_tool_rounds=5,
            )

        self.assertEqual(result["text"], "Here is my analysis.")
        self.assertEqual(result["tool_calls"], [])
        # stream called exactly once — no extra rounds
        self.assertEqual(mock_client.messages.stream.call_count, 1)

    def test_one_tool_call_round_trip(self):
        """
        Round 1: Claude returns stop_reason=tool_use with one get_sample_rows block.
        Round 2: tool result injected; Claude returns end_turn with final text.
        Verify the tool executor was called with the right inputs and the
        tool result was included in the second API call's messages.
        """
        from app.services.claude_client import ask_claude_agentic

        # Round 1 — tool_use
        round1_msg = _make_message(
            stop_reason="tool_use",
            content=[
                _make_block(type="thinking", thinking="I need to see some rows."),
                _make_block(
                    type="tool_use",
                    id="tool_abc123",
                    name="get_sample_rows",
                    input={"columns": ["STATUS"], "where_clause": "STATUS IS NULL", "limit": 5,
                           "reason": "check null pattern"},
                ),
            ],
        )
        # Round 2 — end_turn
        round2_msg = _make_message(
            stop_reason="end_turn",
            content=[_make_block(type="text", text='{"table_type": "fact", "new_instances": []}')],
        )

        call_count = [0]
        def stream_side_effect(**kwargs):
            call_count[0] += 1
            msg = round1_msg if call_count[0] == 1 else round2_msg
            cm = MagicMock()
            cm.__enter__ = MagicMock(return_value=cm)
            cm.__exit__ = MagicMock(return_value=False)
            cm.get_final_message = MagicMock(return_value=msg)
            return cm

        mock_client = MagicMock()
        mock_client.messages.stream.side_effect = stream_side_effect

        tool_executor = MagicMock(return_value="STATUS | \n---\nNULL |\nNULL |")

        with patch("app.services.claude_client.get_claude_client", return_value=mock_client):
            result = ask_claude_agentic(
                prompt="Analyse this table.",
                tools=[{"name": "get_sample_rows", "input_schema": {}}],
                tool_executor=tool_executor,
                max_tool_rounds=5,
            )

        # Tool executor called once with the right args
        tool_executor.assert_called_once_with(
            "get_sample_rows",
            {"columns": ["STATUS"], "where_clause": "STATUS IS NULL", "limit": 5,
             "reason": "check null pattern"},
        )

        # Two API calls total
        self.assertEqual(mock_client.messages.stream.call_count, 2)

        # Second call must have the tool result in messages
        second_call_kwargs = mock_client.messages.stream.call_args_list[1][1]
        messages = second_call_kwargs["messages"]
        # messages: original user turn, assistant turn (with tool_use), tool_result turn
        self.assertEqual(len(messages), 3)
        tool_result_turn = messages[2]
        self.assertEqual(tool_result_turn["role"], "user")
        self.assertEqual(tool_result_turn["content"][0]["type"], "tool_result")
        self.assertEqual(tool_result_turn["content"][0]["tool_use_id"], "tool_abc123")
        self.assertIn("NULL", tool_result_turn["content"][0]["content"])

        # tool_calls recorded in return value
        self.assertEqual(len(result["tool_calls"]), 1)
        self.assertEqual(result["tool_calls"][0]["name"], "get_sample_rows")
        self.assertIn("NULL", result["tool_calls"][0]["result"])

    def test_max_tool_rounds_respected(self):
        """Loop exits after max_tool_rounds even if model keeps returning tool_use."""
        from app.services.claude_client import ask_claude_agentic

        tool_use_block = _make_block(
            type="tool_use", id="tid1", name="get_sample_rows", input={"reason": "r"},
        )
        infinite_msg = _make_message(stop_reason="tool_use", content=[tool_use_block])
        end_msg = _make_message(stop_reason="end_turn", content=[_make_block(type="text", text="done")])

        call_count = [0]
        def stream_side_effect(**kwargs):
            call_count[0] += 1
            # return end_turn on call max+1 so we also test the final-message extraction
            msg = end_msg if call_count[0] > 3 else infinite_msg
            cm = MagicMock()
            cm.__enter__ = MagicMock(return_value=cm)
            cm.__exit__ = MagicMock(return_value=False)
            cm.get_final_message = MagicMock(return_value=msg)
            return cm

        mock_client = MagicMock()
        mock_client.messages.stream.side_effect = stream_side_effect

        with patch("app.services.claude_client.get_claude_client", return_value=mock_client):
            result = ask_claude_agentic(
                prompt="test",
                tool_executor=MagicMock(return_value="rows"),
                max_tool_rounds=3,
            )

        # max_tool_rounds=3 → at most 4 total API calls (rounds 0,1,2,3)
        self.assertLessEqual(mock_client.messages.stream.call_count, 4)

    def test_thinking_blocks_accumulated(self):
        """Thinking text from multiple rounds is concatenated in the return value."""
        from app.services.claude_client import ask_claude_agentic

        round1 = _make_message(
            stop_reason="tool_use",
            content=[
                _make_block(type="thinking", thinking="First thought."),
                _make_block(type="tool_use", id="t1", name="get_sample_rows", input={"reason": "r"}),
            ],
        )
        round2 = _make_message(
            stop_reason="end_turn",
            content=[
                _make_block(type="thinking", thinking="Second thought."),
                _make_block(type="text", text="final"),
            ],
        )

        call_count = [0]
        def stream_se(**kwargs):
            call_count[0] += 1
            msg = round1 if call_count[0] == 1 else round2
            cm = MagicMock()
            cm.__enter__ = MagicMock(return_value=cm)
            cm.__exit__ = MagicMock(return_value=False)
            cm.get_final_message = MagicMock(return_value=msg)
            return cm

        mock_client = MagicMock()
        mock_client.messages.stream.side_effect = stream_se

        with patch("app.services.claude_client.get_claude_client", return_value=mock_client):
            result = ask_claude_agentic("test", tool_executor=MagicMock(return_value="r"))

        self.assertIn("First thought.", result["thinking"])
        self.assertIn("Second thought.", result["thinking"])

    def test_tool_executor_exception_caught(self):
        """If the tool executor raises, the loop continues with an error string, not a crash."""
        from app.services.claude_client import ask_claude_agentic

        round1 = _make_message(
            stop_reason="tool_use",
            content=[_make_block(type="tool_use", id="t1", name="get_sample_rows", input={})],
        )
        round2 = _make_message(
            stop_reason="end_turn",
            content=[_make_block(type="text", text="done")],
        )

        call_count = [0]
        def stream_se(**kwargs):
            call_count[0] += 1
            cm = MagicMock()
            cm.__enter__ = MagicMock(return_value=cm)
            cm.__exit__ = MagicMock(return_value=False)
            cm.get_final_message = MagicMock(return_value=round1 if call_count[0] == 1 else round2)
            return cm

        mock_client = MagicMock()
        mock_client.messages.stream.side_effect = stream_se

        exploding_executor = MagicMock(side_effect=RuntimeError("Snowflake is down"))

        with patch("app.services.claude_client.get_claude_client", return_value=mock_client):
            result = ask_claude_agentic("test", tool_executor=exploding_executor)

        # Should not raise; tool_calls result should contain the error string
        self.assertEqual(len(result["tool_calls"]), 1)
        self.assertIn("Tool execution error", result["tool_calls"][0]["result"])
        self.assertEqual(result["text"], "done")


# ─────────────────────────────────────────────────────────────────────────────
# 2.  _execute_sample_tool — safety guardrails
# ─────────────────────────────────────────────────────────────────────────────

class TestExecuteSampleTool(unittest.TestCase):
    """
    _execute_sample_tool is a method on RuleIntelligenceAgent.
    We instantiate the agent and mock only sf_session so no DB call happens.
    """

    def _make_agent(self, mock_rows=None):
        """Return a RuleIntelligenceAgent with sf_session patched."""
        # Patch storage and sf_session so __init__ / imports don't fail
        with patch("app.services.agents.rule_intelligence_agent.storage"), \
             patch("app.services.agents.rule_intelligence_agent.sf_session") as mock_sf:
            from app.services.agents.rule_intelligence_agent import RuleIntelligenceAgent
            agent = RuleIntelligenceAgent()
            agent._closed_set_columns = {}
            agent._source = None
        return agent, mock_sf

    def test_valid_query_returns_formatted_rows(self):
        """Normal case: columns + where_clause + limit → formatted table."""
        with patch("app.services.agents.rule_intelligence_agent.sf_session") as mock_sf:
            mock_sf.query.return_value = [
                {"STATUS": "NULL_VAL", "AMOUNT": "0"},
                {"STATUS": "NULL_VAL", "AMOUNT": "-1"},
            ]
            from app.services.agents.rule_intelligence_agent import RuleIntelligenceAgent
            agent = RuleIntelligenceAgent.__new__(RuleIntelligenceAgent)
            agent._closed_set_columns = {}
            agent._source = None

            table = _fake_table("MYDB.MYSCH.ORDERS")
            inputs = {
                "columns": ["STATUS", "AMOUNT"],
                "where_clause": "STATUS IS NULL",
                "limit": 5,
                "reason": "check null pattern",
            }
            result = agent._execute_sample_tool(table, inputs)

        self.assertIn("STATUS", result)
        self.assertIn("NULL_VAL", result)
        # Pipe separator present
        self.assertIn("|", result)

    def test_forbidden_keyword_in_where_rejected(self):
        """WHERE clause containing SELECT should be rejected, not executed."""
        with patch("app.services.agents.rule_intelligence_agent.sf_session") as mock_sf:
            from app.services.agents.rule_intelligence_agent import RuleIntelligenceAgent
            agent = RuleIntelligenceAgent.__new__(RuleIntelligenceAgent)
            agent._closed_set_columns = {}
            agent._source = None

            table = _fake_table()
            inputs = {"where_clause": "1=1 UNION SELECT * FROM OTHER_TABLE", "reason": "injection attempt"}
            result = agent._execute_sample_tool(table, inputs)

        self.assertIn("Rejected", result)
        mock_sf.query.assert_not_called()

    def test_drop_keyword_rejected(self):
        """DROP in WHERE clause is blocked."""
        with patch("app.services.agents.rule_intelligence_agent.sf_session") as mock_sf:
            from app.services.agents.rule_intelligence_agent import RuleIntelligenceAgent
            agent = RuleIntelligenceAgent.__new__(RuleIntelligenceAgent)
            agent._closed_set_columns = {}
            agent._source = None

            table = _fake_table()
            inputs = {"where_clause": "1=1; DROP TABLE ORDERS", "reason": "bad actor"}
            result = agent._execute_sample_tool(table, inputs)

        self.assertIn("Rejected", result)
        mock_sf.query.assert_not_called()

    def test_semicolon_keyword_rejected(self):
        """Semicolons (statement terminator) are blocked."""
        with patch("app.services.agents.rule_intelligence_agent.sf_session") as mock_sf:
            from app.services.agents.rule_intelligence_agent import RuleIntelligenceAgent
            agent = RuleIntelligenceAgent.__new__(RuleIntelligenceAgent)
            agent._closed_set_columns = {}
            agent._source = None

            table = _fake_table()
            inputs = {"where_clause": "AMOUNT < 0; --", "reason": "comment injection"}
            result = agent._execute_sample_tool(table, inputs)

        self.assertIn("Rejected", result)

    def test_row_cap_enforced(self):
        """Limit is capped at _SAMPLE_MAX_ROWS=20 regardless of what Claude requests."""
        with patch("app.services.agents.rule_intelligence_agent.sf_session") as mock_sf:
            mock_sf.query.return_value = [{"X": str(i)} for i in range(20)]
            from app.services.agents.rule_intelligence_agent import RuleIntelligenceAgent, _SAMPLE_MAX_ROWS
            agent = RuleIntelligenceAgent.__new__(RuleIntelligenceAgent)
            agent._closed_set_columns = {}
            agent._source = None

            table = _fake_table()
            inputs = {"limit": 999, "reason": "greedy"}
            agent._execute_sample_tool(table, inputs)

        called_sql = mock_sf.query.call_args[0][0]
        self.assertIn(f"LIMIT {_SAMPLE_MAX_ROWS}", called_sql)

    def test_empty_result_returns_no_rows_message(self):
        with patch("app.services.agents.rule_intelligence_agent.sf_session") as mock_sf:
            mock_sf.query.return_value = []
            from app.services.agents.rule_intelligence_agent import RuleIntelligenceAgent
            agent = RuleIntelligenceAgent.__new__(RuleIntelligenceAgent)
            agent._closed_set_columns = {}
            agent._source = None

            result = agent._execute_sample_tool(_fake_table(), {"reason": "test"})

        self.assertIn("no rows matched", result)

    def test_sf_exception_returns_error_string(self):
        with patch("app.services.agents.rule_intelligence_agent.sf_session") as mock_sf:
            mock_sf.query.side_effect = Exception("connection reset")
            from app.services.agents.rule_intelligence_agent import RuleIntelligenceAgent
            agent = RuleIntelligenceAgent.__new__(RuleIntelligenceAgent)
            agent._closed_set_columns = {}
            agent._source = None

            result = agent._execute_sample_tool(_fake_table(), {"reason": "test"})

        self.assertIn("Query failed", result)

    def test_column_list_forwarded_to_sql(self):
        """Requested columns appear in the generated SELECT."""
        with patch("app.services.agents.rule_intelligence_agent.sf_session") as mock_sf:
            mock_sf.query.return_value = [{"ORDER_ID": "1", "STATUS": "ACTIVE"}]
            from app.services.agents.rule_intelligence_agent import RuleIntelligenceAgent
            agent = RuleIntelligenceAgent.__new__(RuleIntelligenceAgent)
            agent._closed_set_columns = {}
            agent._source = None

            agent._execute_sample_tool(
                _fake_table(),
                {"columns": ["ORDER_ID", "STATUS"], "reason": "verify format"},
            )

        sql = mock_sf.query.call_args[0][0]
        self.assertIn("ORDER_ID", sql)
        self.assertIn("STATUS", sql)

    def test_where_clause_forwarded_to_sql(self):
        """Safe WHERE clause ends up in the executed SQL."""
        with patch("app.services.agents.rule_intelligence_agent.sf_session") as mock_sf:
            mock_sf.query.return_value = [{"AMOUNT": "-5"}]
            from app.services.agents.rule_intelligence_agent import RuleIntelligenceAgent
            agent = RuleIntelligenceAgent.__new__(RuleIntelligenceAgent)
            agent._closed_set_columns = {}
            agent._source = None

            agent._execute_sample_tool(
                _fake_table(),
                {"where_clause": "AMOUNT < 0", "reason": "find negatives"},
            )

        sql = mock_sf.query.call_args[0][0]
        self.assertIn("WHERE AMOUNT < 0", sql)


# ─────────────────────────────────────────────────────────────────────────────
# 3.  _self_critique_proposals — weak proposals filtered
# ─────────────────────────────────────────────────────────────────────────────

class TestSelfCritiqueProposals(unittest.TestCase):

    def _agent(self):
        from app.services.agents.rule_intelligence_agent import RuleIntelligenceAgent
        agent = RuleIntelligenceAgent.__new__(RuleIntelligenceAgent)
        agent._closed_set_columns = {}
        return agent

    def _proposals(self, n=3):
        return [
            {
                "definition_id": None,
                "new_definition": {"name": f"Check {i}", "category": "data_quality", "description": "desc"},
                "scope": "column",
                "column_name": f"COL_{i}",
                "template_shape": "not_null",
                "threshold_config": {},
                "severity": "medium",
                "violation_detected": False,
                "rationale": f"Rationale {i}",
            }
            for i in range(n)
        ]

    def _existing_reuse_proposals(self, n=3):
        """Existing-reuse proposals (definition_id set, no new_definition) —
        these are the ones that actually go through the critique. Novel
        proposals bypass it by design."""
        return [
            {
                "definition_id": f"def-{i}",
                "new_definition": None,
                "scope": "column",
                "column_name": f"COL_{i}",
                "template_shape": "not_null",
                "threshold_config": {},
                "severity": "medium",
                "violation_detected": False,
                "rationale": f"Rationale {i}",
            }
            for i in range(n)
        ]

    def test_all_kept_when_scores_above_threshold(self):
        """All proposals with mean >= 3 are kept."""
        proposals = self._existing_reuse_proposals(2)
        scores_response = {
            "scores": [
                {"index": 0, "evidence": 4, "impact": 4, "approval": 4, "drop_reason": None},
                {"index": 1, "evidence": 3, "impact": 3, "approval": 3, "drop_reason": None},
            ]
        }

        agent = self._agent()
        with patch("app.services.agents.rule_intelligence_agent.ask_claude_json", return_value=scores_response):
            result = agent._self_critique_proposals(
                proposals=proposals,
                table_asset=_fake_table(),
                column_stats_text="COL_0  null%=0  distinct=50",
                run_id="run-x",
                min_score=3.0,
            )

        self.assertEqual(len(result), 2)

    def test_weak_proposals_dropped(self):
        """Proposals with mean < min_score are removed from the list."""
        proposals = self._existing_reuse_proposals(3)
        scores_response = {
            "scores": [
                {"index": 0, "evidence": 5, "impact": 5, "approval": 5, "drop_reason": None},
                # index 1: mean = 1.0 → dropped
                {"index": 1, "evidence": 1, "impact": 1, "approval": 1,
                 "drop_reason": "no evidence in stats"},
                # index 2: mean = 2.0 → dropped when min_score=3
                {"index": 2, "evidence": 2, "impact": 2, "approval": 2,
                 "drop_reason": "speculative"},
            ]
        }

        agent = self._agent()
        with patch("app.services.agents.rule_intelligence_agent.ask_claude_json", return_value=scores_response), \
             patch("app.services.agents.rule_intelligence_agent.storage") as mock_storage:
            result = agent._self_critique_proposals(
                proposals=proposals,
                table_asset=_fake_table(),
                column_stats_text="",
                run_id="run-x",
                min_score=3.0,
            )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["definition_id"], "def-0")
        # dropped proposals are logged to RULE_CRITIQUE_DROPS
        self.assertEqual(mock_storage.log_critique_drop.call_count, 2)

    def test_boundary_score_exactly_3_is_kept(self):
        """Mean == threshold exactly is kept (>= threshold)."""
        proposals = self._existing_reuse_proposals(1)
        scores_response = {
            "scores": [{"index": 0, "evidence": 3, "impact": 3, "approval": 3, "drop_reason": None}]
        }

        agent = self._agent()
        with patch("app.services.agents.rule_intelligence_agent.ask_claude_json", return_value=scores_response):
            result = agent._self_critique_proposals(proposals, _fake_table(), "", run_id="run-x", min_score=3.0)

        self.assertEqual(len(result), 1)

    def test_parse_failure_keeps_all_proposals(self):
        """If the critique call returns None (unparseable JSON), all proposals are kept."""
        proposals = self._existing_reuse_proposals(3)

        agent = self._agent()
        with patch("app.services.agents.rule_intelligence_agent.ask_claude_json", return_value=None):
            result = agent._self_critique_proposals(proposals, _fake_table(), "", run_id="run-x")

        self.assertEqual(len(result), 3)

    def test_empty_proposals_returns_empty(self):
        agent = self._agent()
        with patch("app.services.agents.rule_intelligence_agent.ask_claude_json") as mock_ask:
            result = agent._self_critique_proposals([], _fake_table(), "", run_id="run-x")
        mock_ask.assert_not_called()
        self.assertEqual(result, [])

    def test_missing_score_entry_keeps_proposal(self):
        """If the critique omits an index, that proposal survives (safe default)."""
        proposals = self._existing_reuse_proposals(2)
        scores_response = {
            "scores": [
                # Only index 0 scored; index 1 missing → kept by default
                {"index": 0, "evidence": 5, "impact": 5, "approval": 5, "drop_reason": None},
            ]
        }

        agent = self._agent()
        with patch("app.services.agents.rule_intelligence_agent.ask_claude_json", return_value=scores_response):
            result = agent._self_critique_proposals(proposals, _fake_table(), "", run_id="run-x")

        self.assertEqual(len(result), 2)

    def test_critique_call_receives_all_proposals(self):
        """The prompt sent to Claude contains all proposal names."""
        proposals = self._existing_reuse_proposals(2)
        scores_response = {
            "scores": [
                {"index": 0, "evidence": 4, "impact": 4, "approval": 4, "drop_reason": None},
                {"index": 1, "evidence": 4, "impact": 4, "approval": 4, "drop_reason": None},
            ]
        }

        agent = self._agent()
        with patch("app.services.agents.rule_intelligence_agent.ask_claude_json", return_value=scores_response) as mock_ask:
            agent._self_critique_proposals(proposals, _fake_table(), "COL_0 null%=5", run_id="run-x")

        prompt_sent = mock_ask.call_args[0][0]
        self.assertIn("def-0", prompt_sent)
        self.assertIn("def-1", prompt_sent)
        self.assertIn("COL_0 null%=5", prompt_sent)

    def test_novel_proposals_bypass_critique(self):
        """New: novel proposals (definition_id=None + new_definition set) skip
        the critique entirely — they're kept without being scored."""
        proposals = [
            {
                "definition_id": None,
                "new_definition": {"name": "Check N", "category": "data_quality", "description": "desc"},
                "scope": "column",
                "column_name": "COL",
                "template_shape": "not_null",
                "threshold_config": {},
                "severity": "medium",
                "violation_detected": False,
                "rationale": "rationale",
            }
        ]

        agent = self._agent()
        with patch("app.services.agents.rule_intelligence_agent.ask_claude_json") as mock_ask:
            result = agent._self_critique_proposals(proposals, _fake_table(), "",
                                                    run_id="run-x", min_score=3.0)
        # No call to Claude — nothing to score, everything is novel
        mock_ask.assert_not_called()
        self.assertEqual(len(result), 1)


# ─────────────────────────────────────────────────────────────────────────────
# 4.  _repair_draft_sql — invalid SQL gets a repair call
# ─────────────────────────────────────────────────────────────────────────────

class TestRepairDraftSql(unittest.TestCase):

    def _agent(self):
        from app.services.agents.rule_intelligence_agent import RuleIntelligenceAgent
        agent = RuleIntelligenceAgent.__new__(RuleIntelligenceAgent)
        agent._closed_set_columns = {}
        return agent

    def test_valid_repair_returned_on_first_attempt(self):
        """When Claude's first repair passes validation, it is returned."""
        fixed_sql = "SELECT COUNT_IF(STATUS IS NULL) AS FAILED_COUNT, COUNT(*) AS TOTAL_COUNT FROM DB.SCH.ORDERS"

        with patch("app.services.agents.rule_intelligence_agent.ask_claude", return_value=fixed_sql), \
             patch("app.services.agents.rule_intelligence_agent.validate_sql") as mock_val:
            mock_val.return_value = SimpleNamespace(is_valid=True, errors=[])
            from app.services.agents.rule_intelligence_agent import RuleIntelligenceAgent
            agent = RuleIntelligenceAgent.__new__(RuleIntelligenceAgent)
            agent._closed_set_columns = {}
            agent._source = None

            result = agent._repair_draft_sql(
                draft_sql="SELECT bad_column FROM ORDERS",
                errors="column 'bad_column' not found",
                table_asset=_fake_table(),
                candidate={"rationale": "check nulls"},
                allowed_tables=["DB.SCH.ORDERS"],
            )

        self.assertEqual(result, fixed_sql)

    def test_repair_strips_markdown_fences(self):
        """Markdown code fences that the model accidentally adds are stripped."""
        fenced = "```sql\nSELECT COUNT_IF(X IS NULL) AS FAILED_COUNT, COUNT(*) AS TOTAL_COUNT FROM DB.SCH.ORDERS\n```"
        expected_clean = "SELECT COUNT_IF(X IS NULL) AS FAILED_COUNT, COUNT(*) AS TOTAL_COUNT FROM DB.SCH.ORDERS"

        with patch("app.services.agents.rule_intelligence_agent.ask_claude", return_value=fenced), \
             patch("app.services.agents.rule_intelligence_agent.validate_sql") as mock_val:
            mock_val.return_value = SimpleNamespace(is_valid=True, errors=[])
            from app.services.agents.rule_intelligence_agent import RuleIntelligenceAgent
            agent = RuleIntelligenceAgent.__new__(RuleIntelligenceAgent)
            agent._closed_set_columns = {}
            agent._source = None

            result = agent._repair_draft_sql(
                draft_sql="bad", errors="err",
                table_asset=_fake_table(),
                candidate={}, allowed_tables=["DB.SCH.ORDERS"],
            )

        self.assertEqual(result, expected_clean)

    def test_all_attempts_fail_returns_none(self):
        """If all repair attempts still fail validation, None is returned."""
        still_bad = "SELECT * FROM ORDERS"

        with patch("app.services.agents.rule_intelligence_agent.ask_claude", return_value=still_bad), \
             patch("app.services.agents.rule_intelligence_agent.validate_sql") as mock_val:
            mock_val.return_value = SimpleNamespace(is_valid=False, errors=["missing FAILED_COUNT"])
            from app.services.agents.rule_intelligence_agent import RuleIntelligenceAgent
            agent = RuleIntelligenceAgent.__new__(RuleIntelligenceAgent)
            agent._closed_set_columns = {}
            agent._source = None

            result = agent._repair_draft_sql(
                draft_sql="bad", errors="err",
                table_asset=_fake_table(),
                candidate={}, allowed_tables=["DB.SCH.ORDERS"],
                max_attempts=2,
            )

        self.assertIsNone(result)

    def test_repair_prompt_contains_error_and_original_sql(self):
        """The repair prompt sent to Claude mentions the original error and SQL."""
        original_sql = "SELECT broken FROM TABLE"
        error_msg = "unknown column 'broken'"

        with patch("app.services.agents.rule_intelligence_agent.ask_claude") as mock_ask, \
             patch("app.services.agents.rule_intelligence_agent.validate_sql") as mock_val:
            mock_val.return_value = SimpleNamespace(is_valid=False, errors=["still bad"])
            mock_ask.return_value = "SELECT 1"
            from app.services.agents.rule_intelligence_agent import RuleIntelligenceAgent
            agent = RuleIntelligenceAgent.__new__(RuleIntelligenceAgent)
            agent._closed_set_columns = {}
            agent._source = None

            agent._repair_draft_sql(
                draft_sql=original_sql,
                errors=error_msg,
                table_asset=_fake_table(),
                candidate={},
                allowed_tables=["DB.SCH.ORDERS"],
                max_attempts=1,
            )

        prompt = mock_ask.call_args[0][0]
        self.assertIn(error_msg, prompt)
        self.assertIn(original_sql, prompt)

    def test_repair_called_from_build_rule_sql_on_invalid_draft(self):
        """
        Integration: _build_rule_sql calls _repair_draft_sql when draft_sql
        fails validation, and returns the repaired SQL.
        """
        bad_sql = "SELECT * FROM ORDERS"
        good_sql = "SELECT COUNT_IF(X IS NULL) AS FAILED_COUNT, COUNT(*) AS TOTAL_COUNT FROM DB.SCH.ORDERS"

        # validate_sql: fail on first call (original), pass on second (repaired)
        validation_results = [
            SimpleNamespace(is_valid=False, errors=["missing FAILED_COUNT"]),
            SimpleNamespace(is_valid=True, errors=[]),
        ]
        call_idx = [0]
        def val_side_effect(sql, **kwargs):
            r = validation_results[min(call_idx[0], len(validation_results) - 1)]
            call_idx[0] += 1
            return r

        with patch("app.services.agents.rule_intelligence_agent.ask_claude", return_value=good_sql), \
             patch("app.services.agents.rule_intelligence_agent.validate_sql", side_effect=val_side_effect):
            from app.services.agents.rule_intelligence_agent import RuleIntelligenceAgent
            agent = RuleIntelligenceAgent.__new__(RuleIntelligenceAgent)
            agent._closed_set_columns = {}
            agent._source = None

            sql, tc = agent._build_rule_sql(
                candidate={"draft_sql": bad_sql, "threshold_config": {}},
                table_asset=_fake_table(),
                target_config={},
            )

        self.assertEqual(sql, good_sql)

    def test_build_rule_sql_returns_none_when_repair_exhausted(self):
        """_build_rule_sql returns (None, None) when draft_sql is invalid and repair also fails."""
        with patch("app.services.agents.rule_intelligence_agent.validate_sql") as mock_val, \
             patch("app.services.agents.rule_intelligence_agent.ask_claude", return_value="still bad"):
            mock_val.return_value = SimpleNamespace(is_valid=False, errors=["always invalid"])
            from app.services.agents.rule_intelligence_agent import RuleIntelligenceAgent
            agent = RuleIntelligenceAgent.__new__(RuleIntelligenceAgent)
            agent._closed_set_columns = {}
            agent._source = None

            sql, tc = agent._build_rule_sql(
                candidate={"draft_sql": "bad sql", "threshold_config": {}},
                table_asset=_fake_table(),
                target_config={},
            )

        self.assertIsNone(sql)
        self.assertIsNone(tc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
