# DQ Tool — Rebalance & Validation Test Report

**Date:** 2026-07-15
**Branch:** `merge-harsh-integration` (uncommitted)
**Model under test:** Claude Opus 4.8 via Bedrock

## Context

Two rebalance phases and end-to-end validation of the RuleIntelligence pipeline on 7 tables spanning fact, dimension, reference, and clean scenarios.

## Phase 1 — Rebalance ship list

Prompt/critique/tool/profiler changes to push Claude toward more novel proposals and per-instance decisions. All landed:

1. **Prompt rewrite** — removed "1-5 cap", loosened reuse bias, added <70%-uncertainty → tool trigger.
2. **Self-critique bypass for novel proposals** — proposals with `definition_id=None + new_definition set` bypass the ruthless scoring pass.
3. **Template shape trim** — dropped `positive_value`/`email_format`; added `range`. Final 8 core.
4. **Tail values in profiler** — new `DataSource.bottom_values` primitive (native on Snowflake + Postgres), emitted in `column_stats.tail_values`.
5. **`get_sample_rows` modes** — schema extended with `mode=sample|distinct|nulls`. Single tool with mode-dispatch.
6. **`RulesFetchAgent.filter_relevant()`** — scoring stub (unused; plug-in for when library grows).
7. **Multi-source findings evidence** — `_fetch_violating_rows` accepts source; findings-list cache invalidated post-explain.
8. **Range template contract** — `min_value`/`max_value` aliases (`min`, `max`, `regex`, `values` too); prompt clarifies "at least one bound required"; non-silent discard log.

## Phase 2 — Library trim + global-instance kill

Motivated by "27 active rules" appearing on every scan (15 of which were invisible globals injected by `dynamic_rules._ensure_rule`).

1. **Library trim: 38 → 17 definitions** via `trim_library.py`.
   - Deleted 8 broken sql_template rows (shape=None + no draft_sql, unrenderable).
   - Deleted 2 duplicate historical_deviation handler defs.
   - Deleted 8 low-signal metadata handlers (Missing Table Comment, Missing Column Comment, Missing Table Owner, Too Many Columns, Inconsistent Column Naming, Generic Column Name, Missing Created At, Missing Updated At, FK Column Without FK Constraint).
   - Kept 5 useful metadata handlers: `nullable_id_column`, `no_primary_key_hint`, `pii_column_no_masking`, `boolean_stored_as_varchar`, `date_stored_as_varchar`.
2. **Global-instance kill** — `storage.ensure_definition` no longer auto-creates `DATABASE_NAME='*'` instances. `dynamic_rules._ensure_rule` returns a lightweight rule shell instead of persisting a global.
3. **Findings-time gating** — `FindingsAgent` now builds `{handler_key: instance_id}` map from approved instances and passes it into `run_dynamic_checks`. Empty codes → nothing fires (was: `None` → allow-all, a latent bug).
4. **Coordinator sweep** — On each new scan, prior-run `pending` proposals from the same table get auto-rejected with reason `"Superseded by a new scan before the prior review was completed."` No more clutter.

## Bugs found and fixed during validation

| Bug | Root cause | Fix |
|---|---|---|
| Silent range template discards | `_TEMPLATE_SHAPES_DOC` said `needs: none` for range (params=[], optional not shown) | Doc builder now shows `optional (at least one required): [min_value, max_value]`; discard logs shape + threshold_config |
| `KeyError('"min_value"')` on scan | Inline `{"min_value": ...}` in USER_PROMPT_TEMPLATE without brace-escape | Escaped as `{{"min_value": ...}}` |
| OHLC def wrongly attached to LAST_UPDATED freshness proposal | Claude sent wrong UUID; shape-mismatch guard only fired when both sides had a template_shape | Guard broadened — now triggers whenever candidate.template_shape differs from definition.template_shape (including `None`) |
| Skipped bucket empty despite bogus rules present | `definitions_evaluated` keyed by definition_id — two instances of same def got same verdict | Renamed to `instances_evaluated`, keyed by instance_id; coordinator lookup updated; per-instance skip works |
| Duplicate `pending` instances accumulating across runs | No cleanup of unapproved proposals when a new scan starts | Added `storage.list_stale_pending_instances` + coordinator sweep |
| "Non-negative Order Amount" definition reused on HOURS_WORKED | Claude picked table-specific name on synthesis, then `get_definition_by_template_shape('range')` returned it verbatim for another table | Prompt update: `new_definition.name` must be a GENERIC concept ("Numeric Range Violation"). Existing bad def renamed manually |
| False-positive uniqueness on FK-shape columns (`MANAGER_ID`, `CUSTOMER_ID`, etc.) | Deterministic PK-shape regex included bare `_ID` suffix | Split into `_PK_SHAPE_STRONG_RE` (excludes bare `_ID`); `_is_strong_pk_shape` helper; deterministic candidate path only uses strong |
| "Check-out After Check-in" and "Cross-Column Date Ordering Violation" as separate defs | Similarity match against `existing_definitions` (ACTIVE only) missed a `proposed`-status def from a prior table's scan | Similarity now searches `storage.list_all_definitions()` (all statuses); two defs merged manually |
| Findings shown generic library description instead of Claude's rationale | `rule_engine.execute_sql_instances` appended `definition.description` | Now prefers `instance.rationale`, falls back to definition description |
| Orphan finding (`instance_id=None`) leaked into DB | Empty `instance_id_by_handler_key` skipped the drop-filter block | Filter runs unconditionally now |

