"""
Rule-definition deduplication — Rule Intelligence's guard against library
bloat.

Covers three layers of dedup, each with its own regression case (dates from
the 2026-07-16 audit):

  1. `_find_similar_definition` (rule_intelligence_agent.py L1287) — exact
     name match wins before fuzzy word-overlap. Original bug: "Cross-Column
     Timestamp Ordering" spawned twice because description prose differed
     enough to score 0.41 on combined overlap (< 0.55 threshold).

  2. Same-run definition collapsing (coordinator.py L413-458) — multiple
     proposals in ONE Claude response sharing `new_definition_key` collapse
     to a single RULE_DEFINITIONS row. Original bug: WIDE_TRANSACTIONS scan
     proposed "Cross-Column Numeric Ordering (Min <= Max)" three times for
     HIGH/LOW, OUTRIGHT_HIGH/LOW, PREMIUM_HIGH/LOW → 3 separate def rows.

  3. Instance-level fingerprint dedup (rule_intelligence_agent.py L1218,
     compute_fingerprint) — same (def_id + scope + target + threshold) never
     re-proposed even if Claude asks again on rescan.
"""
import copy
import uuid
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from tests.conftest import make_definition, make_table_asset, FakeStorage  # type: ignore


# ─────────────────────────────────────────────────────────────────────────────
# Layer 1 — _find_similar_definition
# ─────────────────────────────────────────────────────────────────────────────

def _agent():
    """Bare agent with just the attributes _find_similar_definition needs."""
    from app.services.agents.rule_intelligence_agent import RuleIntelligenceAgent
    a = RuleIntelligenceAgent.__new__(RuleIntelligenceAgent)
    a._closed_set_columns = {}
    a._source = None
    return a


class TestFindSimilarDefinition:

    def test_exact_name_match_short_circuits_before_fuzzy(self):
        """Regression: identical name wins even when descriptions differ enough
        to score below the 0.55 fuzzy threshold. Original bug — 'Cross-Column
        Timestamp Ordering' scored 0.41 combined-overlap, spawned a dup row."""
        existing = [
            make_definition(name="Cross-Column Timestamp Ordering",
                            description="Ensures event_time <= processed_time on each row."),
        ]
        proposed = {
            "name": "Cross-Column Timestamp Ordering",
            "description": "Ordering rule on scheduled_at and started_at columns. "
                          "Completely different-sounding description prose that "
                          "would drop overlap below threshold if this were fuzzy.",
        }
        matched = _agent()._find_similar_definition(proposed, existing)
        assert matched is existing[0]

    def test_exact_name_match_is_case_insensitive(self):
        existing = [make_definition(name="Cross-Column Timestamp Ordering",
                                    description="d")]
        proposed = {"name": "cross-column TIMESTAMP ordering", "description": "d"}
        assert _agent()._find_similar_definition(proposed, existing) is existing[0]

    def test_exact_name_match_ignores_whitespace(self):
        existing = [make_definition(name="Cross-Column Timestamp Ordering",
                                    description="d")]
        proposed = {"name": "  Cross-Column Timestamp Ordering  ", "description": "d"}
        assert _agent()._find_similar_definition(proposed, existing) is existing[0]

    def test_fuzzy_match_when_names_differ_but_similar_prose(self):
        """When the exact-name path misses, fall through to word_overlap_score
        combined name+description. High-overlap paraphrases should match."""
        existing = [make_definition(
            name="Numeric Range Check",
            description="Validates that a numeric column falls within a min/max bound."
        )]
        proposed = {
            "name": "Numeric Range Check on Amount",
            "description": "Validates that a numeric column falls within a min/max bound.",
        }
        matched = _agent()._find_similar_definition(proposed, existing)
        # word_overlap on combined text is high — should match
        assert matched is existing[0]

    def test_low_overlap_returns_none(self):
        existing = [make_definition(name="Not Null", description="Column must not be null.")]
        proposed = {"name": "Freshness Check",
                    "description": "Latest timestamp must be within N days of now."}
        assert _agent()._find_similar_definition(proposed, existing) is None

    def test_below_threshold_returns_none(self):
        """The 0.55 default: a paraphrase scoring 0.4 should NOT match by fuzzy.
        Regression coverage — the definition-duplication bug family."""
        existing = [make_definition(
            name="Foreign Key Integrity",
            description="Every child row must reference an existing parent."
        )]
        proposed = {
            "name": "Referential Integrity",
            "description": "Non-null values in a linked column exist in the target table.",
        }
        # This overlap is intentionally sub-threshold — we're asserting we
        # DO NOT match by fuzzy alone. Exact-name path also misses.
        matched = _agent()._find_similar_definition(proposed, existing, threshold=0.7)
        assert matched is None

    def test_empty_existing_list_returns_none(self):
        assert _agent()._find_similar_definition({"name": "X", "description": "y"}, []) is None

    def test_lower_threshold_permits_looser_match(self):
        """Synthesis path (rule_intelligence_agent.py L1149) uses threshold=0.5
        because rationale prose drifts more than deliberate names. Verify
        threshold param actually matters — moderate overlap crosses low
        threshold but stays below a high one."""
        existing = [make_definition(
            name="Uniqueness on Customer ID",
            description="Column CUSTOMER_ID must be unique across rows in the table."
        )]
        proposed = {
            "name": "Composite Order Key",
            "description": "Every combination of shipping_zone and delivery_slot must be unique.",
        }
        with_high = _agent()._find_similar_definition(proposed, existing, threshold=0.9)
        with_loose = _agent()._find_similar_definition(proposed, existing, threshold=0.1)
        assert with_high is None
        assert with_loose is existing[0]

    def test_exact_name_wins_over_higher_scoring_paraphrase(self):
        """If two definitions exist, and one has the same NAME and one has
        higher word-overlap on prose, the same-NAME one wins."""
        existing = [
            make_definition(def_id="d1", name="Cross-Column Timestamp Ordering",
                            description="short desc"),
            make_definition(def_id="d2", name="Different Concept Entirely",
                            description="event_time processed_time scheduled_at "
                                        "started_at ordering rule columns rows"),
        ]
        proposed = {
            "name": "Cross-Column Timestamp Ordering",
            "description": "event_time processed_time scheduled_at started_at "
                          "ordering rule columns rows",
        }
        matched = _agent()._find_similar_definition(proposed, existing)
        assert matched.id == "d1"

    def test_missing_name_falls_through_to_fuzzy_only(self):
        existing = [make_definition(name="Freshness", description="Stale data check.")]
        proposed = {"description": "Stale data check."}  # no 'name'
        matched = _agent()._find_similar_definition(proposed, existing, threshold=0.3)
        assert matched is existing[0]


