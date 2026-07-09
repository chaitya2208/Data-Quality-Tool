# Deferred & Future Work

> Running log of everything intentionally left out so far — either deferred per
> `mvp-scope.md`'s phasing, or discovered as a gap while building. Each entry
> says *why* it's deferred and *what unlocks it*, so nothing gets silently
> forgotten. Update this file whenever new work is knowingly deferred.
>
> Last updated: 2026-07-06 (through: connection layer, metadata tools,
> profiling tools, storage tools, 5 rule skills, Database Explorer + Table
> Profile Page frontend, SQL validation tools, LangGraph workflow, hybrid
> Claude rule recommendation, Rule Test Execution Agent, storage wiring +
> Human Approval APIs + Recommended Rules Page, Rule Execution Agent +
> Alert Agent + Active Rules Page + Alerts Dashboard, Rule Execution History
> Page, Table Health Page, Rule/Alert Business Explanations, Feedback Loop
> — including its follow-up live FALSE_POSITIVE verification and the
> unfiltered-recommended-rules S3/TLS gap discovered while doing it —,
> Manual Scan Options / schema-scope scanning, Background Job Progress /
> the live agent-activity feed, the remaining scan scopes + bulk Rule
> Execution, LangSmith tracing, closing the three biggest
> previously-tracked unlocks in one session — sample-first profiling for
> large tables, the PII/Sensitivity Classification Agent, and the
> scheduler with real alert auto-resolve — and, in a following session,
> Sample Failed Rows / ALERT_VIOLATION_SAMPLES (§6/§15), the Volume skill's
> historical-average comparison (§6), and a Settings page for schedule
> management (closing the API-only gap noted under §28), and in a further
> session: Claude-authored SQL for its own rules (closing §16's "not built"
> SQL-generation gap), rule source badges (Claude vs. Template), scan
> auto-navigation fix, and the Agentic Deep Scan design decision (§30)).

---

## 1. Safety-critical, not yet built

These were called out as **mandatory hard gates** in `architecture.md` but
don't exist in code yet. Nothing dangerous has happened because we haven't
built the pieces that would need them (no rule execution against approved
rules) — but they must land before that does.

- **SQL Validator** (`architecture.md` §6) — **exists, wired in, and now
  wired into storage too.** `tools/sql_validation_tools.py`
  (`validate_select_only()`, `detect_forbidden_keywords()`,
  `validate_no_semicolon_chaining()`, `validate_allowed_table()`, composed
  by `validate_sql()`), built on `sqlglot` per `architecture.md` §6's
  explicit call for a real parser. `agents/sql_validation_agent.py` calls
  it on every rule's `generated_sql` before the graph/pipeline returns —
  verified against every real template in `rule_template_tools.py` (all
  pass) and a battery of attack strings. **Storage wiring done**:
  `main.py`'s `recommend-rules` route now calls
  `storage_tools.store_recommended_rule()` per rule after
  `run_dq_workflow()` returns, mapping `validation_status`
  (`VALID`/`INVALID`) into storage's `VALIDATION_STATUS`
  (`PASSED`/`FAILED`) — verified end-to-end against real Snowflake data
  (14 real `RECOMMENDED_RULES` rows persisted for
  `REPLAY_BRONZE_INGESTION_AUDIT_TBL`). The Edit route
  (`PATCH /api/rules/{rule_id}/edit`) re-runs `validate_sql()` on any
  edited SQL and updates `VALIDATION_STATUS` accordingly, and the Approve
  route refuses (400) to approve a rule whose `VALIDATION_STATUS != PASSED`
  — this is the hard gate architecture.md §6 requires before a Rule
  Execution Agent (still unbuilt) would ever run `generated_sql` for real.