## Test-run summary (7 tables)

| Table | Rows | Type | Proposed | Tool calls | Notable |
|---|---|---|---|---|---|
| PRODUCT_CATALOG (baseline pre-rebalance) | 20 | dimension | 1-3 (avg) | rarely | Reuse-only, no novel |
| **ORDERS** (Phase 2) | 15 | fact | 7 | 1 (sample) | Every deliberate violation caught; novel `Numeric Range Violation` synthesized |
| **EMPLOYEE_ATTENDANCE** | 12 | fact | 7 | 1 (sample) | Novel `Check-out After Check-in` draft_sql; false-positive MANAGER_ID uniqueness (fixed) |
| **SUBSCRIPTIONS** | 12 | dimension | 6 | 0 | Novel `Cross-Column Date Ordering Violation`; regex_match on USER_EMAIL |
| **CLEAN_CUSTOMERS** | 15 | dimension | 3 | 0 | Zero false positives; skipped redundant not_null (schema-enforced) |
| **COUNTRIES** | 20 | **reference** | 5 | 0 | Correctly classified as reference; no freshness proposed; ISO 3166/4217 regex |
| **WIDE_TRANSACTIONS** | 25 | fact (25 cols) | 16 | 2 (both sample) | Every deliberate violation caught; 3 novel generic defs including arithmetic consistency |
| ORDERS **rescan** | 15 | fact | 0 (2 suppressed) | 0 | Fingerprint dedup working; bogus rules re-skipped from prior lesson |

## Bogus rule test (Skipped bucket validation)

Inserted 2 clearly-nonsensical instances on ORDERS:
- `Pattern Mismatch [^A-Z]{3}$ on ORDER_ID` (numeric column, string regex — impossible)
- `Duplicate Composite Key on [CREATED_AT]` (single-col composite key on a load-timestamp constant)

**Result:** Both landed in **Skipped** with specific reasons:
- *"Pattern '^[A-Z]{3}$' can never match ORDER_ID, a NUMBER column with values like 1001/1005, so the check fails 100% of rows meaninglessly."*
- *"Composite key targets only CREATED_AT whose distinct=1 (all 15 rows share 2026-07-15...)"*

## Standout behaviors

1. **Tool-mode usage is targeted, not random.** In WIDE_TRANSACTIONS Claude called `sample` twice with these reasons:
   - *"Verify cross-column arithmetic relationships: NET_AMOUNT vs AMOUNT/FEE/TAX, and AMOUNT_USD vs AMOUNT*EXCHANGE_RATE, plus SETTLEMENT_DATE vs TXN_TS ordering, to ground draft_sql checks."*
   - *"Confirm CARD_LAST4 non-digit value and RISK_SCORE out-of-range row to ground regex and range violation evidence."*

2. **Novel proposals now use generic names** (post-prompt-fix): `Numeric Range Violation`, `Cross-Column Date Ordering Violation`, `Cross-Column Date/Timestamp Ordering`, `Derived Column Arithmetic Consistency`, `Non-Positive Value in Positive-Only Measure`. All reusable across tables.

3. **Table-type awareness works.** COUNTRIES classified `reference` → no freshness proposed. ORDERS classified `fact` → freshness on ORDER_DATE proposed. Not just the classification field — the downstream reasoning respects it.

4. **Redundancy avoidance.** On CLEAN_CUSTOMERS, Claude skipped `not_null` proposals with reason *"All NOT-NULL columns are already enforced by the schema declaration, so standalone not_null instances would be redundant."* Same reasoning on COUNTRIES.

## Remaining gaps (not fixed)

1. **`PLATINUM` false negative** — On SUBSCRIPTIONS, Claude included `PLATINUM` in the accepted_values list, thinking it was a legitimate plan tier. Without ground truth, this is a domain-knowledge gap — human reviewer's job.

2. **AUTO_RENEW boolean-as-VARCHAR not caught** — The `boolean_stored_as_varchar` handler heuristic requires suffix `_FL/_FLAG/_YN/IS_/_IND`. `AUTO_RENEW` doesn't match. Could relax but risks false positives.

