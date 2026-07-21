# DQ Tool — Test Suite Report

Written 2026-07-20 as part of the Rule-Intelligence / Findings-lifecycle
verification pass.

## Summary

| Kind        | Files | Tests | Status |
|-------------|-------|-------|--------|
| Unit        | 8     | 120   | ✅ all pass |
| Integration | 1     | 13    | ⏸ gated on `RUN_INTEGRATION=1` (requires Snowflake + Bedrock) |

Run everything the harness can run offline:

```
cd backend && python -m pytest tests/ -q
```

Run integration tests against a real Snowflake account (`backend/.env` must
be filled in, SSO logged in):

```
python -m tests.seed_dq_issue_tables          # one-time table setup
RUN_INTEGRATION=1 python -m pytest tests/test_integration_end_to_end.py -v
```

## What each file covers

### Existing (fixed regressions)

`tests/test_agentic_features.py` (28 tests) — Bedrock tool-use loop, sample
tool safety guardrails, self-critique scoring, draft-SQL repair. Two
regressions fixed as part of this pass:

  * `_execute_sample_tool` requires `agent._source` (added during the
    2026-07-15 rebalance). Tests now init `_source=None` so the sf_session
    fallback path exercises correctly.
  * `_self_critique_proposals` signature: `run_id` is now required and the
    helper switched from `ask_claude` to `ask_claude_json`. Tests updated
    to reflect current contract + a new test verifies novel proposals
    bypass critique entirely (audit fix from 2026-07-15).

### New

`tests/test_definition_dedup.py` (23 tests) — the RuleIntelligence library
dedup logic, in three layers.

* **Layer 1** — `_find_similar_definition`: exact-name-first, then fuzzy
  overlap. Regression coverage for the "Cross-Column Timestamp Ordering
  ×2" bug (score 0.41 < 0.55 threshold before the exact-name path was
  added).
* **Layer 2** — same-run collapsing (`new_definition_key`): the
  WIDE_TRANSACTIONS 3× duplicate case reproduced and asserted fixed.
* **Layer 3** — fingerprint dedup across runs: canonical JSON ordering,
  scope + threshold + target invariants.

`tests/test_scan_finalizer.py` (18 tests) — the incident lifecycle state
machine. All four branches (CREATE / UPDATE / RESOLVE / REOPEN), mute
semantics, `fail_history` cap at 50, first_detected_at preservation,
mixed batches with all four outcomes in one scan.

`tests/test_schema_drift.py` (11 tests) — Tier-1 drift detection. First
scan returns `[]` (no false positives), column-added / removed /
type-changed / nullability-changed. Type normalization
(`NUMBER(38,0)` == `NUMBER`) and Snowflake `is_nullable="YES"` handling.

`tests/test_rule_engine.py` (9 tests) — sample-count reconciliation
regression coverage: if `len(sample_rows) > FAILED_COUNT`, override count.
Also aggregate-rule exemption (freshness, row_count_*), evidence contract
keys (`fail_count`, `total_count`, `sample_rows` always present), and
Postgres lowercase-keys compatibility.

`tests/test_sample_tool_modes.py` (13 tests) — the mode dispatch added in
the 2026-07-15 rebalance: `sample` / `distinct` / `nulls`. Column-name
validation, WHERE-clause keyword blocklist, fallback to `sf_session`
when no per-run `_source` is set.

`tests/test_reasoning_persistence.py` (10 tests) — classification-decision
propagation (`get_skip_ids`, `get_severity_override`, keep_running
defaults), intelligence-log shape (`signals_used.sample_tool_calls`,
`past_context_health` empty / ok / error semantics), and per-instance
rationale round-trip from proposal → RULE_INSTANCES row.

`tests/test_candidate_processing.py` (8 tests) — the many-branch dispatch
in `_process_candidate`:
  - Active/pending fingerprint → `suppressed{reason='already_active|pending'}`
  - Rejected fingerprint with same evidence → suppress
  - Rejected fingerprint with different evidence → re-propose
  - Unknown definition_id → promotion via template_shape canonical (audit fix #9)
  - Shape mismatch → discard wrong def_id, fall through
  - `_ground_accepted_values` trims to observed values

### Integration (gated)

`tests/test_integration_end_to_end.py` (13 tests, all `@pytest.mark.integration`) —

  * **Coverage:** `test_planted_issue_is_caught` — parametrized over
    `tests/seed_dq_issue_tables.PLANTED_ISSUES` (9 planted DQ issues across
    8 tables). Each issue must surface as a proposal on the correct
    column, and finding evidence must fall in the expected fail-count
    range.
  * **Lifecycle:** first scan creates findings → rescan with no data
    change UPDATEs them (first_detected_at preserved) → fix data + rescan
    RESOLVEs → reintroduce bad data within 7 days REOPENs
    (reopened_count >= 1).
  * **Library dedup:** repeated scans of the same table don't grow
    `RULE_DEFINITIONS.SOURCE='claude'` linearly.

The seed script `tests/seed_dq_issue_tables.py` creates 9 dedicated tables
in `PLAYGROUND_DB.TEST_DQ_ISSUES` with 9 documented, planted issues:

| Table              | Column       | Planted issue          |
|--------------------|--------------|------------------------|
| NULL_HEAVY         | CUSTOMER_ID  | 10% NULLs              |
| DUPLICATE_KEY      | USER_ID      | 5 duplicate rows       |
| OUT_OF_RANGE       | AGE          | AGE=250                |
| OUT_OF_RANGE       | SCORE        | SCORE=-20, 150         |
| BAD_FORMAT         | EMAIL        | 4 malformed emails     |
| STALE_TIMESTAMPS   | LAST_UPDATED | 2018-2021 timestamps   |
| ENUM_VIOLATIONS    | STATUS       | 3 unknown states       |
| ORPHAN_FK          | PARENT_ID    | 3 orphan references    |
| NEGATIVE_AMOUNTS   | AMOUNT_USD   | 4 negative revenue rows|

## Bugs found during test authoring

1. **Test-code regressions (fixed):**
   * `_execute_sample_tool` requires `_source` attribute — 18
     pre-existing test call sites didn't initialize it → fixed.
   * `_self_critique_proposals` signature drift — pre-existing tests
     hadn't caught up to the current API → fixed.

2. **No new production bugs found**, but the test scaffolding is now in
   place to catch:
   * Definition-library duplication if same-run collapsing regresses.
   * Silent fingerprint-dedup regressions.
   * Lifecycle state-machine changes that break first_detected_at
     preservation or REOPEN window semantics.
   * Sample-tool safety guardrail bypasses.

## Coverage gaps intentionally deferred

* `Coordinator._execute` orchestration flow — the branching is complex
  and depends heavily on live storage state; covered pragmatically by
  the integration suite (which drives the coordinator end-to-end)
  rather than a unit-level rewrite.
* `RulesFetchAgent.filter_relevant()` — designed but not yet called from
  coordinator (per project memory). Add a test when it's wired in.
* Findings explanation agent + notification hooks — not part of Rule
  Intelligence proper.