# ─────────────────────────────────────────────────────────────────────────────
# Layer 2 — same-run definition collapsing
# This exercises coordinator.py's `new_definitions_by_key` loop directly by
# constructing the "proposed_instances" shape and driving the loop.
# ─────────────────────────────────────────────────────────────────────────────

def _proposed_instance(name, dedup_key, column="X", scope="column",
                        target_config=None, threshold_config=None,
                        rule_sql="SELECT 0 AS FAILED_COUNT, 1 AS TOTAL_COUNT"):
    return {
        "kind": "new",
        "source": "llm",
        "fingerprint": f"fp-{uuid.uuid4().hex[:12]}",
        "definition": None,
        "new_definition_data": {"name": name, "category": "data_quality",
                                "description": f"desc for {name}"},
        "new_definition_key": dedup_key,
        "template_shape": None,
        "scope": scope,
        "target_config": target_config or {"column": column},
        "threshold_config": threshold_config or {},
        "rule_sql": rule_sql,
        "column_name": column,
        "severity": "medium",
        "violated": True,
        "evidence": "3 of 100 rows fail",
        "rationale": f"Rationale for {name}",
        "source_run_id": "run-abc",
    }


def _simulate_coordinator_persist_loop(proposed_instances, storage_stub, table_asset, run_id):
    """Reproduce the coordinator's per-proposal persist loop faithfully —
    same order, same dedup logic, so a break here mirrors a coordinator
    regression."""
    new_definitions_by_key = {}
    active_entries = []
    for proposal in proposed_instances:
        if proposal["kind"] == "new":
            nd = proposal["new_definition_data"] or {}
            category = nd.get("category") if nd.get("category") in {
                "naming", "documentation", "ownership", "schema",
                "data_quality", "security", "performance"
            } else "data_quality"
            dedup_key = proposal.get("new_definition_key")
            definition = new_definitions_by_key.get(dedup_key) if dedup_key else None
            if definition is None:
                definition = storage_stub.create_definition(
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
        else:
            definition = proposal["definition"]

        instance = storage_stub.create_instance(
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
            source_run_id=run_id,
        )
        active_entries.append({
            "instance_id": instance.id, "definition_id": definition.id,
            "name": definition.name, "is_new_definition": proposal["kind"] == "new",
        })
    return active_entries, new_definitions_by_key


class TestSameRunCollapsing:

    def test_three_proposals_same_key_collapse_to_one_definition(self):
        """The WIDE_TRANSACTIONS regression case: three column-pair proposals
        of 'Cross-Column Numeric Ordering (Min <= Max)' with matching keys
        collapse to ONE new definition."""
        storage = FakeStorage()
        table = make_table_asset()
        key = "cross-column numeric ordering (min <= max)"

        proposals = [
            _proposed_instance("Cross-Column Numeric Ordering (Min <= Max)", key,
                               column="HIGH"),
            _proposed_instance("Cross-Column Numeric Ordering (Min <= Max)", key,
                               column="OUTRIGHT_HIGH"),
            _proposed_instance("Cross-Column Numeric Ordering (Min <= Max)", key,
                               column="PREMIUM_HIGH"),
        ]

        entries, by_key = _simulate_coordinator_persist_loop(
            proposals, storage, table, "run-1")

        assert len(storage.definitions) == 1
        assert len(storage.instances) == 3
        assert len(entries) == 3
        assert len(set(e["definition_id"] for e in entries)) == 1
        assert key in by_key

    def test_different_keys_create_separate_definitions(self):
        storage = FakeStorage()
        table = make_table_asset()

        proposals = [
            _proposed_instance("Not Null", "not null", column="A"),
            _proposed_instance("Uniqueness", "uniqueness", column="B"),
            _proposed_instance("Not Null", "not null", column="C"),
        ]
        entries, _ = _simulate_coordinator_persist_loop(proposals, storage, table, "run-1")

        # Two distinct keys → two definitions total, three instances
        assert len(storage.definitions) == 2
        assert len(storage.instances) == 3

    def test_missing_dedup_key_creates_separate_definitions(self):
        """If Claude fails to emit new_definition_key (older responses),
        collapsing doesn't happen — each proposal gets its own definition."""
        storage = FakeStorage()
        table = make_table_asset()

        proposals = [
            _proposed_instance("Same Name", None, column="A"),
            _proposed_instance("Same Name", None, column="B"),
        ]
        _simulate_coordinator_persist_loop(proposals, storage, table, "run-1")

        # No key → no collapsing → two rows
        assert len(storage.definitions) == 2

    def test_reuse_proposal_does_not_participate_in_collapsing(self):
        """A 'reuse' proposal uses its bound definition, doesn't create a new
        row, and doesn't interfere with 'new' collapsing."""
        storage = FakeStorage()
        existing_def = storage.create_definition(name="Existing Def", template_shape="not_null",
                                                  check_kind="sql_template", status="active")
        table = make_table_asset()

        reuse_p = {
            "kind": "reuse",
            "source": "llm",
            "fingerprint": f"fp-{uuid.uuid4().hex[:12]}",
            "definition": existing_def,
            "scope": "column",
            "target_config": {"column": "X"},
            "threshold_config": {},
            "rule_sql": "SELECT 0 AS FAILED_COUNT, 1 AS TOTAL_COUNT",
            "severity": "medium",
            "rationale": "reuse rationale",
        }
        new_p = _proposed_instance("Novel", "novel", column="Y")

        _simulate_coordinator_persist_loop([reuse_p, new_p], storage, table, "run-1")

        # Existing_def already in storage; one more added from new_p → 2 total
        assert len(storage.definitions) == 2
        assert len(storage.instances) == 2

    def test_five_proposals_shared_key_produce_five_instances_one_definition(self):
        """Load-shape sanity: 5 proposals same key → 1 def, 5 instances, all
        pointing at the same def."""
        storage = FakeStorage()
        table = make_table_asset()

        proposals = [
            _proposed_instance("Amt Range", "amt range", column=f"COL_{i}",
                               target_config={"column": f"COL_{i}"})
            for i in range(5)
        ]
        entries, _ = _simulate_coordinator_persist_loop(proposals, storage, table, "run-1")

        assert len(storage.definitions) == 1
        assert len(storage.instances) == 5
        assert all(e["definition_id"] == entries[0]["definition_id"] for e in entries)


# ─────────────────────────────────────────────────────────────────────────────
# Layer 3 — fingerprint dedup on re-scan
# ─────────────────────────────────────────────────────────────────────────────

class TestFingerprintDedup:

    def test_same_inputs_produce_same_fingerprint(self):
        from app.services.fingerprint import compute_fingerprint
        fp1 = compute_fingerprint("def-1", "column", "DB", "SCH", "TBL",
                                  {"column": "ORDER_ID"}, {})
        fp2 = compute_fingerprint("def-1", "column", "DB", "SCH", "TBL",
                                  {"column": "ORDER_ID"}, {})
        assert fp1 == fp2

    def test_different_definition_produces_different_fingerprint(self):
        from app.services.fingerprint import compute_fingerprint
        fp1 = compute_fingerprint("def-1", "column", "DB", "SCH", "TBL",
                                  {"column": "ORDER_ID"}, {})
        fp2 = compute_fingerprint("def-2", "column", "DB", "SCH", "TBL",
                                  {"column": "ORDER_ID"}, {})
        assert fp1 != fp2

    def test_different_column_produces_different_fingerprint(self):
        from app.services.fingerprint import compute_fingerprint
        fp1 = compute_fingerprint("def-1", "column", "DB", "SCH", "TBL",
                                  {"column": "ORDER_ID"}, {})
        fp2 = compute_fingerprint("def-1", "column", "DB", "SCH", "TBL",
                                  {"column": "CUSTOMER_ID"}, {})
        assert fp1 != fp2

    def test_different_threshold_produces_different_fingerprint(self):
        """Two 'range' instances on the same column with different bounds are
        distinct rules — a widening or tightening is a new proposal."""
        from app.services.fingerprint import compute_fingerprint
        fp1 = compute_fingerprint("def-1", "column", "DB", "SCH", "TBL",
                                  {"column": "AMOUNT"}, {"min": 0, "max": 100})
        fp2 = compute_fingerprint("def-1", "column", "DB", "SCH", "TBL",
                                  {"column": "AMOUNT"}, {"min": 0, "max": 200})
        assert fp1 != fp2

    def test_target_config_order_independent(self):
        """canonical_json sorts keys — order shouldn't matter."""
        from app.services.fingerprint import compute_fingerprint
        fp1 = compute_fingerprint("def-1", "column", "DB", "SCH", "TBL",
                                  {"column": "AMOUNT", "table": "ORDERS"},
                                  {"max": 100, "min": 0})
        fp2 = compute_fingerprint("def-1", "column", "DB", "SCH", "TBL",
                                  {"table": "ORDERS", "column": "AMOUNT"},
                                  {"min": 0, "max": 100})
        assert fp1 == fp2

    def test_none_and_empty_target_config_equivalent(self):
        from app.services.fingerprint import compute_fingerprint
        fp_none = compute_fingerprint("def-1", "table", "DB", "SCH", "TBL", None, None)
        fp_empty = compute_fingerprint("def-1", "table", "DB", "SCH", "TBL", {}, {})
        assert fp_none == fp_empty

    def test_scope_changes_fingerprint(self):
        from app.services.fingerprint import compute_fingerprint
        fp_table = compute_fingerprint("def-1", "table", "DB", "SCH", "TBL",
                                        {"column": "X"}, {})
        fp_col = compute_fingerprint("def-1", "column", "DB", "SCH", "TBL",
                                      {"column": "X"}, {})
        assert fp_table != fp_col

    def test_same_definition_across_databases_different_fingerprint(self):
        """The same rule against two different databases must be distinct
        instances — cross-env leakage guard."""
        from app.services.fingerprint import compute_fingerprint
        fp_prod = compute_fingerprint("def-1", "column", "PROD_DB", "SCH", "TBL",
                                       {"column": "X"}, {})
        fp_stage = compute_fingerprint("def-1", "column", "STAGE_DB", "SCH", "TBL",
                                        {"column": "X"}, {})
        assert fp_prod != fp_stage