- **PII masking middleware** — `COLUMN_PROFILES` has `IS_PII` /
  `SENSITIVITY_LEVEL` / `LLM_SHARING_POLICY` columns, but nothing populates
  them (see §3) — no PII/Sensitivity Agent exists yet, so every column
  profile's `is_pii` is always `False` today. **The enforcement point now
  exists**: `tools/claude_tools.py`'s `_mask_column_profile()` strips
  min/max/top_values for any column with `is_pii=True` before it reaches
  Claude (stats like null%/distinct_count still pass through, per
  architecture.md §7's `ALLOW_STATS_ONLY` tier) — verified directly that
  masking triggers correctly. This is enforced today for the one LLM call
  site that exists (`recommend_rules_with_claude()`); it does nothing yet in
  practice because no column is ever classified `is_pii=True` until the PII
  agent exists. Building that agent remains the real unlock.

## 2. Rule Fingerprint / Deduplication

- `RECOMMENDED_RULES.RULE_FINGERPRINT` column exists (SQL schema).
  `storage_tools.store_recommended_rule()` accepts a `rule_fingerprint` param
  and stores it as given.
  **Nothing computes it.** None of the 5 skills set this field on their
  output candidates.
- Missing piece: `rule_engine/rule_deduplicator.py` — should hash
  `table + column + rule_type + normalized_sql` into a stable string, so a
  re-scan can detect "already proposed this" instead of duplicating it.
- Consequence today: profiling + running skills on the same table twice
  produces duplicate candidate rows with no automatic detection.

## 3. PII / Sensitivity Classification — **RESOLVED, see §27**

- `COLUMN_PROFILES.IS_PII` / `PII_TYPE` / `SENSITIVITY_LEVEL` /
  `LLM_SHARING_POLICY` are written by `store_column_profile()` but always at
  their defaults (`FALSE` / `NULL`) — `snowflake_profiling_tools.py` only
  computes statistics, it does not classify columns.
- Missing piece: a dedicated **PII/Sensitivity Agent** (per
  `architecture.md`) that inspects column names/samples and fills these
  fields in, before any real LLM-based recommendation reads from a column.
- **Resolved 2026-07-06**: `agents/pii_agent.py` now exists (deterministic
  regex/heuristics + LLM-assist for ambiguous columns), wired into
  `graphs/dq_workflow_graph.py` between profiling and rule recommendation.
  See §27 for the full writeup and live verification evidence.

## 4. Sampling / Large-Table Profiling — **RESOLVED, see §26**

- `snowflake_profiling_tools.py` runs **full-table scans** for every stat
  (`COUNT(*)`, `COUNT(DISTINCT ...)`, `MIN`/`MAX`, top-values `GROUP BY`).
  Fine for MVP-sized tables (tested against 12-row and 1-row tables).
- Real tables in this account go up to **31.5M rows**
  (`REPLAY_BRONZE_INGESTION_RECORDS_TBL`). Profiling one of those today would
  mean multiple full scans of 31M rows, once per column.
- **Resolved 2026-07-06**: tables at/above 100k rows (checked cheaply via
  `SHOW TABLES` metadata) now profile from a fixed-size `SAMPLE (50_000
  ROWS)` instead of a full scan. See §26 for the full writeup, including a
  real live-verified run against the actual 31.5M-row table.
- Missing piece: sample-first profiling (`SAMPLE (...)` / percentage-based),
  with depth as a visible, user-facing setting — this was already called out
  in `architecture.md` §10 and `mvp-scope.md`, not a new gap.

## 5. Real Query/Profile Cancellation

- The frontend blocks switching database/schema/table while a profile is
  running (fixed defensively — see below), but this only stops the *UI* from
  showing a mismatched result. It does **not** cancel the backend's in-flight
  Snowflake queries.
- If a user starts profiling a 31M-row table, there is currently no way to
  actually stop the scan mid-flight short of killing the backend process.
- Missing piece: real cancellation needs explicit Snowflake session/query
  cancellation (`ABORT_STATEMENT` or similar), wired to a cancel action from
  the UI — non-trivial, explicitly deferred rather than half-built.

## 6. Rule Execution & Test-Run

- **Rule Test Execution Agent now exists**:
  `agents/rule_test_execution_agent.py`. Runs every `VALID` rule's
  `generated_sql` against the read-only source connection right now
  (`run_query(..., timeout=30)`, statement timeout added to
  `snowflake_connection.run_query()`/`_run()` alongside this agent, per
  architecture.md §6/§10's "enforce statement timeout... on every source
  query" — previously unimplemented), and sets `test_status`
  (`PENDING`/`PASSED`/`FAILED`/`ERROR`) + `test_result`
  (`would_pass`/`total_count`/`failed_count`/`failure_percentage`/
  `error_message`/`evaluated_at`) on each rule dict. Wired into both
  `agents/scan_pipeline.py` and `graphs/dq_workflow_graph.py` as the step
  after SQL validation; `main.py`'s `recommend-rules` route now returns
  `tested_rules` (was `validated_rules`).
- **Bug fixed as part of this**: `tools/rule_template_tools.uniqueness_sql()`
  was missing a `TOTAL_COUNT` column — every other template (completeness,
  accepted-values, positive-amount, freshness, volume) returns both
  `FAILED_COUNT` and `TOTAL_COUNT`; uniqueness only returned `FAILED_COUNT`.
  Fixed so `failure_percentage` can be computed uniformly across all rule
  types.
- **Vocabulary gap, flagged deliberately**: this agent's `test_status`
  includes `ERROR` (SQL genuinely failed at execution time), but
  `04_create_rule_tables.sql`'s comment on `TEST_STATUS` only lists
  `PENDING/PASSED/FAILED`. `ERROR` is kept distinct from `FAILED` on purpose
  — `FAILED` means the query ran fine and found bad rows, `ERROR` means the
  rule's SQL itself is broken; collapsing them would hide that distinction
  from a human reviewer. Same category of gap as
  `sql_validation_agent.py`'s `VALID`/`INVALID` vs. storage's
  `PENDING`/`PASSED`/`FAILED` — mapping agent vocabulary into whatever
  storage's `TEST_STATUS` column ultimately accepts is the still-pending
  storage-wiring work's job (see §1 above / `context.md`'s "next step"),
  not solved here.
- **`test_result.sample_failed_rows` — RESOLVED, see §29.**
- Rules that are `INVALID` (no SQL, or unsafe SQL) are **not** executed —
  they stay `test_status="PENDING"`, `test_result=None`. Same "recommended,
  not yet executable" treatment as elsewhere in this codebase for
  Claude-sourced rule types with no template.
- **Storage now wired** (see §14/§16): `RECOMMENDED_RULES.TEST_RESULT` /
  `TEST_STATUS` are persisted by `main.py`'s `recommend-rules` route.
- **`RULES.RULE_EXECUTION_HISTORY` is now written to** — see §16.
  `agents/rule_execution_agent.py` (not this test-execution agent) is the
  Rule Execution Agent architecture.md §4b called for, running *approved*
  rules on manual trigger via `POST /api/rules/{rule_id}/run`. This
  test-execution agent still only tests *recommended* rules once, at
  recommendation time, before a human ever sees them — a distinct,
  earlier step in the pipeline that remains as originally built.
- **Volume skill's historical-average comparison — RESOLVED, see §29.**
  Same applies to any future Distribution-drift or anomaly-detection rule
  type from the original README (still not built — this only closed the
  Volume case).

## 7. Orchestration (LangGraph)

- **First graph now exists**: `graphs/dq_workflow_graph.py` —
  `metadata_agent → profiling_agent → rule_recommendation_agent →
  sql_generation_agent → sql_validation_agent → END`, using `langgraph`
  (added to `requirements.txt`). Each node wraps a deterministic agent in
  `agents/*.py` (`metadata_agent.py`, `profiling_agent.py`,
  `rule_recommendation_agent.py`, `sql_generation_agent.py`,
  `sql_validation_agent.py`) — no LLM call anywhere in this graph yet, per
  explicit instruction to get deterministic agents working before any
  Claude-based one. Exposed via `POST .../tables/{table}/recommend-rules` in
  `main.py`. Verified end-to-end against real Snowflake data
  (`PLAYGROUND_DB.BRONZE.REPLAY_BRONZE_INGESTION_AUDIT_TBL`): one HTTP call
  returns 5 recommended rules, each with `generated_sql` and
  `validation_status: VALID`.
- A parallel plain-Python chain (no LangGraph) also exists:
  `agents/scan_pipeline.py`, calling the same 5 agents directly. Kept
  alongside the graph rather than deleted — simpler to call from a script/
  test, and the ask treated "deterministic agents" and "LangGraph workflow"
  as two separate milestones.
- **Updated**: the graph now also has `rule_test_execution_agent` as a
  sixth node (Metadata → Profiling → Recommend → SQL-gen → SQL-validate →
  Test-execute, per `architecture.md` §4a), and **persistence is wired**
  — but at the route level (`main.py`), not as a seventh graph node (see
  §1 above and §14 below for the resolved design questions on this). The
  graph/`scan_pipeline.py` themselves remain pure compute, returning
  results in memory; `main.py`'s recommend-rules route is what calls
  `store_profile_result()`/`store_recommended_rule()` after the graph
  returns.
- **Still not built as a graph**: Human Approval is real now (see §14), but
  as async REST routes per `architecture.md` §8 (not a graph pause, which
  was never the design) — it is not, and was never meant to be, a graph
  node. **Rule Execution (approved rules → alerts, §4b) is now real too**
  (see §16), but also as plain agent/route code, not a graph node — same
  pattern as the recommendation flow's persistence: architecture.md's
  diagram is a conceptual flow, not a literal instruction that every step
  must be a LangGraph node. No graph covers §4b; `dq_workflow_graph.py`
  still only covers §4a (recommendation).
- **Rule Recommendation is now hybrid, not 100% deterministic** — see §13
  below for the Claude integration itself. The graph/pipeline node names are
  unchanged (`rule_recommendation_agent`) but that agent now calls Claude
  internally as well as the 5 skills.

## 8. Storage tables created but unused

Reserved on purpose (see `04_create_rule_tables.sql` /
`02_create_core_tables.sql` comments) so no future migration is needed —
listed here so it's clear they're intentionally idle, not forgotten:

- `CORE.APP_CONNECTIONS` — no UI to manage multiple source connections yet
  (single connection via `.env` today).
- `PROFILING.METADATA_SNAPSHOTS` — raw discovery snapshots folded into
  `TABLE_PROFILES`/`COLUMN_PROFILES` for now; useful later for schema-drift
  diffing across scans.
- `RULES.USER_FEEDBACK` — approve/reject/edit signals currently live inline
  on `REJECTED_RULES.REJECTION_REASON` / `ALERTS.STATUS`; a dedicated
  feedback table matters once retrieval-based learning (§9) needs to query
  feedback independent of the rule/alert it came from.

## 9. Feedback Learning (MVP2)

- Per `mvp-scope.md`: retrieval-based few-shot learning from past
  approve/reject decisions, not fine-tuning. Not started — an LLM
  recommendation call now exists (§13) but nothing stores/retrieves feedback
  from it yet.

## 10. Scheduling / Non-Interactive Auth (MVP2) — **scheduling RESOLVED, see §28; auth half still open**

- `get_source_connection()` uses **external-browser SSO** — interactive by
  design, requires a human at a browser. Fine for dev / manual scans.
- Scheduled/background scans (MVP2) need **key-pair auth** instead (service
  account + RSA key, no browser). Noted directly in
  `tools/snowflake_connection.py`'s docstring; not implemented.
- **Resolved 2026-07-06 (scheduling half only)**: `scheduler.py` now
  exists — a real interval-based poll loop that re-runs approved rules and
  re-scans tables on a timer, with no manual click. It still runs on the
  same interactive-SSO connection this section describes, so it is not yet
  truly unattended -- the user has access to a key-pair auth key but needs
  to go through their organization first; that swap is the one piece of
  this section still genuinely open. See §28 for the full writeup.

## 11. App-owned storage: shared database, not a separate one

- Per `architecture.md`, source data and app state should live in separate
  databases (different trust levels: read-only vs. full-access).
- In practice: personal databases (`USER$...`) structurally block
  `CREATE TABLE` (Snowflake platform limitation, confirmed directly), and no
  other write-capable role/database was grantable. App storage
  (`CORE`/`PROFILING`/`RULES`/`ALERTS`/`LOGS` schemas) currently lives
  **inside `PLAYGROUND_DB`**, the same database used as the scan source —
  kept apart only by schema, not database.
- Consequence: the "read-only source" guarantee is enforced only by the SQL
  Validator (§1, not yet built) and by convention, not by a hard database
  boundary. Revisit if a genuinely separate write-capable database/role
  becomes available.

## 12. Rule thresholds are hardcoded MVP defaults

Every skill picked a concrete number where the original spec said something
vague ("low," "close," "few"). These are reasonable starting points, not
tuned values — worth revisiting once real approve/reject feedback exists:

- Completeness: null% ≤ **5%**
- Uniqueness: distinct/non-null ratio ≥ **98%**
- Validity (STATUS): ≤ **10** distinct values to propose accepted-values
- Freshness: max age **24 hours** (same default regardless of table)

`THRESHOLD_CONFIG` is stored per-rule and is user-editable at approval time,
so these defaults are a starting point, not a hard limit.

## 13. Frontend: approval / active-rules / alerts UI now built

- Built so far: Connections status, Database Explorer (DB → schema → table →
  columns), Table Profile Page, **Recommended Rules Page** (§14),
  **Active Rules Page** (§16), **Alerts Dashboard** (§16).
- **Not** built as originally specified: the two-column `⇒` drag-across UI
  from the original README. Built instead: a single sortable table with
  per-row Approve/Reject/Edit/View-details actions (flagged deviation —
  simpler to implement and test for MVP1, and functionally equivalent: a
  rule still moves from "pending" to a decided state on approve/reject).
  Revisit the two-column visual if the user wants it specifically; nothing
  about the API shape blocks building that UI later.
- Still not built: Agent progress/log view (backing table
  `LOGS.AGENT_RUN_LOGS` exists and is written to by
  `storage_tools.log_agent_run()`, but nothing displays it yet). No
  `IS_ACTIVE` toggle UI/route (Active Rules Page displays but can't flip
  it). No "run all approved rules" bulk action — "Run now" is one rule at
  a time only.

## 14. Human Approval APIs + storage wiring (previously deferred, now built)

- **Storage wiring** (previously the top-tracked gap, see §1): resolved.
  `main.py`'s `recommend-rules` route now persists both the table/column
  profile (`store_profile_result()`, split out of
  `snowflake_profiling_tools.profile_and_store_table()` so the
  already-computed profile can be stored without re-scanning) and every
  tested rule (`store_recommended_rule()` per rule), tied to the route's
  real `scan_id`.
- **New storage reads**: `storage_tools.list_recommended_rules()`,
  `get_recommended_rule()`, `update_recommended_rule()`. Approval status
  (`PENDING`/`APPROVED`/`REJECTED`) is **computed**, not stored — a LEFT
  JOIN against `APPROVED_RULES`/`REJECTED_RULES` on
  `ORIGINAL_RECOMMENDED_RULE_ID` in both list/get queries, since a rule's
  presence in those tables *is* its decision state (per
  `04_create_rule_tables.sql`'s design — no separate status column on
  `RECOMMENDED_RULES` for this).
- **New routes in `main.py`**: `GET /api/rules/recommended` (+ `?scan_id=`),
  `GET /api/rules/recommended/{rule_id}`, `POST /api/rules/{rule_id}/approve`,
  `POST /api/rules/{rule_id}/reject`, `PATCH /api/rules/{rule_id}/edit`.
  Built in Python/FastAPI, not Node — the ask specified Node but
  architecture.md's locked decision is Python-only backend (no Node BFF);
  the user acknowledged this explicitly when asking for the task.
- Approve is blocked (400) unless `validation_status == "PASSED"` — an
  approved rule is exactly what the Rule Execution Agent (§15, built after
  this) runs unattended, so `architecture.md` §6's hard gate must hold at
  approval time, not just at recommendation time. Edit re-runs `validate_sql()` when
  `generated_sql` changes and updates `validation_status` accordingly, so a
  rule fixed via Edit can become approvable. All three mutating routes
  return 409 (Conflict) on a rule that's already `APPROVED`/`REJECTED` — no
  "undo" route exists (one-way transition, matches `architecture.md` §8's
  approval flow, which has no reversal step either).
- **Status-vocabulary mapping resolved** (previously open questions,
  tracked across sessions): `validation_status`
  (`VALID`/`INVALID`) → storage's `VALIDATION_STATUS`
  (`PASSED`/`FAILED`); `test_status`
  (`PENDING`/`PASSED`/`FAILED`/`ERROR`) → storage's `TEST_STATUS`
  (`PENDING`/`PASSED`/`FAILED`, with `ERROR` collapsed into `FAILED`).
  Every rule is stored regardless of status — `INVALID`/`ERROR` ones
  included — so nothing Claude recommends is silently hidden from the
  approval screen; the richer detail survives in `test_result`/the
  response-only `validation_errors`, just not as a separate stored status
  value.
- **Frontend**: Recommended Rules Page added to `apps/web/src/App.jsx`
  (see §13), with a "Recommend rules" trigger button next to "Profile this
  table" in the Columns panel, and simple useState-based page nav (no
  router dependency, matching this codebase's existing plain-React style).
- **Verified end-to-end against real Snowflake and a live browser**:
  backend — recommend-rules persisted 14 real `RECOMMENDED_RULES` rows (+
  profile rows) for `REPLAY_BRONZE_INGESTION_AUDIT_TBL`; approve/reject/edit
  exercised against those real rows including the approve-blocked-on-INVALID
  400, the double-approve 409, and an edit that fixed unsafe/broken SQL and
  flipped `FAILED→PASSED`. Frontend — driven headlessly via Playwright
  against the real backend (system Chrome, since the corporate proxy's TLS
  interception blocks Playwright's own browser download, same root cause as
  the Bedrock TLS issue noted elsewhere in this doc): table renders with the
  correct columns, view-details expansion and edit+revalidate both work.
  One real bug found this way, not by code review: a `<ul>` nested inside a
  `<p>` in the detail view (invalid HTML → React hydration warning),
  fixed.

## 15. Rule Execution Agent + Alert Agent + Active Rules Page + Alerts Dashboard (previously the "most natural next step," now built)

architecture.md §4b's execution half of the workflow — the part the
recommendation/approval work (§14) explicitly left undone — is now real,
end to end: an approved rule can be run manually, its result is stored,
and a failure surfaces as a real alert a human can act on.

- **`agents/rule_execution_agent.py`** — `run_rule_execution_agent(rule_id)`.
  Flow: fetch the approved rule (`storage_tools.get_approved_rule()`, new)
  → re-validate its SQL (`validate_sql()`, defense in depth — the rule was
  already validated at recommendation/approval time, but re-checking here
  costs nothing and doesn't trust a status set once, earlier) → run it
  (`run_query(..., timeout=30)`) → `store_execution_result()` always → if
  the run FAILED, call `agents/alert_agent.run_alert_agent()`.
  **Deviation flagged**: unlike every other agent in this codebase
  (pure compute, persistence lives in the caller/route), this agent
  persists directly — the ask's own flow explicitly listed "store
  execution history" and "call Alert Agent" as steps *inside* the agent,
  not the route's. Kept as asked, flagged rather than silently
  restructured back to the usual split.
- **Four execution statuses**, not the three `RULE_EXECUTION_HISTORY.STATUS`
  schema comment lists (`PENDING/PASSED/FAILED`... actually no PENDING
  either, since a row is only ever written after a real attempt):
    - `PASSED` — ran, `failed_count == 0`.
    - `FAILED` — ran, `failed_count > 0` → triggers an alert.
    - `ERROR` — the SQL itself raised at execution time (query broke).
    - `SKIPPED` — never attempted: the rule is `IS_ACTIVE = false`, or
      re-validation failed (e.g. SQL edited/rotted since approval). Kept
      distinct from `ERROR` on purpose — "we tried and it broke" vs. "we
      deliberately didn't try" are different failure modes a human needs
      to tell apart.
- **`agents/alert_agent.py`** — `run_alert_agent(rule, execution_id, execution_result)`.
  Only creates an alert when `execution_result["status"] == "FAILED"`;
  returns `None` (no alert) for PASSED/ERROR/SKIPPED — an ERROR/SKIPPED
  run never actually told us whether the *data* is bad, so alerting on it
  would be a false signal. Builds title/description/severity/failed_count/
  failure_percentage and calls `storage_tools.store_alert()` (which itself
  sets `STATUS='OPEN'`/`CREATED_AT` — this agent doesn't set those).
  **Not built** (real future work, not silently expanded into this task):
  alert grouping/deduplication across repeated failures of the same rule,
  an LLM-generated explanation (architecture.md §5's "explained, grouped"
  alerts — only "created" is done), and `ALERT_VIOLATION_SAMPLES` (no
  agent computes sample failed rows anywhere yet, same gap as §6's
  `test_result.sample_failed_rows`).
- **`storage_tools.py` additions**: `get_approved_rule()`,
  `list_approved_rules()` (LEFT JOIN `RULE_EXECUTION_HISTORY`, `QUALIFY
  ROW_NUMBER() OVER (PARTITION BY a.RULE_ID ORDER BY h.STARTED_AT DESC) = 1`
  to get each rule's most recent execution in one query — partitioned by
  the *approved-rule* id, not the history table's id, so a never-run rule
  still gets exactly one row with NULL last-run fields instead of being
  dropped or double-counted), `list_alerts()` / `get_alert()` (both joined
  with `APPROVED_RULES` for database/schema/table/rule_name — `ALERTS`
  itself only stores `RULE_ID`), `get_alerts_summary()` (one aggregate
  query: total/critical/warning open counts, distinct rules failed today,
  distinct tables affected among open alerts). `update_alert_status()`
  already existed (built early, unused until now) and is reused as-is for
  accept/false-positive.
- **New routes in `main.py`**: `GET /api/rules/active` (Active Rules
  Page), `POST /api/rules/{rule_id}/run` (the agent above), `GET
  /api/alerts/summary`, `GET /api/alerts` (optional database_name/
  schema_name/table_name/severity/status/date filters, AND-ed together;
  `date` is an exact-day match via `TO_DATE()`, not a range), `POST
  /api/alerts/{alert_id}/accept`, `POST /api/alerts/{alert_id}/false-positive`.
- **Alert statuses**: only `OPEN → ACCEPTED / FALSE_POSITIVE` are wired,
  per the ask's explicit MVP scope. No `REJECTED` route (not requested).
  **No `RESOLVED` route, on purpose, not an oversight**: architecture.md
  §8 says "a passing rule auto-clears its alert on next run" — resolution
  is meant to happen automatically when a re-run passes, not by a human
  clicking a button. No scheduler/auto-rerun exists yet (§10), so there is
  no real event that would ever produce `RESOLVED` today; a manual
  "mark resolved" route would let a human set a status a real re-run could
  never have produced yet.
- **Frontend**: `apps/web/src/App.jsx` gained an `ActiveRulesPage`
  component (rule name/table/column, Active/Inactive badge — display-only,
  no toggle, see §13 — Rule SQL, Severity, Schedule placeholder since
  `schedule_config` is never set by anything yet, Last run status +
  failed/total counts, Last run timestamp, Run now button) and a new
  `apps/web/src/AlertsPage.jsx` (5 summary stat tiles per the dataviz
  skill's stat-tile contract, a filter bar, a recent-alerts table with
  Accept/False-positive actions). **Deviation flagged**: the ask specified
  `AlertsPage.tsx` — built as `.jsx` instead, since this project has no
  TypeScript configured anywhere (plain JS throughout `apps/web`, per
  architecture.md's stack decision); same category of deviation as
  building the Human Approval APIs in Python/FastAPI instead of Node (§14).
- **Verified end-to-end against real Snowflake and a live browser** (same
  Playwright-against-system-Chrome approach as §14, since Playwright's own
  browser download is blocked by the same corporate-proxy TLS interception
  noted elsewhere in this doc): synthetic approved rules run through
  `POST /run` correctly produced `PASSED` (no alert), `FAILED` (real
  `ALERTS` row, every field correct — title, description, severity,
  failed_count, failure_percentage, `STATUS=OPEN`), and `SKIPPED` (a
  `DROP TABLE` payload caught by re-validation, never executed) outcomes;
  404 on a nonexistent rule_id. Active Rules Page and Alerts Dashboard
  both confirmed rendering real data, filters working, and Run
  now/Accept/False-positive actions all correctly updating state after
  refetch, zero console errors in every check.

## 16. Claude integration (first LLM call in this codebase)

- **`tools/claude_tools.py`** — `recommend_rules_with_claude(input_json)`,
  called from the now-hybrid `agents/rule_recommendation_agent.py`. First
  LLM call anywhere in this codebase; everything before it (`tools/`,
  `skills/`, `agents/`, `graphs/`) was deterministic Python, per explicit
  instruction to build those first.
- **Connects via Amazon Bedrock**, not the first-party Anthropic API, per
  explicit instruction. Two Bedrock surfaces exist; only one works on this
  account — verified directly:
  - `AnthropicBedrockMantle` (newer Messages-API endpoint) → 403,
    `bedrock-mantle:CreateInference` not granted to this IAM user.
  - `AnthropicBedrock` (legacy `InvokeModel` API) → works, using an
    **inference-profile model ID** (`us.anthropic.claude-sonnet-5`) — the
    bare `anthropic.claude-sonnet-5` 404s on `InvokeModel` ("on-demand
    throughput isn't supported... use an inference profile").
  - Auth: `AWS_BEARER_TOKEN_BEDROCK` env var (Bedrock bearer-token auth,
    auto-detected by the SDK — no code-level credential handling needed).
    Added to `.env.example` alongside `AWS_REGION`.
- **Structured output**: `output_config.format` (native structured outputs)
  and `strict: true` on the tool definition **both 400 on this account's
  legacy Bedrock path** ("Extra inputs are not permitted") — verified
  directly. Uses forced tool use (`tool_choice: {"type": "tool", "name":
  ...}`) instead, which does work here. Not a hard schema-validation
  guarantee the way `strict: true` would be — Claude's JSON is trusted to
  match the tool's `input_schema` because it's forced to call that one tool,
  not server-enforced.
- **TLS**: this network intercepts HTTPS via a corporate proxy whose
  certificate isn't in Python's `certifi` bundle. Fixed by passing a scoped
  `httpx.Client(verify=truststore.SSLContext(...))` into the `AnthropicBedrock`
  client's `http_client=` param — **not** `truststore.inject_into_ssl()`,
  which patches the global `ssl` module process-wide and broke the
  Snowflake connector's own TLS handshake the moment `claude_tools.py` was
  imported anywhere in the same process (`OperationalError: maximum
  recursion depth exceeded` inside snowflake-connector — found and fixed
  during this work, not a pre-existing bug).
- **PII masking**: `_mask_column_profile()` strips `min_value`/`max_value`/
  `top_values` for any column with `is_pii=True` before it reaches Claude —
  see §1's note on why this doesn't do anything in practice yet (no PII
  agent exists to ever set `is_pii=True`).
- **Model choice**: Sonnet (`claude-sonnet-5`), not Opus — this call happens
  once per profiled table inside a request path (not a long-horizon
  agentic loop), so Sonnet's latency/cost profile fits better for this
  well-specified structured-extraction task.
- **Hybrid dedup**: `agents/rule_recommendation_agent.py`'s
  `_is_duplicate_of_template()` drops any Claude-sourced candidate that
  matches an existing template rule on `(column_name, rule_type)` — a
  code-level backstop on top of the system prompt's own "don't repeat a
  template rule" instruction, per mvp-scope.md's idempotent-scan invariant.
- **Scoring**: Claude's own `confidence`/`severity` are trusted, but
  `priority` is **not** — `skills/_shared.compute_priority()` (extracted out
  of `build_candidate()` for reuse here) recomputes it from
  confidence × severity-weight, same formula as every template rule, per
  mvp-scope.md's "numbers from code, words from the LLM" invariant.
- **Verified end-to-end against real Snowflake data**
  (`PLAYGROUND_DB.BRONZE.REPLAY_BRONZE_INGESTION_AUDIT_TBL`, both via
  `agents/scan_pipeline.py` and `graphs/dq_workflow_graph.py`): Claude found
  a genuine cross-column business-logic issue no template could
  (`RAW_ROW_COUNT ≠ SILVER_ROW_COUNT + REJECTED_ROW_COUNT` on a row marked
  `STATUS='RECONCILED'`), alongside several other business/domain rules,
  none of them restating a template rule.
- **Not built**: SQL Generation Agent has no LLM fallback for Claude-sourced
  rule types the template dispatcher doesn't recognize (e.g. `ACCURACY`,
  `CONSISTENCY`) — those rules correctly reach `sql_validation_agent.py`
  with `generated_sql=None` and are marked `INVALID` ("SQL is empty"),
  visible to a human as "recommended, not yet executable" rather than
  silently dropped or crashing the run. Building an LLM SQL-generation
  fallback (routed through the SQL Validator, same as template SQL) is
  future work — see §1 and architecture.md's "template-first, LLM only when
  template can't express it."

## 17. Rule Execution History Page (previously deferred display gap, now built)

- The ask: show *both* passed and failed rule runs, separately from
  alerts (alerts only exist for `FAILED` runs — `alert_agent.py` no-ops on
  `PASSED`/`ERROR`/`SKIPPED`, per §15 — so the Alerts Dashboard alone could
  never satisfy "user can see both passed and failed rule runs").
- **`storage_tools.list_execution_history(status=None, rule_id=None)`** —
  new. Reads `RULES.RULE_EXECUTION_HISTORY` directly (not `ALERTS`), LEFT
  JOIN `APPROVED_RULES` for `rule_name`/`database_name`/`schema_name`/
  `table_name`/`column_name` (same join pattern as `list_alerts()` —
  `RULE_EXECUTION_HISTORY` itself only stores `RULE_ID`). Duration is
  computed in SQL via `DATEDIFF('second', STARTED_AT, ENDED_AT)`, not
  subtracted client-side. Optional `status`/`rule_id` filters, AND-ed.
- **New route**: `GET /api/rules/execution-history` (optional
  `?status=`/`?rule_id=`).
- **New frontend page**: `apps/web/src/ExecutionHistoryPage.jsx` — table of
  rule name / table / status / failed count / failure % / duration / last
  run time, a status filter dropdown, reusing the existing
  `test-status-badge` CSS classes (`passed`/`failed`/`error`/`pending`);
  added a `.test-status-skipped` variant (App.css) since execution history
  surfaces `SKIPPED` runs too, a status the pre-existing badge set never
  needed. Wired into `App.jsx`'s nav as a 5th tab ("Execution History").
- **Explicitly did not touch**: `agents/alert_agent.py`'s "only alert on
  `FAILED`" rule — a passing run correctly still produces no alert; this
  page is a second, independent read of `RULE_EXECUTION_HISTORY`, not a
  change to what creates an `ALERTS` row. No new execution-producing logic
  either — this only reads history rows `rule_execution_agent.py` (§15)
  already writes on every "Run now" click.
- **Verified**: `GET /api/rules/execution-history` against real Snowflake
  data returned real `PASSED` rows (from earlier manual "Run now" testing)
  with correct `database_name`/`schema_name`/`table_name`/`duration_seconds`
  fields; `?status=FAILED`/`?status=SKIPPED` correctly returned `[]` (no
  runs of those statuses exist right now, not a query bug). Frontend
  verified via Vite serving the compiled component with no syntax errors
  and the nav/page wiring resolving correctly; a full interactive
  Playwright click-through (the level of verification §14/§15 used) was
  not completed this round — skipped by explicit user direction after the
  backend-data + compile check above, not silently dropped.

## 18. Table Health Page (per-table DQ score, new dashboard view)

- The ask: a table-level rollup — total active rules, passed, failed, open
  alerts, a simple DQ score (`passed / total * 100`), last scan time.
- **`storage_tools.list_table_health()`** — new. One row per table with
  ≥1 approved rule (a table with none isn't monitored yet, so it has no
  health to show — same exclusion logic this codebase applies elsewhere).
  Reuses the "latest execution per rule" `QUALIFY ROW_NUMBER()` pattern
  from `list_approved_rules()` to classify each active rule as
  passed/failed by its most recent run; a never-run rule (or one whose
  latest run was `ERROR`/`SKIPPED`) counts toward `total_active_rules` but
  not `passed_rules` or `failed_rules` — it hasn't told us pass/fail yet.
  `open_alerts` joins through `APPROVED_RULES` (same pattern as
  `get_alerts_summary()`, since `ALERTS` only stores `RULE_ID`).
  `last_scan_at` is `MAX(PROFILED_AT)` from `TABLE_PROFILES` — a *scan*
  (profiling), not a rule *execution*; those are different actions in
  this codebase. `dq_score` is computed in Python as
  `round(passed / total * 100, 1)`, `None` if `total_active_rules == 0`
  (guards the division; shouldn't occur given the base query already
  requires ≥1 active rule).
- **New route**: `GET /api/tables/health`.
- **New frontend page**: `apps/web/src/TableHealthPage.jsx` — one row per
  table (active rules / passed / failed / open alerts / DQ score / last
  scan time). DQ score rendered as a labeled bar (dataviz-skill guidance:
  reuse the existing three-tier status palette rather than invent new
  chart colors) — green ≥90%, amber 70–89%, red <70% (new `.dq-score-*`
  CSS classes in `App.css`, same green/amber/red hexes as
  `severity-badge`/`alert-status-badge`, not new colors). Wired into
  `App.jsx`'s nav as a 6th tab ("Table Health").
- **Not built**: no drill-down from a table row to its individual rules
  (the ask only specified the rollup, not a click-through — Active Rules
  Page and Recommended Rules Page already exist for per-rule detail).
  No trend-over-time / DQ-score history (would need periodic snapshots of
  this same query, which needs a scheduler — see §10 — to mean anything
  beyond "score right now"). No account-wide/cross-table aggregate tile.
- **Verified**: `GET /api/tables/health` against real Snowflake data
  returned one real row (`REPLAY_BRONZE_INGESTION_AUDIT_TBL`: 1 active
  rule, 1 passed, 0 failed, 0 open alerts, `dq_score: 100.0`, real
  `last_scan_at`) — correct given that table's one approved rule's only
  recorded run so far was `PASSED`. Frontend verified via Vite serving
  the compiled component with no syntax errors and nav/page wiring
  resolving correctly; same as §17, a full interactive Playwright
  click-through was not run this round.

## 19. Rule/Alert Business Explanations (Claude explains, code executes -- golden rule)

- The ask: use Claude for rule explanation, alert explanation, business
  impact, false-positive risk -- but keep SQL execution deterministic.
  Golden rule stated explicitly: "Claude explains and recommends. Code
  validates and executes. Human approves." This is purely additive text on
  top of an already-fully-decided pipeline; nothing about SQL generation,
  validation, execution, thresholds, or severity changed.
- **Schema**: new migration `07_add_explanation_columns.sql` adds
  `BUSINESS_EXPLANATION` / `BUSINESS_IMPACT` / `FALSE_POSITIVE_RISK`
  (all nullable `STRING`) to both `RULES.RECOMMENDED_RULES` and
  `ALERTS.ALERTS`. Applied via `run_migrations.py` (idempotent
  `ADD COLUMN IF NOT EXISTS`, no data loss on existing rows).
- **`tools/claude_tools.py`** — two new forced-tool-use calls,
  `explain_rule_with_claude(rule)` and
  `explain_alert_with_claude(rule, execution_result)`, same Bedrock/
  `AnthropicBedrock`/inference-profile/TLS pattern as
  `recommend_rules_with_claude()` (see that function's docstring — nothing
  new needed here, same account quirks apply). The tool schema
  (`_EXPLANATION_ITEM_SCHEMA`) only has three string fields — no SQL,
  threshold, or severity field exists for Claude to fill in, so it is
  structurally impossible for this call to smuggle a rule change back
  through the response, not just discouraged by prompt wording.
- **New agents**: `agents/rule_explanation_agent.py` /
  `agents/alert_explanation_agent.py` — thin wrappers that call the above
  and catch any Claude/Bedrock failure, falling back to a deterministic
  templated sentence (never leaves the field blank, never fails the scan/
  alert over an explanation call) — same "LLM failing must not fail the
  pipeline" convention as `rule_recommendation_agent.py`.
- **Wiring**: `main.py`'s `recommend-rules` route calls
  `run_rule_explanation_agent()` per rule right before
  `store_recommended_rule()` (which now accepts and persists the three
  fields). `agents/alert_agent.py` calls `run_alert_explanation_agent()`
  right before `store_alert()` (same three fields, same persist-through
  pattern) — only on the `FAILED` branch, since that's the only branch
  that creates an alert at all.
- **Frontend**: `App.jsx`'s `RuleDetailRow` (Recommended Rules Page) and a
  new `AlertDetailRow` (`AlertsPage.jsx`, behind a new "View details"
  button per alert row) both render the three fields, visually set apart
  from deterministic fields with a `.explanation-block` left-accent-bar
  style (`App.css`) — a reviewer can tell at a glance which text is
  Claude's prose vs. system-computed fields (description/reason/evidence).
- **Verified against real Snowflake + real Bedrock**: a full
  `recommend-rules` scan on `REPLAY_BRONZE_INGESTION_AUDIT_TBL` produced
  14 rules, every one with a distinct, genuinely business-friendly
  explanation/impact/false-positive-risk triple (both template-sourced
  rules like `COMPLETENESS`/`UNIQUENESS` and Claude-sourced
  `ACCURACY`/`CONSISTENCY`/`DISTRIBUTION` rules) — confirmed persisted in
  `RECOMMENDED_RULES` via a direct query, not just in the HTTP response.
  `alert_explanation_agent.run_alert_explanation_agent()` was also
  exercised directly against real Bedrock with a realistic synthetic
  FAILED result (no real alert existed at verification time to trigger
  naturally) and produced correct, distinct text. Frontend verified via
  Vite compiling both changed files cleanly with the new UI wired in.
- **Not built**: no explanation for `PASSED`/`ERROR`/`SKIPPED` executions
  (by design — only `FAILED` creates an alert at all, per §15's existing
  logic, unchanged here). No caching/reuse of a rule's explanation across
  re-scans (a re-scanned table gets fresh Claude calls, same as its
  template/Claude rule recommendations already do). No retry/backoff on
  the explanation call beyond the one fallback -- a transient Bedrock
  failure means fallback text, not a retried call.

## 20. Feedback Loop (rejections/false-positives/edits now shape future recommendations)

- The ask: on reject, store rule type/table/column/reason/timestamp. On the
  *next* recommendation for that table: don't re-suggest a rejected rule/
  column combo; lower priority on a false-positive history; use an edited
  threshold as a future signal. Definition of done: "rejected rules are
  not blindly suggested again" — verified directly (see below).
- **Activates `RULES.USER_FEEDBACK`**, previously created but completely
  unused (see §8/§9). Two new migrations:
  `08_add_feedback_columns.sql` adds `RULE_TYPE`/`DATABASE_NAME`/
  `SCHEMA_NAME`/`TABLE_NAME`/`COLUMN_NAME` (the lookup key);
  `09_add_feedback_threshold_column.sql` adds `THRESHOLD_CONFIG` (VARIANT,
  same convention as every other structured-config column in this schema)
  to carry the *value* of an edit, not just that one happened.
- **`storage_tools.store_user_feedback()`** — new. One row per REJECT/
  EDIT/FALSE_POSITIVE, denormalized with the full lookup key so a *future*
  scan (which mints a brand-new `rule_id`) can find this feedback without
  needing the original (possibly long-gone from the candidate set) rule.
  **`get_feedback_for_table()`** — new. All feedback for one table, newest
  first, read once per scan rather than once per candidate rule.
- **Wired into three existing routes** (`main.py`), no new routes needed:
  `POST /api/rules/{id}/reject` now also writes a REJECT feedback row;
  `PATCH /api/rules/{id}/edit` writes an EDIT feedback row (with the new
  `threshold_config`) whenever the edit touches `threshold_config`;
  `POST /api/alerts/{id}/false-positive` writes a FALSE_POSITIVE feedback
  row using the alert's underlying rule's type/location (via the existing
  `ALERTS -> APPROVED_RULES` join, extended to also select `RULE_TYPE` --
  no new stored column on `ALERTS` itself, since `APPROVED_RULES.RULE_TYPE`
  already existed).
- **`agents/rule_recommendation_agent.py`** — new `_apply_feedback()` step,
  run after Claude augmentation + dedup + scoring, before returning.
  Matches every candidate (template- and Claude-sourced alike) on
  `(rule_type, column_name)` against the table's feedback, same
  coarse-but-deliberate key `_is_duplicate_of_template()` already uses:
    - REJECT -> candidate dropped entirely.
    - FALSE_POSITIVE -> priority halved (`_FALSE_POSITIVE_PRIORITY_MULTIPLIER
      = 0.5`) — soft, not a drop; the rule concept had merit (it was
      approved and ran) but this specific alert didn't hold up.
    - EDIT -> candidate's `threshold_config` overwritten with the most
      recent human-edited value (feedback is read newest-first, so
      first-seen-per-key is already latest), and `generated_sql`
      **re-rendered** through `rule_template_tools.render_sql_for_rule()`
      to match -- without this, a rule shown to a reviewer with an edited
      threshold would silently carry SQL generated against the *old*
      default, a real correctness bug caught during verification, not
      hypothetical (see below).
  A feedback-lookup failure (storage/network) is caught and logged,
  falling back to the un-adjusted candidate set -- same "must not fail the
  scan" convention as the Claude call it sits next to.
- **Verified against real Snowflake, full round-trip, three separate
  sessions (2026-07-05)**:
  1. Rejected `STATUS should not be null` (COMPLETENESS/STATUS) via the
     real `/reject` route on `REPLAY_BRONZE_INGESTION_AUDIT_TBL` →
     confirmed a real `USER_FEEDBACK` row (`REJECT | COMPLETENESS |
     STATUS`) → re-ran a full `recommend-rules` scan on the same table →
     confirmed the rule was **not** in the new 13-rule result (down from
     14 the previous scan, exactly the one dropped rule, nothing else
     changed).
  2. Edited `STATUS should be one of the observed values`'s
     `threshold_config` from `{"accepted_values": ["RECONCILED"]}` to add
     `"PENDING"`/`"FAILED"` via the real `/edit` route → confirmed a real
     `USER_FEEDBACK` row with the new value → re-ran the scan again →
     confirmed the fresh candidate's `threshold_config` was seeded with
     the edited list **and** its `generated_sql` correctly read
     `STATUS NOT IN ('RECONCILED', 'PENDING', 'FAILED')`, not the stale
     single-value SQL a naive threshold-only override would have left in
     place.
  3. **FALSE_POSITIVE, live, full round-trip** (follow-up test session,
     closing the one gap the first pass only covered synthetically):
     edited a real recommended rule's SQL to guarantee a failure
     (`SELECT 1 AS FAILED_COUNT, COUNT(*) AS TOTAL_COUNT FROM ...`),
     approved it (`APPROVED_RULES.RULE_ID = '4e2566fb-5588-498b-
     80a9-afa8141bd045'`), ran it via the real `/run` route → produced a
     real `FAILED` execution (`RULE_EXECUTION_HISTORY.EXECUTION_ID =
     '8a9264dc-3d5b-45c1-8e61-25ee93a62808'`) and a real alert
     (`ALERTS.ALERT_ID = '14fb5322-d44b-4b6d-b791-acab2a1d5232'`, with a
     real Claude-generated business_explanation/business_impact/
     false_positive_risk triple — a bonus live re-check of §19 too) →
     marked it false-positive via the real `/false-positive` route →
     confirmed a real `USER_FEEDBACK` row (`FALSE_POSITIVE | COMPLETENESS
     | BATCH_ID`) → re-ran the scan again → confirmed the fresh
     `BATCH_ID should not be null` candidate's `priority` dropped from
     `0.95` to exactly `0.475` (the 0.5x multiplier, live, not synthetic).
     `_apply_feedback()`'s REJECT-drop branch was also unit-verified
     directly against synthetic feedback in the same follow-up session
     (a REJECT-matched candidate correctly dropped, a non-matched
     candidate passed through untouched) — the REJECT *filter* itself was
     already proven live in step 1 above, this just re-confirmed the
     isolated function logic.
  **Test rows left in place on purpose** (not cleaned up — flagged here so
  a future session can delete them if the demo data should stay pristine,
  rather than silently forgotten): the rejected/edited rule_ids above, plus
  the synthetic always-fail `APPROVED_RULES` row `4e2566fb-...` (set
  `IS_ACTIVE = FALSE` on it to stop it showing as a live monitored rule
  without deleting history), its execution row, and its alert. All three
  `USER_FEEDBACK` rows from this testing are real, intentional feedback
  signals, not noise -- they now genuinely suppress/deprioritize those
  exact rule-type/column combos on this table, which is correct behavior,
  not a side effect to undo.
- **Not built**: no UI surface for browsing feedback history directly
  (a reviewer sees its *effect* -- the rule is gone, or its threshold is
  pre-filled -- not a "why was this filtered" explanation on the
  Recommended Rules Page). No decay/expiry on old feedback (a rejection
  from months ago blocks a rule just as hard as one from yesterday --
  reasonable for MVP1, worth revisiting once retrieval-based few-shot
  learning (§9, MVP2) exists and needs to weight recency anyway). No
  `APPROVE` feedback_type written (the ask only asked for the three
  negative/corrective signals; `04_create_rule_tables.sql`'s comment on
  `FEEDBACK_TYPE` lists `APPROVE` too, but nothing in this ask needed to
  look that up, so it stays unwritten rather than added speculatively).

## 21. `GET /api/rules/recommended` (unfiltered) breaks at real data volume — corporate-proxy TLS, same root cause as the Bedrock issue

- **Discovered during Feedback Loop verification** (2026-07-05), not
  something introduced by that work — a pre-existing environment
  limitation that only surfaces once `RECOMMENDED_RULES` accumulates
  enough rows for one table (accumulated to ~59 rows total across many
  test scans by this point in the project).
- **What happens**: `GET /api/rules/recommended` with no `?scan_id=`
  filter (i.e. "list every recommended rule ever, across every scan")
  takes 60–80+ seconds and then fails with a 500:
  `HTTPSConnectionPool(host='sfc-va3-ds1-89-customer-stage.s3.amazonaws.com'
  ...) SSLError(... certificate verify failed)`.
- **Root cause**: once a query's result set is large enough, Snowflake's
  Python connector fetches results from a signed S3 URL instead of
  returning them inline over the main session connection. That S3 fetch
  goes through a *different* TLS handshake than the one
  `tools/snowflake_connection.py` already patches for the main
  connection — and this network's corporate-proxy TLS interception
  (already documented for the Bedrock `AnthropicBedrock` client's
  `http_client=` fix, and for why `truststore.inject_into_ssl()` was
  rejected globally) breaks that separate S3 handshake too. Same
  underlying network condition as those two issues, third distinct place
  it's now bitten this project.
- **What's unaffected**: the app's actual usage pattern —
  `GET /api/rules/recommended?scan_id=...`, which is all `App.jsx`'s
  `RecommendedRulesPage` ever calls (it's always handed a real `scanId`
  from the Database Explorer's "Recommend rules" flow) — stays fast
  (~1.5–2s, verified directly) regardless of total table size, since a
  single scan's result set is small. The unfiltered route is a
  dev/debugging convenience (`GET /api/rules/recommended` with no query
  param), not something a real user flow exercises today.
- **Not fixed**: no code change made. Fixing this for real needs either
  (a) a Snowflake connector/session setting that keeps small-enough
  results inline longer (raising the inline-vs-S3 threshold), or (b) the
  same `truststore`-scoped-`http_client` pattern `claude_tools.py` uses
  for Bedrock, applied to whatever HTTP client the Snowflake connector
  uses internally for its S3 fetches — connector internals, not
  something `snowflake_connection.py`'s own `http_client=` param
  controls today. Revisit if the unfiltered route ever becomes a real
  product need (e.g. an admin "all recommendations across all scans"
  view) rather than staying a debug-only query.

## 22. Manual Scan Options (scope picker: table / schema / selected tables / full database)

- The ask (per `README.md`'s original requirement for a user-chosen scan
  scope): let the user choose scope before scanning, starting with "one
  table" (already existed, just implicit from Database Explorer navigation)
  and expanding to "schema," explicitly **not** starting with a full-account
  scan.
- **Built**: `apps/web/src/App.jsx`'s new `ScanPanel` component — a 4-option
  scope picker ("This table" / "This schema" / "Selected tables" / "Full
  database") shown once a schema is selected. Only the first two are
  functional; "Selected tables" and "Full database" are rendered
  **disabled** with a "Soon" badge (reusing the existing muted-badge color
  formula from `test-status-pending`/`active-no`) rather than hidden —
  communicates the eventual scope model without pretending it works today.
  Replaces the old table-gated "Recommend rules" button that lived inside
  `ColumnsPanel`.
- **Backend**: `main.py`'s single-table `recommend_rules()` route was
  refactored with no behavior change — its body moved into
  `_recommend_rules_for_table()`, which raises instead of catching, so a new
  `POST /api/databases/{db}/schemas/{schema}/recommend-rules` route
  (schema scope) can call it per table and decide per-table how to handle a
  failure, instead of the single-table route's "one exception -> whole
  request 500."
- **Schema scan execution model, explicit choice**: sequential loop over
  `_recommend_rules_for_table()` inside one synchronous HTTP request — same
  pattern already used everywhere else in this codebase (no background job
  queue / task table / polling route exists anywhere yet). One table's
  failure is recorded per-table (`{table_name, scan_id: null, error}`) and
  the loop continues — same "don't abort on one failure" convention as
  `DQWorkflowState`'s per-node error capture and `alert_agent.py`'s
  per-rule handling, just applied at the per-table level.
- **No parent/child scan grouping column exists** in `SCAN_RUNS` (checked
  directly — `TARGET_TABLE`/`TARGET_SCHEMA` are nullable, but there's no
  `PARENT_SCAN_ID` or similar). Rather than a schema migration, the schema
  route creates one umbrella `SCAN_RUN` row (`TARGET_TABLE=NULL`) purely to
  represent "a schema-scoped scan was kicked off," while each table's real
  work still gets its own `scan_id` via the unchanged
  `_recommend_rules_for_table()` → `create_scan_run()` path. The frontend
  aggregates by fetching `GET /api/rules/recommended?scan_id=...` once per
  table's `scan_id` and concatenating — confirmed via code read that
  `agents/rule_recommendation_agent.py`'s feedback lookup
  (`_apply_feedback()` / `get_feedback_for_table()`) is keyed purely on
  table/column/rule_type, not `scan_id`, so per-table scan_ids don't break
  the feedback loop (§20).
- **Verified end-to-end against real Snowflake and real Bedrock**: ran the
  new schema-scan route (`POST /api/databases/PLAYGROUND_DB/schemas/RAW/
  recommend-rules`) against `PLAYGROUND_DB.RAW`, which has exactly one small
  (0-row) table — chosen deliberately over `BRONZE` (which has a real
  31.5M-row table, unsafe to full-scan without sampling, §4). Returned
  `HTTP 200` in ~130s with umbrella `scan_id=1945c95f-742e-4fa1-a87d-
  fb314fdf403d` and one table result (`REPLAY_RAW_RECORDS_TBL`,
  `scan_id=86ef02d2-3b3b-4269-89b5-76a6d5be3463`, `rule_count=10`,
  `error=null`). Confirmed the frontend's exact aggregation call
  (`GET /api/rules/recommended?scan_id=86ef02d2-...`) returns all 10 real
  rules — a mix of template-sourced (UNIQUENESS/VOLUME/FRESHNESS/
  COMPLETENESS) and real Claude/Bedrock-sourced (CONSISTENCY/VALIDITY/
  ACCURACY) rule types, each with a real `validation_status`/`test_status`.
  Also confirmed the single-table route (`recommend_rules()`) still returns
  identical behavior post-refactor (its logic is now shared via
  `_recommend_rules_for_table()`, unchanged). Test data left in place
  (matching this project's §20 convention) rather than deleted.
- **Not built, deliberately**:
  - "Selected tables" scope (multi-select checkboxes across the Tables
    column) — UI and backend route both unbuilt; disabled placeholder only.
  - "Full database" scope — explicitly **not** started first per the ask;
    also unbuilt, disabled placeholder only. Real account-wide scanning
    remains MVP3 scope per `mvp-scope.md`/`README.md`'s risk callout.
  - True background/async execution for a schema scan with progress
    polling — a schema with many/large tables will hold one HTTP request
    open for a long time; this needs a real job queue or `SCAN_RUNS`
    polling route, neither of which exists yet. Flagged here rather than
    half-built. Revisit alongside §10's scheduler work, which will need
    similar async infra anyway.
  - No new tests were added — this codebase currently verifies by exercising
    real routes against Snowflake/Bedrock (see `context.md`'s "verify
    against real Snowflake and real Bedrock" convention), not a unit-test
    suite.

## 23. Background Job Progress (live agent-activity feed)

- The ask: show the user what the agent is doing during a scan (metadata
  discovery started/completed, profiling started/completed, rules
  recommended, SQL generated, SQL validated, testing completed, awaiting
  approval), backed by `LOGS.AGENT_RUN_LOGS` — a table that already existed
  (per README's "UI can see logs overview of what agent is performing"
  requirement) with a writer function (`storage_tools.log_agent_run()`) that
  had never actually been called from anywhere (§13 flagged this gap
  explicitly: "backing table exists and is written to" was aspirational,
  not yet true).
- **Built**: `graphs/dq_workflow_graph.py`'s six nodes each now call a new
  `_log()` helper (best-effort, catches its own failure so a logging error
  can never break the scan — same "must not fail the pipeline" convention
  as the Claude/Bedrock calls) at STARTED and COMPLETED/FAILED for their
  step, writing through the pre-existing `log_agent_run()`. `main.py`'s
  `_recommend_rules_for_table()` adds one final `AWAITING_APPROVAL` log
  entry after storage completes — the one milestone from the ask's list
  that isn't a graph node.
- **New read path**: `storage_tools.list_agent_run_logs(scan_id)` (oldest
  first — a progress feed reads as a timeline, not a most-recent-first
  table, unlike every other `list_*()` in this module) backing new
  `GET /api/scans/{scan_id}/logs`.
- **Live-polling gap and how it's bridged**: scans run as one long
  synchronous POST (no background job queue exists — see §22's same
  observation), so the frontend has no `scan_id` to poll until that POST
  already resolves, by which point the scan is over and "live" progress
  would be pointless. Solved with a second new route,
  `GET /api/databases/{db}/schemas/{schema}/latest-scan[?table_name=]`
  (backed by new `storage_tools.get_latest_scan_id()`), which looks up the
  in-flight scan by its target instead of needing the id handed to it.
  Confirmed directly (concurrent curl requests against a real running scan)
  that Snowflake's connector and FastAPI's threadpool tolerate a concurrent
  read against the same cached app-DB connection a write-heavy scan is
  using — no connection contention, no need to build separate connection
  pooling for this.
- **Schema-scope polling deliberately coarser than table-scope**: a
  schema scan's umbrella `SCAN_RUN` (`TARGET_TABLE IS NULL`) never gets
  per-step logs itself (only each table's own scan does, via the same graph
  nodes). `get_latest_scan_id(table_name=None)` therefore does *not* filter
  to `TARGET_TABLE IS NULL` — it returns the most recent scan for the
  schema regardless of table, which naturally tracks whichever table the
  sequential per-table loop (§22) is currently processing. A schema scan's
  progress panel shows one table's full step sequence at a time, restarting
  at "Metadata discovery started" for the next table — not a running total
  across all tables, which would need the parent/child scan grouping this
  codebase doesn't have (§22 already flagged this gap for a different
  reason).
- **Frontend**: `apps/web/src/App.jsx` gained a `ScanProgressPanel`
  component (a checklist with ✓/…/✕ markers per `PROGRESS_STEP_LABELS`,
  mapping `STEP_NAME`+`STATUS` into the ask's exact wording) and polling
  state/logic (`startProgressPolling`/`stopProgressPolling`, a
  `setInterval` at 2s, cleared on unmount and on scan completion) in
  `DatabaseExplorer`, wired into both `runRecommendRules()` (table scope)
  and `runSchemaScan()` (schema scope).
- **Not built**: no progress display for `profile_table()` (the older,
  separate profile-only route) — this ask's step list matches the
  recommend-rules workflow specifically. No historical/past-scan log
  browsing UI (the panel only ever shows the currently-running scan; past
  logs are readable via the same `GET /api/scans/{scan_id}/logs` route but
  nothing in the UI links to it after a scan completes). No log detail
  beyond message text (`DETAILS` VARIANT column exists on
  `AGENT_RUN_LOGS` and is stored, but no logger call in this feature
  populates it — every `_log()` call passes `message` only).
- **Verified end-to-end against real Snowflake, real Bedrock, and a real
  headless-Chromium browser session** (Playwright against system Chrome,
  same approach as §14/§15 — Playwright's own browser download is blocked
  by the corporate-proxy TLS interception documented elsewhere in this
  doc): a real table-scope scan on `PLAYGROUND_DB.RAW.REPLAY_RAW_RECORDS_TBL`
  showed the progress panel updating incrementally roughly every 2s over
  the scan's ~144s real runtime, with every expected step label appearing
  in order (`Metadata discovery started` → `Metadata completed` →
  `Profiling started` → `Profiling completed` → `Recommending rules...` →
  `Rules recommended` → `Generating SQL...` → `SQL generated` →
  `Validating SQL...` → `SQL validated` → `Testing rules...` →
  `Testing completed`), then correctly navigating to the Recommended Rules
  page. Zero browser console errors. Also confirmed directly via `curl`
  against the real backend: a schema scan on `PLAYGROUND_DB.RAW` produced
  13 real, correctly-ordered, timestamped `AGENT_RUN_LOGS` rows for its one
  table, ending with a real `Awaiting approval (9 rules)` entry.

## 24. Remaining scan scopes (Selected tables, Full database) and bulk Rule Execution

Closes the two scan scopes §22 left disabled ("Selected tables", "Full
database") and the "no run-all-approved bulk action" gap §13 flagged.

- **"Selected tables" scope**: `ScanPanel` now renders a checkbox list of
  every table in the selected schema when this scope is chosen; the user
  picks any subset. New backend route
  `POST /api/databases/{db}/schemas/{schema}/recommend-rules-selected`
  (body: `{table_names: [...]}`) loops the same
  `_recommend_rules_for_table()` used everywhere else in this file over
  just that list — no new per-table logic, only a new way to produce the
  list of tables to loop over (previously always `list_tables()`'s full
  result).
- **"Full database" scope**: one selected database, every schema, every
  table within it — **not** account-wide/cross-database (that stays
  explicitly out of scope, see below). New route
  `POST /api/databases/{db}/recommend-rules` loops over every non-
  `INFORMATION_SCHEMA` schema, and within each, every table, via the same
  per-table workflow. A schema-level failure (e.g. a schema this role can't
  read) is recorded like a failed table entry rather than aborting the rest
  of the database — same don't-abort-on-one-failure convention as every
  other loop in this codebase.
- **Row-count safety guard before running**: full-table profiling has no
  sampling yet (§4), so scanning every table in a database can mean a
  multi-hour run across large tables. New `GET /api/databases/{db}/scan-
  preview` (per-schema and total table/row counts, real `SHOW TABLES` row
  counts, no query execution) backs a `window.confirm()` in the frontend
  showing the real total before the scan starts — verified directly against
  `PLAYGROUND_DB` (27 tables, 10 schemas, ~110.4M rows, correctly flagging
  `BRONZE`/`SILVER`'s multi-million-row tables) and confirmed the dialog
  fires with those exact numbers in a live browser session.
- **Bulk Rule Execution ("Run Rules")**: new `POST /api/rules/run-all` --
  loops the *existing*, unmodified `run_rule_execution_agent()` (fetch ->
  revalidate -> execute -> store history -> alert-if-failed) over every row
  from `list_approved_rules()`. No new execution logic; the per-rule agent
  behaves identically whether called once (the existing "Run now" button)
  or in this loop. Inactive rules are included in the loop rather than
  pre-filtered out, since the agent's own `SKIPPED` status for an inactive
  rule is a meaningful, distinct signal worth keeping visible in the bulk
  response (attempted-and-skipped vs. never-listed) rather than silently
  dropped before the agent runs. New `ActiveRulesPage` "Run Rules" button
  shows a summary (`N passed / N failed / N error / N skipped`) after one
  click.
- **Design question resolved: does approving a rule re-run it?** No --
  explicitly decided not to, and this section records both what exists
  today and the ideal alternative considered:
  - **What exists today (three distinct "runs", not one)**: (1) the Rule
    Test Execution Agent's pre-approval preview (`test_status`/
    `test_result` shown on the Recommended Rules page, computed once at
    scan time, never written to `RULE_EXECUTION_HISTORY`, never alerts);
    (2) the per-rule manual "Run now" (post-approval only, real history +
    alert); (3) this section's new bulk "Run Rules" (same agent as (2),
    looped). Approving a rule is a pure status move — `store_approved_rule()`
    copies the recommended rule into `APPROVED_RULES`; it does not read or
    act on the pre-approval test result, and does not itself execute
    anything.
  - **Why not re-run on approval**: the pre-approval test result is a
    snapshot from scan time. By the time a human gets around to approving
    a rule (minutes, hours, or days later in a real workflow), the
    underlying data may have changed — reusing the stale snapshot as if it
    were a fresh execution-history row would misrepresent when the rule
    was actually last checked against real data. A fresh run closes that
    gap; skipping it trades correctness for one fewer click.
  - **The ideal version, for a future session**: architecture.md's
    conceptual flow (recommend → approve → execute → alert) doesn't
    strictly require a *separate* human click between approve and first
    execution — a more automated version could auto-trigger exactly one
    real run immediately on approval (still writing to
    `RULE_EXECUTION_HISTORY` for real, still eligible for an alert), so a
    newly-approved rule shows up in Active Rules with a real "last run"
    already populated instead of "Never run" until someone clicks Run.
    That's a small, well-scoped addition on top of what exists now (the
    `/approve` route would call `run_rule_execution_agent()` once after
    `store_approved_rule()` succeeds) — deliberately not built this round
    since it changes the approve route's behavior and wasn't asked for,
    but flagged here as the natural next step once the current three-run
    model earns its keep. This is also the natural point to eventually
    connect to §10's scheduler (recurring runs), rather than only
    ever running on approval or a manual click.
- **Real bug found and fixed during verification, not by code review**: the
  new `.scan-scope-option.active` (blue background, white text) was
  losing to `.scan-scope-option:hover` (blue text) on CSS specificity —
  hovering the currently-selected scope button rendered blue text on a
  blue background, invisible. Only surfaced by literally reading computed
  styles in a live headless-Chromium session (`getComputedStyle` on the
  active button showed identical `color`/`background-color`); a code
  read alone would not have caught it. Fixed with
  `:hover:not(:disabled):not(.active)`.
- **Verified end-to-end against real Snowflake and a real browser**:
  selected-tables scan on `PLAYGROUND_DB.RAW` (1 table selected) returned
  real `scan_id`s and 9 rules, same shape as the schema-scan route;
  full-database route smoke-tested against `SNOWFLAKE_LEARNING_DB` (0
  tables across all its schemas) to confirm the schema-enumeration +
  per-table-loop + result-aggregation logic without the cost of a real
  multi-table run (a real run against a large database was deliberately
  not exercised here -- that is exactly the scenario the row-count
  confirmation dialog exists to make a human decide about, not something
  to trigger during a routine verification pass); bulk run-all exercised
  against 2 real pre-existing approved rules, producing one real `FAILED`
  (with a real new alert, confirmed via `/api/alerts/summary`) and one real
  `PASSED` in a single call -- exactly the demo script's "some pass, some
  fail" step.

## 25. LangSmith tracing (agent/LLM observability)

- The ask: add LangSmith to the project. Purely additive -- traces what
  already runs, changes no agent's logic, output, or storage.
- **New `tools/langsmith_tools.py`**: `configure_langsmith_tracing()`
  (called once at `main.py` startup) sets a process-wide LangSmith client so
  every `graphs/dq_workflow_graph.py` `.invoke()` call traces automatically
  (LangGraph's own LangChain-core-based tracer picks up the global client
  with no other code change), and `get_traced_client_for_anthropic()` wraps
  the `AnthropicBedrock` client in `tools/claude_tools.py` with
  `langsmith.wrappers.wrap_anthropic()` for full input/output/token
  visibility on the Claude/Bedrock call specifically. Both no-op (return
  unchanged / skip entirely) if `LANGSMITH_API_KEY` isn't set -- tracing is
  opt-in, never a hard dependency for the app to run.
- **TLS gap, fourth instance of the same root cause**: this network's
  corporate-proxy TLS interception (already documented for Bedrock,
  §16, and Snowflake's S3 result-fetch, §21) also breaks LangSmith's
  `requests`-based client -- confirmed directly (`SSLCertVerificationError:
  self-signed certificate in certificate chain` calling the real LangSmith
  API). **Verified fix, non-obvious**: passing a truststore-backed
  `requests.Session` into `Client(session=...)` does **not** work --
  `Client.__init__` mounts its own `_LangSmithHttpAdapter` on `https://`
  *after* accepting the caller's session, silently overwriting it. The
  actual fix is to construct `Client()` first, then remount a
  truststore-backed `HTTPAdapter` on `client.session` *afterward* so it
  overwrites LangSmith's own adapter instead of the reverse -- confirmed by
  reading real traces back via `client.list_runs()`, not just "no exception
  raised" (an early attempt looked like it worked because `client.info`
  silently swallows connection failures and returns an empty object either
  way -- a strict call like `client._get_settings()` was needed to prove
  the first fix attempt was still actually failing). Same "scoped fix, never
  the global `truststore.inject_into_ssl()`" principle as §16 -- that global
  patch previously broke the Snowflake connector's own TLS handshake.
- **`.env`/`.env.example`**: `LANGSMITH_TRACING`, `LANGSMITH_API_KEY`,
  `LANGSMITH_PROJECT=agentic-dq-platform` added, same pattern as every other
  credential in this project. `langsmith==0.9.7` added to `requirements.txt`
  explicitly -- it was already present transitively (via `langgraph` ->
  `langchain-core`), but pinning it directly avoids depending on that
  transitive chain staying stable.
- **Not built**: no LangSmith instrumentation for anything outside the
  LangGraph workflow and the Claude call -- `agents/scan_pipeline.py` (the
  parallel non-graph chain), `agents/rule_execution_agent.py`, and
  `agents/alert_agent.py` are untraced (none of them call Claude or run
  through the graph). No sampling/rate-limiting configured (every call
  traces at 100%) -- fine at this project's real call volume, worth
  revisiting if that changes. No LangSmith-based evaluation/dataset work
  (out of scope per `mvp-scope.md`'s "evaluation framework" exclusion).
- **Verified end-to-end against the real LangSmith API and a real scan**:
  ran a full `recommend-rules` scan on `PLAYGROUND_DB.RAW.
  REPLAY_RAW_RECORDS_TBL` (real Snowflake + real Bedrock, `HTTP 200` in
  ~142s, 10 rules) with tracing active, then queried the real LangSmith
  project (`agentic-dq-platform`) via `client.list_runs()` and confirmed the
  exact expected trace tree: a root `LangGraph` run with child spans for
  all six nodes (`metadata_agent` -> `profiling_agent` ->
  `rule_recommendation_agent` -> `sql_generation_agent` ->
  `sql_validation_agent` -> `rule_test_execution_agent`), and a nested
  `ChatAnthropic` LLM-type run inside `rule_recommendation_agent` (the real
  Claude/Bedrock call), plus additional top-level `ChatAnthropic` runs from
  the rule/alert explanation calls (`agents/rule_explanation_agent.py` /
  `agents/alert_explanation_agent.py`, which reuse the same wrapped
  client). All runs showed `status: success`.

## 26. Sample-first profiling for large tables (closes §4)

- The ask: one of "the three biggest unlocks" per this doc's own framing
  -- full-table-scan profiling was unsafe on this account's real 31M+ row
  tables, and every prior session's testing explicitly avoided
  `BRONZE.REPLAY_BRONZE_INGESTION_RECORDS_TBL` for exactly that reason.
- **Built**: `tools/snowflake_profiling_tools.py`'s `profile_table()` now
  checks a table's row count *cheaply* first --
  `snowflake_metadata_tools.get_table_row_count_estimate()` (new), reusing
  `list_tables()`'s free `SHOW TABLES` metadata, no scan -- against a new
  `_SAMPLE_ROW_THRESHOLD = 100_000`. At or above it, every per-column query
  (`profile_column_nulls`/`profile_column_distincts`/
  `profile_numeric_min_max`/`profile_top_values`, each gained a new
  `sample_size` param) runs against `{fqn} SAMPLE (50_000 ROWS)` instead of
  the full table; `null_count`/`distinct_count` (count-shaped stats) are
  scaled back up to full-table estimates using the sample's *actual*
  returned row count, not the requested size (`SAMPLE (n ROWS)` returns
  `min(n, table_size)`, so a table just over the threshold could return
  fewer rows than asked). `min_value`/`max_value`/`top_values` are reported
  as-observed-in-the-sample, not corrected -- there's no way to "scale" an
  extremum or a frequency ranking. Every column dict gains `is_sampled:
  bool`; `profile_table()`'s table-level result also gains `sample_size`.
  Below the threshold, behavior is byte-for-byte unchanged from before this
  feature (real `COUNT(*)`, unsampled per-column queries) -- verified via
  regression test (see below), not just by code inspection.
- **New migration** `infra/snowflake/10_add_sampling_columns.sql`:
  `TABLE_PROFILES.IS_SAMPLED`/`SAMPLE_SIZE`. **Real bug hit and fixed
  during this migration, not by code review**: `ALTER TABLE ... ADD COLUMN
  IF NOT EXISTS <col> ... DEFAULT <val>` raises Snowflake error `002028:
  ambiguous column name` when re-run against a column that *already*
  exists (confirmed directly, reproduced in isolation) -- inline `DEFAULT`
  is the trigger; every other `ADD COLUMN IF NOT EXISTS` migration in this
  repo (`07`/`08`/`09_*.sql`) already avoided `DEFAULT` for what turns out
  to be this same reason, previously undocumented. Fixed by dropping the
  inline default; app code (`storage_tools.store_table_profile()`) always
  passes an explicit `is_sampled` value on INSERT, so no column-level
  default was needed for correctness anyway.
- **`storage_tools.store_table_profile()`** gained `is_sampled`/
  `sample_size` kwargs, threaded through from
  `snowflake_profiling_tools.store_profile_result()`.
- **Not built**: no user-facing "sampling depth" setting (architecture.md
  §10 calls for depth to be "a visible setting, not an agent guess") --
  the threshold/sample-size are fixed module constants
  (`_SAMPLE_ROW_THRESHOLD`/`_SAMPLE_SIZE`), not configurable per-scan or
  via the UI. No `cost_guard` utility (architecture.md §10's "estimates/
  limits query cost before expensive scans") -- the threshold check itself
  is the only cost guard that exists. Distinct-count scaling is a linear
  approximation (`distinct_count * (row_count / sample_rows_seen)`), not a
  statistically rigorous distinct-count estimator (e.g. HyperLogLog) --
  reasonable for a DQ-rule-thresholding use case, not presented as exact.
- **Verified end-to-end against the real 31.5M-row table**: a full
  `recommend-rules` scan on `PLAYGROUND_DB.BRONZE.
  REPLAY_BRONZE_INGESTION_RECORDS_TBL` completed in ~196s (previously
  avoided entirely in this project's testing) with every column persisted
  `IS_SAMPLED=True`/`SAMPLE_SIZE=50000` in `TABLE_PROFILES`, confirmed via
  direct query, not just the HTTP response. Regression-verified the
  small-table path is unchanged: a scan on `PLAYGROUND_DB.RAW.
  REPLAY_RAW_RECORDS_TBL` (well under the threshold) persisted
  `IS_SAMPLED=False` on every column, same as every prior session's runs.

## 27. PII / Sensitivity Classification Agent (closes §3)

- The ask: the second of "the three biggest unlocks" -- the masking floor
  in `tools/claude_tools.py`'s `_mask_column_profile()` had existed since
  the Claude integration was first built, correctly checking `is_pii` and
  stripping `min_value`/`max_value`/`top_values` when set, but had been
  **permanently a no-op** the entire time: no agent ever set `is_pii=True`
  on any column, ever, in this codebase's history until now.
- **Two tiers, per architecture.md §7's exact spec** ("Column -> PII
  detector (regex/heuristics) + LLM assist for ambiguous cases"):
  1. **New `tools/pii_detection_tools.py`** -- pure regex/heuristics, no
     LLM call. `classify_column_deterministic(column_name, top_values)`
     checks column-name patterns first (EMAIL/PHONE/PAN/AADHAAR/
     FINANCIAL_ID/ADDRESS/NAME, matching `04_create_rule_tables.sql`'s
     `PII_TYPE` DDL comment exactly), then a value-shape regex check
     against `top_values` (data profiling already collected, no new
     query) for columns whose name alone doesn't give it away. A separate
     "obviously safe" pattern (`*_ID`/`*_AT`/`*_COUNT`/etc.) short-circuits
     system/audit columns to non-PII directly. Returns `None` (ambiguous)
     when neither tier matches -- the signal to escalate to Claude.
     `SENSITIVITY_TO_POLICY` maps `LOW/MEDIUM/HIGH -> ALLOW_RAW_SAMPLE/
     ALLOW_MASKED_SAMPLE/ALLOW_STATS_ONLY`, matching architecture.md §7's
     table exactly.
  2. **New `claude_tools.classify_columns_with_claude(table_fqn,
     ambiguous_columns)`** -- one batched Claude/Bedrock call per table
     (not per column) for whatever the deterministic pass left ambiguous,
     same forced-tool-use structured-output pattern as
     `recommend_rules_with_claude()` (reuses `_get_client()`, no new
     Bedrock/TLS wiring needed). Only sample values (never full column
     data) are sent. Instructed to prefer the stricter classification when
     unsure -- a false positive here costs a stats-only view; a false
     negative could leak real PII to a future LLM call.
- **New `agents/pii_agent.py`** -- `run_pii_agent()`, same pure-compute
  convention as `profiling_agent.py`: takes a `column_profiles` list,
  returns it enriched with `is_pii`/`pii_type`/`sensitivity_level`/
  `llm_sharing_policy` per column. If the Claude call itself fails
  (network/auth/throttling), every still-ambiguous column falls back to
  the *safest* classification (`HIGH`/`ALLOW_STATS_ONLY`), never to "not
  PII" -- an LLM failure must not fail the scan (same convention as
  `rule_recommendation_agent.py`'s Claude call), but it also must never
  silently downgrade an unclassified column to "safe to share raw" just
  because the classifier broke.
- **Wired into `graphs/dq_workflow_graph.py`** as a new `pii_agent` node,
  inserted between `profiling_agent` and `rule_recommendation_agent` --
  matching architecture.md §4a's pipeline order exactly ("Profiling ->
  PII/Sensitivity -> Rule Recommendation"). This is the step that finally
  gives `rule_recommendation_agent`'s Claude call real classifications to
  mask against.
- **`snowflake_profiling_tools.store_profile_result()`** now reads
  `is_pii`/`pii_type`/`sensitivity_level`/`llm_sharing_policy` off each
  column dict via `.get()` (defaulting to unclassified if absent, e.g. for
  the standalone `/profile` route which has no PII step) and passes them
  into `storage_tools.store_column_profile()` -- which already accepted
  these four kwargs and had for a long time, just never received real
  values.
- **Not built**: no caching/reuse of a table's PII classification across
  re-scans (a re-scanned table gets a fresh deterministic pass + fresh
  Claude call for ambiguous columns every time, same as rule
  recommendations already do). No UI surface showing PII classifications
  (a reviewer would need to query `COLUMN_PROFILES` directly to see them
  today). Sensitivity thresholds/policy mapping are fixed, not
  user-configurable.
- **Verified end-to-end against real Snowflake and real Bedrock**: the
  same 31.5M-row-table scan that verified sampling (§26) also exercised
  PII classification for real -- `BATCH_ID`/`LINE_NUMBER` matched the
  deterministic "safe" pattern directly; `FILE_NAME`/`FILE_PATH`/
  `RECORD_TYPE`/`RAW_LINE` were genuinely ambiguous (no name/value match)
  and were correctly classified non-PII by a real Claude LLM-assist call
  (reasonable: file-ingestion metadata, not personal data). On a separate
  small-table scan, `FILE_CONTENT` was classified `is_pii=True` by the
  same LLM-assist tier -- a real, contextual judgment call (a "file
  content" column could plausibly hold arbitrary embedded personal data),
  confirming the LLM tier reasons per-column rather than rubber-stamping.
  Directly confirmed via `_mask_column_profile()` that a synthetic
  `is_pii=True` column's `min_value`/`max_value`/`top_values` are now
  genuinely stripped before reaching Claude, with `null_percentage`
  correctly passing through unmasked (`ALLOW_STATS_ONLY` tier) -- this
  masking path was provably a no-op before this session (see above); it
  is real now, confirmed via direct query of `COLUMN_PROFILES` for both
  test scans (`IS_PII`/`PII_TYPE`/`SENSITIVITY_LEVEL`/`LLM_SHARING_POLICY`
  all genuinely populated, not left at defaults).

## 28. Scheduler (closes §10, activates architecture.md §8's auto-resolve)

- The ask: the third of "the three biggest unlocks" -- every scan/
  execution in this codebase was manual-click-only; nothing had ever run
  on a timer; `storage_tools.update_alert_status()`'s own docstring had
  documented "auto-resolve when a rule passes on a later scan" as an
  intended caller since before this feature existed, but no real
  recurring trigger had ever existed to be that caller.
- **Auth/connection reality, decided explicitly with the user**: this
  scheduler runs on the **same cached interactive-SSO source connection**
  every other route in this app uses
  (`tools/snowflake_connection.py`'s `_source_conn`) -- **not** true
  unattended/headless operation. The user does have access to a key-pair
  auth key but needs to go through their organization to get it, so real
  key-pair auth (`ALTER USER ... SET RSA_PUBLIC_KEY=...` on the Snowflake
  side, then swapping `_connect_source()`'s `authenticator=` for
  `private_key=<der bytes>` -- exact swap point already noted in that
  function's own docstring) is **deliberately not built this round**.
  Consequence, flagged explicitly: a scheduled job firing before any human
  has completed one interactive browser login in this backend process
  will hit the same SSO popup a manual click would, with no human at the
  keyboard to click through it -- this scheduler automates *when* a job
  runs, not *who* authenticates it. This is the natural next step once
  the key is available; nothing about `scheduler.py`'s own logic is
  auth-method-specific, only `snowflake_connection.py` would need to
  change.
- **New `apps/backend/agent_service/scheduler.py`** -- an
  `apscheduler.BackgroundScheduler` (thread-based, not asyncio; this
  codebase has no other asyncio-heavy code to integrate with), started/
  stopped via `main.py`'s new `lifespan` context manager (FastAPI's
  `@asynccontextmanager` pattern replaces the old bare `app = FastAPI(...)`
  construction). One poll job (`_run_due_schedules`, default every 1
  minute, `SCHEDULER_POLL_INTERVAL_MINUTES` env var) checks every active
  `CORE.SCAN_SCHEDULES` row for whether `INTERVAL_MINUTES` have elapsed
  since `LAST_RUN_AT` (or it's never run at all, which is always due), and
  fires it. Two schedule types: `RULE_EXECUTION` (re-runs every approved
  rule) and `RESCAN` (re-runs recommend-rules over a target database/
  schema/table). One schedule's failure is logged and does not stop the
  others -- same don't-abort-on-one-failure convention as every other loop
  in this codebase.
- **New migration** `infra/snowflake/11_create_scan_schedules_table.sql`:
  `CORE.SCAN_SCHEDULES` (`SCHEDULE_ID`/`SCHEDULE_TYPE`/`TARGET_DATABASE`/
  `TARGET_SCHEMA`/`TARGET_TABLE`/`INTERVAL_MINUTES`/`IS_ACTIVE`/
  `LAST_RUN_AT`). Deliberately minimal -- an interval in minutes, not a
  cron expression, per mvp-scope.md's "orchestrator on a timer" phrasing,
  not a full cron system.
- **Real code-organization bug found and fixed during verification, not
  by code review**: the scheduler's poll job originally tried
  `from main import run_all_approved_rules, ...` to reuse the exact logic
  the manual "Run Rules" button already used. This raised
  `ModuleNotFoundError: No module named 'main'` on every real tick,
  confirmed directly by watching a live scheduler run against the actual
  backend -- uvicorn loads that file as
  `apps.backend.agent_service.src.main`, not as a bare top-level `main`
  module, and `src/` was never set up as an importable package (no
  `src/__init__.py`). Fixed by extracting the three functions that needed
  to be shared (`recommend_rules_for_table`/`recommend_rules_for_tables`/
  `run_all_approved_rules`) out of `main.py` into a new
  `scan_operations.py` at the package root (sibling to `tools/`/`agents/`/
  `graphs/`/`scheduler.py`) -- both `main.py`'s HTTP routes and
  `scheduler.py`'s poll job now import the identical functions from
  there, with no circular dependency in either direction. This is a pure
  move, not a rewrite -- every function's behavior is byte-for-byte
  unchanged from what previously lived inline in `main.py`, confirmed via
  the regression checks below.
- **Auto-resolve, the literal unlock**: `scan_operations.
  run_all_approved_rules()` -- called by both the scheduler's
  `RULE_EXECUTION` job and the pre-existing manual "Run Rules" button --
  now checks every `PASSED` result for that rule's most recent `OPEN`
  alert (new `storage_tools.get_open_alert_for_rule()`) and transitions it
  to `RESOLVED` via the pre-existing `update_alert_status()`. This is
  deliberately in the *shared* function, not scheduler-only: a manual
  "Run Rules" click that happens to see a rule pass after it previously
  failed is just as much a real "later run" as a scheduled one, and
  should auto-resolve the same way.
- **Frontend**: `AlertsPage.jsx`'s status filter dropdown and
  `App.css`'s `.alert-status-*` badge styles gained a `RESOLVED` option/
  style (green, reusing the existing `ACCEPTED` tint) -- the only UI
  change this feature needed, since `RESOLVED` alerts now genuinely occur.
  **Settings page for creating/managing schedules — RESOLVED, see §29**
  (create + list + deactivate; no edit/delete UI, per that section's scope).
- **Not built**: no dynamic per-schedule APScheduler jobs (one shared poll
  job checks all schedule rows each tick, rather than one APScheduler job
  per `SCAN_SCHEDULES` row) -- simpler, avoids add/remove-job bookkeeping
  as schedules are created/deleted, at the cost of poll-interval
  granularity rather than exact per-schedule timing. No historical-average
  rule types yet (deferred-and-future-work.md #6's volume-skill gap) --
  this scheduler is what makes recurring execution history *possible* to
  accumulate, but the skill itself still doesn't query that history.
- **Verified end-to-end against real Snowflake, with zero manual HTTP
  calls during the actual test window**: fixed a real pre-existing
  synthetic test rule's SQL
  (`APPROVED_RULES.RULE_ID='4e2566fb-5588-498b-80a9-afa8141bd045'`, left
  permanently-failing since an earlier session's feedback-loop
  verification) to guarantee a pass, confirmed its real pre-existing
  `OPEN` alert (`ALERTS.ALERT_ID='fb82a011-...'`), created a real
  `RULE_EXECUTION` schedule via `POST /api/schedules` (1-minute interval),
  then made **no further calls** and simply waited. Confirmed via direct
  query afterward: `SCAN_SCHEDULES.LAST_RUN_AT` populated with a real
  timestamp: three separate real `RULE_EXECUTION_HISTORY` rows appeared
  roughly a minute apart, all `PASSED`; and -- the actual target behavior
  -- `ALERTS.STATUS` for `fb82a011-...` flipped from `OPEN` to `RESOLVED`
  with a real `UPDATED_AT` timestamp, with no `/run` or `/run-all` call
  made by a human or a test script during that window. Separately created
  a real `RESCAN` schedule against `PLAYGROUND_DB.RAW.
  REPLAY_RAW_RECORDS_TBL` (chosen for safety, same reasoning as every
  prior schema-scan test in this project) and confirmed two real new
  `SCAN_RUNS` rows appeared on their own, one `COMPLETED`. Both test
  schedules were deactivated (`IS_ACTIVE=FALSE`) after verification rather
  than deleted, so the real proof (`LAST_RUN_AT` history, the resolved
  alert, the execution history rows) stays intact -- same "leave real
  verification evidence in place" convention as this project's other
  live-verified features.

## 29. Sample Failed Rows, Volume historical-average, and a Settings page for schedules (closes §6/§15's sample-rows gap, §6's volume gap, and §28's UI gap)

Three previously-tracked gaps, closed in one session:

- **Sample failed rows / `ALERT_VIOLATION_SAMPLES`** — the ask: attempt
  sample rows for every rule type, template and Claude-sourced alike; a
  combination of real rows (row-level types) or a note+evidence fallback
  (table-level/no-SQL types) where real rows aren't possible.
  - **New `tools/rule_template_tools.py` sample builders**:
    `completeness_sample_sql()`/`uniqueness_sample_sql()`/
    `accepted_values_sample_sql()`/`email_format_sample_sql()`/
    `positive_amount_sample_sql()` — row-returning `SELECT * ... LIMIT 10`
    counterparts to the existing `COUNT(*)`-aggregate templates, same
    WHERE/HAVING predicate reused. `freshness_evidence_sql()` (a single
    `MAX(timestamp)` row, not per-row) backs FRESHNESS's evidence fact.
    New dispatcher `render_sample_sql_for_rule()` mirrors
    `render_sql_for_rule()`'s VALIDITY sub-type disambiguation, returning
    `None` for FRESHNESS/VOLUME/unknown rule_type (no per-row predicate
    exists for those). No `LIMIT` enforcement exists anywhere in
    `tools/sql_validation_tools.py` (confirmed directly) — every sample
    function hardcodes its own `LIMIT 10`.
  - **New `tools/pii_detection_tools.mask_sample_rows()`** — row-level
    masking (distinct from `claude_tools._mask_column_profile()`, which
    masks column-*profile* stats, not row *values*) per each column's full
    3-tier `LLM_SHARING_POLICY`: `ALLOW_RAW_SAMPLE` passes through,
    `ALLOW_MASKED_SAMPLE` → `***MASKED***` placeholder, `ALLOW_STATS_ONLY`
    (or a column with no policy on record at all) → field dropped
    entirely. Missing-policy defaults to dropped, not passed-through —
    same "never silently downgrade to safe-to-share" convention
    `agents/pii_agent.py` already applies to its own Claude-failure
    fallback.
  - **New `tools/sample_query_tools.build_sample_failed_rows()`** — the one
    shared dispatcher both call sites use, producing a single shape:
    `{rows: list[dict] | None, note: str | None, evidence: dict | None}`.
    Row-level type with real failures → `rows` populated (masked). FRESHNESS
    → `evidence: {most_recent_value, column}`. VOLUME → `evidence:
    {current_row_count, historical_avg_row_count}` (no extra query — reads
    straight off `threshold_config`/`total_count` already in hand). No SQL
    at all (a Claude-sourced rule_type the template dispatcher doesn't
    recognize) → a descriptive `note`, same "recommended, not yet
    executable" treatment this codebase gives those rules elsewhere. A
    row-level type that passed (`failed_count` is `0`/`None`) returns
    `None` outright — nothing to show, no reason to spend a query proving
    it.
  - **Test-execution path**: `agents/rule_test_execution_agent.py`'s
    `run_rule_test_execution_agent()` gained a `column_profiles` param
    (threaded through from `agents/scan_pipeline.py` and
    `graphs/dq_workflow_graph.py`, both of which already have it in scope
    at that point in the pipeline — `pii_agent` runs before this agent, so
    every column already carries a real `llm_sharing_policy`, no extra
    storage lookup needed). `test_result` gains a `sample_failed_rows` key,
    stored inside `RECOMMENDED_RULES.TEST_RESULT`'s existing VARIANT column
    — no migration needed, `04_create_rule_tables.sql`'s DDL comment
    already documented this exact key.
  - **Real-execution path**: `agents/rule_execution_agent.py` builds
    `sample_failed_rows` only on a real `FAILED` result (via a new
    `storage_tools.list_latest_column_profiles()` lookup for column
    policies, since this agent starts from `get_approved_rule()`, not a
    fresh scan, and has no `column_profiles` in memory the way the
    test-execution path does), then passes it into
    `agents/alert_agent.run_alert_agent()`'s new 4th parameter.
    `alert_agent.py` calls `storage_tools.store_violation_samples()` right
    after `store_alert()` returns a real `alert_id` — this function
    already existed (built for a previous ask) but was dead code, never
    called by anything, until now.
  - **New read path**: `storage_tools.get_violation_samples()`, wired into
    `get_alert()` as a `violation_samples` key (single-alert fetch only,
    not `list_alerts()` — avoids an N+1 on the Alerts Dashboard's list
    view). New route `GET /api/alerts/{alert_id}` (didn't exist before —
    only mutating alert routes existed).
  - **Frontend**: `App.jsx`'s `RuleDetailRow` replaced its raw
    `JSON.stringify(rule.test_result)` line with a structured
    `SampleFailedRows` component (a real table when `rows` exist, else the
    `note`/`evidence` fallback). `AlertsPage.jsx`'s `AlertDetailRow` now
    fetches its own `GET /api/alerts/{alert_id}` on expand (mirrors
    `RuleEditForm`'s existing own-fetch-on-demand pattern) since
    `list_alerts()` doesn't carry `violation_samples`, and renders the same
    component (duplicated per-file, same convention as `formatTimestamp`).
  - **Verified end-to-end against real Snowflake**: a real scan on
    `REPLAY_BRONZE_INGESTION_AUDIT_TBL` showed the VOLUME rule's
    `sample_failed_rows` correctly using the `{rows: null, note, evidence}`
    shape. Directly exercised `run_rule_test_execution_agent()` against a
    real guaranteed-failing STATUS check with synthetic column policies
    (`ALLOW_MASKED_SAMPLE` on `BATCH_ID`, `ALLOW_RAW_SAMPLE` on `STATUS`)
    and confirmed the returned row had `BATCH_ID` masked and `STATUS`
    passed through raw. Separately edited a real recommended rule's SQL to
    guarantee a real failure, approved it, ran it via the real `/run`
    route, and confirmed a real `ALERT_VIOLATION_SAMPLES` row with the
    actual offending row's data, readable back via `GET
    /api/alerts/{alert_id}` and rendered correctly in a live headless-
    Chromium session (Playwright against system Chrome, same approach as
    every other frontend verification in this project) with zero console
    errors. `mask_sample_rows()` itself unit-verified directly with
    synthetic `ALLOW_RAW_SAMPLE`/`ALLOW_MASKED_SAMPLE`/`ALLOW_STATS_ONLY`/
    missing-policy columns, confirming all three tiers plus the
    default-to-dropped behavior.
  - **Not built**: no caching of a rule's sample rows across re-scans (a
    re-tested rule gets a fresh sample query every time, same as every
    other per-scan computation in this codebase). `ExecutionHistoryPage.jsx`
    does not display sample rows — that page reads `RULE_EXECUTION_HISTORY`
    directly, which has no VARIANT column at all; samples for the
    real-execution path live only in `ALERT_VIOLATION_SAMPLES` (keyed by
    alert, not execution), so only a `FAILED` run that produced a real
    alert has a sample to show, reachable via the Alerts Dashboard, not
    Execution History.

- **Volume skill's historical-average comparison** — the ask: once enough
  scan history exists, compare current row count to the historical average
  instead of only checking for an empty table.
  - **New `storage_tools.list_table_profile_history()`** — most recent N
    `PROFILING.TABLE_PROFILES` rows for one table (that table naturally
    accumulates one row per scan already, via `store_table_profile()`'s
    always-insert-never-upsert behavior), each `{row_count, profiled_at}`.
    Called from `agents/rule_recommendation_agent.py` itself (same
    "agent does its own storage lookup, wrapped in try/except" pattern
    `_apply_feedback()`'s `get_feedback_for_table()` call already
    established there), not pushed up into the graph/pipeline — keeps
    `skills/volume_skill.py`'s `suggest_volume_rules()` a pure function per
    `skills/_shared.py`'s convention, just with a new optional
    `row_count_history` param.
  - **Logic**: fewer than 3 prior profiles → unchanged static ">0" rule,
    byte-for-byte identical to before this feature. 3 or more → **replaces**
    (not adds to) the static rule with a historical-average check —
    proposing both would be two approval decisions about the same
    underlying concern, since the average check is a strict superset (an
    empty table after a positive average is itself a huge deviation,
    correctly flagged CRITICAL). Bands: the rule's SQL trips at ±30%
    deviation from the average (the more sensitive trigger, so drift gets
    caught early once approved and re-run on a schedule); the *proposed*
    rule's initial severity is CRITICAL if the current snapshot is already
    ≥50% off the average at proposal time, else WARNING — same "severity
    from current evidence" pattern the static rule already used. Both bands
    always recorded transparently in `threshold_config`
    (`historical_avg_row_count`/`warning_band_pct`/`critical_band_pct`/
    `profiles_considered`/`current_row_count`) regardless of which one drove
    the severity choice.
  - **New `rule_template_tools.volume_historical_sql()`** — also fixes a
    pre-existing bug in the legacy `volume_sql()` in passing: that function
    hardcoded `TOTAL_COUNT=1` instead of the real row count (kept
    unchanged for backward compatibility with any already-approved rule
    using the old shape); the new function reports the real `COUNT(*)` as
    `TOTAL_COUNT`, which the sample-rows feature's VOLUME evidence display
    depends on. `render_sql_for_rule()`'s VOLUME branch dispatches to
    whichever shape a rule's `threshold_config` implies
    (`historical_avg_row_count` present → new function; absent → legacy
    `volume_sql()`), so old approved rules keep working unchanged.
  - **No migration needed** — `THRESHOLD_CONFIG` is already VARIANT, same
    as every other skill's computed-threshold convention (e.g.
    `freshness_skill.py`'s `max_age_hours`).
  - **Feedback loop interoperates for free**: `_apply_feedback()`'s
    existing EDIT-threshold-override-then-re-render-SQL logic already
    reads whatever's in `threshold_config` and calls `render_sql_for_rule()`
    — since that function's VOLUME branch already reads
    `historical_avg_row_count`/`warning_band_pct` straight out of
    `threshold_config`, a human's edited baseline/band flows through on the
    next scan with zero VOLUME-specific code added to `_apply_feedback()`.
  - **Verified end-to-end against real Snowflake**: a real scan on
    `REPLAY_BRONZE_INGESTION_AUDIT_TBL` (which had 6 prior `TABLE_PROFILES`
    rows from earlier sessions, past the 3-profile threshold) correctly
    produced a historical-average VOLUME candidate — `threshold_config`
    showing `historical_avg_row_count: 1.0`, `profiles_considered: 6`,
    `generated_sql` matching `volume_historical_sql()`'s exact
    `ABS(COUNT(*) - avg) > avg * pct/100.0` shape, `TOTAL_COUNT` reporting
    the real row count (not the legacy hardcoded `1`), and `severity:
    WARNING` (current count matched the average exactly, 0% deviation).
  - **Not built**: no user-facing UI to configure the 3-profile threshold or
    the 30%/50% bands (fixed module constants, same "not yet a visible
    setting" gap `architecture.md` §10 already flags for sampling depth).

- **Settings page for schedule management** — the ask: create + list +
  deactivate (no edit/delete). `POST /api/schedules`/`GET /api/schedules`
  already existed as API-only routes with no frontend consumer.
  - **New backend**: `storage_tools.get_scan_schedule()` (single-row
    fetch, same fetch-then-404 convention as `get_alert()`/
    `get_approved_rule()`) and `set_scan_schedule_active()` (flips
    `IS_ACTIVE`). New route `POST /api/schedules/{schedule_id}/deactivate`
    — verb-suffixed-POST, matching `/api/rules/{rule_id}/approve`'s
    convention rather than a generic PATCH (which this codebase reserves
    for genuine multi-field edits). Deliberately **idempotent, no 409** on
    an already-inactive schedule — a toggle isn't a domain-irreversible
    transition the way approve/reject is, so a stale-UI double-click
    shouldn't be punished the way `_require_pending_rule()`'s repeat-action
    409 punishes a double-approve.
  - **New frontend** `apps/web/src/SettingsPage.jsx` — same shape/
    conventions as `TableHealthPage.jsx` (own `API_BASE`, `loadSchedules()`/
    `useEffect` on mount, `.rules-page` shell). Create-schedule form
    (`.rule-edit-form`, extended to also style plain `<input>` — previously
    only styled `select`/`textarea`, since no prior form in this codebase
    used bare text/number inputs inside that class) with `schedule_type`/
    `target_database`/`target_schema`/`target_table`/`interval_minutes`
    fields (schema/table only shown for `RESCAN`; `target_database` always
    required per the backend's `CreateScheduleRequest`, shown with a note
    that it's ignored for `RULE_EXECUTION` rather than hidden, so the
    required-ness isn't a submit-time surprise). Schedules table reusing
    `.active-badge`/`.rules-table`/`.reject-button` verbatim. A `.muted`
    caveat note explains the SSO/session limitation already documented in
    §10/§28 (schedules run on the same interactive session as manual
    actions; a backend restart needs one human login before a schedule can
    fire for real).
  - **Wired into `App.jsx`**: new "Settings" nav tab, same
    self-contained-no-props pattern as `AlertsPage`/`ExecutionHistoryPage`/
    `TableHealthPage`.
  - **Verified end-to-end against real Snowflake and a live browser**:
    created a real schedule via the live form (Playwright against system
    Chrome), confirmed it appeared in the list as Active, deactivated it via
    the live button, confirmed it flipped to Inactive — both steps
    re-fetching the whole list rather than patching one row locally, same
    convention as `ActiveRulesPage`'s `runNow()`/`runAll()`. Zero console
    errors throughout. `npm run build` (Vite production build) also
    confirmed all three changed/new frontend files compile cleanly with no
    syntax errors.

---

## 30. Agentic Deep Scan — Claude explores data before recommending rules

**Status: not built — design decision documented here for future implementation.**

### What the current system does

When Claude recommends rules, it works from a pre-computed snapshot: null
percentage, distinct count, min, max, and the top-N most frequent values for
each column. That snapshot is sufficient for the majority of quality rules
(completeness, range checks, allowed-value sets, cross-column consistency).

### The gap: tail anomalies that snapshots can't see

Pre-computed statistics are aggregate summaries. A column like `AGE` might
have `min=1`, `max=999`, `null_pct=0.0`, and top values of `[28, 34, 31, 29,
45]` — all of which look unremarkable. The outlier value `999` (a sentinel
used by a upstream system to mean "unknown") appears in only 5 of 10 million
rows, so it never surfaces in top values and does not affect min/max in a way
that screams wrong. Under the current design, Claude sees nothing about those
5 rows and will not write a `VALIDITY` rule flagging `AGE > 150` as
impossible.

A human analyst running `SELECT AGE, COUNT(*) FROM table GROUP BY AGE ORDER
BY AGE DESC LIMIT 20` would catch this in seconds. Claude cannot do that
today.

### Proposed feature: agentic tool-use loop ("deep scan")

Give Claude a `run_query` tool during the recommendation call so it can issue
exploratory SELECT statements before deciding which rules to propose. The
recommendation call becomes a tool-use loop rather than a single
`messages.create`:

1. Claude receives the column profile snapshot as usual (first message).
2. Claude may call `run_query(sql)` zero or more times to investigate columns
   it finds suspicious — checking tail distributions, rare-value counts, or
   cross-column relationships.
3. Each query result is returned to Claude as a tool result; Claude can issue
   follow-up queries.
4. When Claude calls the `recommend_rules` tool (its final structured output),
   the loop ends and the rest of the pipeline runs exactly as today.

This is strictly opt-in: the existing fast path (pre-computed stats only, no
extra queries) stays the default. Deep scan would be a user-selectable mode,
comparable to enabling "advanced analysis" before a scan.

### Motivating example end-to-end

```
Table: HR.EMPLOYEES (50 M rows)
Column: AGE  min=1  max=999  null_pct=0.0  top_values=[28,34,31,29,45]

Claude (deep scan): This max=999 looks suspicious for age. Let me check the
distribution at the high end.

run_query("SELECT AGE, COUNT(*) FROM HR.EMPLOYEES WHERE AGE > 120 GROUP BY AGE")
→ [{"AGE": 999, "COUNT": 5}]

Claude: Found 5 rows with AGE=999, a sentinel value. Recommending:
  rule_type: VALIDITY
  rule_name: AGE must be a realistic human age (≤ 120)
  generated_sql: SELECT COUNT_IF(AGE > 120) AS FAILED_COUNT, COUNT(*) AS TOTAL_COUNT
                 FROM HR.EMPLOYEES
```

Without deep scan, this rule is never proposed. With deep scan, it surfaces
automatically.

### Tradeoffs

| Axis | Impact |
|---|---|
| Latency | Each `run_query` call takes 0.5–5 s. A cap of ~10 queries per table adds up to ~50 s worst case in addition to the LLM round-trips. |
| Cost | Extra LLM calls and tokens. At Sonnet 5 pricing and 10 turns per table, rough overhead is ~$0.05–0.10 per table in deep-scan mode. Acceptable for a consciously-chosen deeper scan. |
| Query safety | All queries must pass through the existing SQL validator before execution. The validator already rejects DDL/DML and enforces a row-limit cap. No new safety surface — the same guardrails that protect test-run queries protect exploration queries. |
| False positive rate | More rules proposed does not mean better rules. Claude should be instructed to only issue exploration queries when it has a concrete suspicion (not to "explore randomly"), and to set confidence appropriately lower for rules derived from tail findings. |
| Idempotency | Exploration queries are read-only SELECTs. They do not affect storage or rule deduplication — the loop produces a rule list, which flows into the same dedup/scoring/feedback pipeline as today. |

### Design: opt-in, not the default

Fast scan (current) remains the default because:
- Most tables don't have tail-anomaly issues that snapshots miss.
- Latency matters for schema-wide and database-wide scans across hundreds of tables.
- Pre-computed stats already catch the vast majority of quality issues.

Deep scan should be opt-in via a UI toggle on the scan configuration screen
(same area as the existing "Manual Scan Options" from §22). Something like
**"Deep scan (Claude explores data — slower, more thorough)"**.

### Infrastructure already in place

- **SQL validator** (`sql_validation_agent.py`, `tools/sql_validation_tools.py`): already enforces
  SELECT-only, row-limit cap, and Snowflake syntax validation. Reuse as-is.
- **`run_query` / `run_source_query`** (`tools/snowflake_connection.py`): already used by
  the test execution agent. Reuse as-is.
- **Timeout/cap pattern**: the test execution agent already implements per-query
  timeouts. Same cap (~30 s per query, ~10 queries total) applies here.
- **Bedrock tool-use loop**: the codebase already uses forced tool use for
  the `recommend_rules` call. Extending to a multi-turn loop is the same
  API surface — add `run_query` as an additional tool, change the call from
  "stop after first tool use" to "loop until `recommend_rules` is called."

### What needs to be built

1. **`run_query` tool definition** in `claude_tools.py`: add a second tool
   alongside `recommend_rules` with a schema that accepts a single SELECT
   statement. The implementation calls `run_source_query()`, passes the result
   through the SQL validator first, and returns rows as a JSON array (capped at
   ~100 rows to control token cost).
2. **Loop driver** in `recommend_rules_with_claude()`: change from a single
   `messages.create` call to a `while` loop that sends tool results back until
   Claude calls `recommend_rules` or the turn cap (10) is hit. If the cap is
   hit without a `recommend_rules` call, fall back to a final forced call with
   the accumulated context.
3. **System prompt update**: instruct Claude that `run_query` is available,
   explain when to use it ("when a column stat looks suspicious and a targeted
   distribution query could confirm or rule out an anomaly"), and set the
   expectation that shallow exploration is better than exhaustive exploration.
4. **UI toggle**: add "Deep scan" checkbox to the scan configuration modal
   (§22's `ScanModal` or equivalent). Pass a `deep_scan=true` flag through
   the API to `scan_operations.py`, which passes it into
   `recommend_rules_with_claude()`.
5. **Session variable for table context**: the `run_query` tool needs to know
   which table is being scanned so it can reject queries against other tables.
   This is already implicitly available (the table name is in scope in
   `recommend_rules_with_claude()`); the tool implementation just needs to
   enforce it.

### Verification plan (when built)

- Unit: mock `run_source_query` to return a synthetic outlier row; assert that
  Claude's final rule set contains a VALIDITY rule targeting that column.
- Integration: run deep scan against a real table that has a known sentinel
  value (e.g., a test table seeded with `AGE=999`); assert the rule appears
  in the recommended set and its `generated_sql` correctly flags the outlier.
- Safety: assert that a `run_query` call with a `DROP TABLE` or `UPDATE`
  statement is rejected by the validator and the loop continues (does not abort
  the whole scan).

---

## Explicitly out of scope for now (per `mvp-scope.md`, not gaps)

Account-wide scanning · multi-step approval · Slack/PagerDuty/email ·
chat with the system · cross-database support · advanced domain learning ·
audit logs · RBAC · secret manager · multi-tenant isolation · LLM fallback
strategy · evaluation framework. See `mvp-scope.md` for the full MVP1→3
phasing these belong to.