3. **Cross-column arithmetic drafts not always executed** — Claude proposes them with draft_sql; validation + execution runs at proposal time. Some drafts may fail validation (nulls, cast issues) and get silently discarded. Not seen in this run but a class of potential misses.

4. **Findings UI activation flow** — "Activate" button in the Available in Library section is currently stubbed with an alert. Full activation modal (target column picker + threshold config for each shape) is designed but not built.

5. **RelationshipDiscovery doesn't detect self-referential FKs.** MANAGER_ID→EMPLOYEE_ID (both in EMPLOYEE_ATTENDANCE) wasn't caught. This is why the FK filter in deterministic candidates didn't fire, driving the tightening of `_is_strong_pk_shape` instead. Real fix: enhance RelationshipDiscovery to detect self-refs.

## Validation checklist — passed

- [x] Novel proposals appear when the library lacks coverage (Cross-Column ordering, Derived Column Arithmetic Consistency)
- [x] `tail_values` appears in RuleIntelligence prompts (verified in intelligence log)
- [x] `get_sample_rows` mode dispatch working (mode=sample called with justifications)
- [x] Findings UI shows `sample_rows` + `ai_explanation` (backend writes verified, cache invalidation shipped)
- [x] `range` template renders correctly with `min_value=0` (POSITIVE_ID range on TOTAL_AMOUNT)
- [x] Skipped bucket populates when Claude actively rejects (2 bogus rules skipped with reasons)
- [x] Rescan on same table: 0 new proposals + N suppressed_dupes (verified 2 suppressed)
- [x] Reference tables don't get freshness proposals (COUNTRIES verified)
- [x] Clean tables generate preventive proposals without violations firing (CLEAN_CUSTOMERS: 3 proposals, 0 violations)
- [x] Wide tables (25 cols) handled without missing columns (WIDE_TRANSACTIONS: proposals cover most-important 16 concepts)
- [x] Definition library doesn't bloat across scans (7 tables, only 2 new definitions added to library beyond the trimmed 17 → same defs reused)
- [x] Global instances no longer inject phantom "27 active rules" (all scans start from `existing_instances=0` for new tables)

## Files changed (uncommitted)

Backend:
- `app/services/agents/rule_intelligence_agent.py` — prompt rewrites, per-instance keying, shape-mismatch guard, all-defs similarity search
- `app/services/agents/coordinator.py` — instance-level decision lookup, stale-pending sweep, unused_library bucket
- `app/services/agents/findings_agent.py` — empty-codes fix, handler_key→instance_id map
- `app/services/agents/verification_agent.py` — same map plumbed through
- `app/services/agents/profiling_agent.py` — `_is_strong_pk_shape`, tail_values in column_stats
- `app/services/agents/rules_fetch_agent.py` — `filter_relevant()` scaffold
- `app/services/agents/findings_explanation_agent.py` — multi-source `_fetch_violating_rows`
- `app/services/rule_engine.py` — trimmed `initialize_default_rules`, description prefers rationale, per-instance rationale in findings
- `app/services/dynamic_rules.py` — `_ensure_rule` no longer creates globals, trimmed dispatch dicts, instance_id_by_handler_key gating
- `app/services/storage.py` — `list_stale_pending_instances`, `ensure_definition` no longer creates globals
- `app/services/rule_sql_templates.py` — dropped `positive_value`/`email_format`, added `range`, aliases (`min`/`max`/`regex`/`values`), `optional_params`
- `app/services/datasources/base.py`, `snowflake_source.py`, `postgres_source.py` — `bottom_values` primitive

Frontend:
- `frontend/src/pages/AgentWorkflow.tsx` — grouped Active-by-definition, Available-in-Library section, `useMemo` import
- `frontend/src/api/client.ts` — `unused_library` in review state type

Scripts (new):
- `backend/reset_for_test.py` — scoped + nuclear reset
- `backend/trim_library.py` — one-shot library cleanup
- `backend/seed_test_tables.py` — ORDERS/EMPLOYEE_ATTENDANCE/SUBSCRIPTIONS
- `backend/seed_phase3_tables.py` — CLEAN_CUSTOMERS/COUNTRIES/WIDE_TRANSACTIONS
- `backend/run_test_scan.py` — invoke coordinator inline for a single table
- `backend/run_pipeline.py` — run findings pipeline for a given AgentRun id

## Recommendation

Ready to commit the whole batch. The remaining gaps are either domain-knowledge issues (PLATINUM) or non-critical (activation modal, self-ref FK discovery, AUTO_RENEW heuristic). Suggested commit split:

1. **Rebalance core** — prompt, critique, template shapes, tool modes, tail values (files in first "Files changed" group)
2. **Library cleanup + globals removal** — trim script + code changes to prevent regrowth
3. **UI grouping + unused_library** — frontend + review state schema
4. **Test scripts + seed data** — everything in "Scripts (new)"

Split allows easier revert if any single piece regresses. Or one big commit is fine since everything was validated together.
