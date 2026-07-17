# DQ Tool — Future Work

Session notes from 2026-07-16 → 2026-07-17. Captures what was shipped, what
was declined, and what remains open.

---

## Shipped this session

### 1. Metadata-rule rendering fixes (frontend)

**Problem:** Metadata-shape rules (PII_COLUMN_NO_MASKING, GENERIC_COLUMN_NAME,
COLUMN_TYPE_MISMATCH, FK_COLUMN_NO_CONSTRAINT, NULLABLE_ID_COLUMN,
INCONSISTENT_NAMING…) have no failing rows — `dynamic_rules._finding`
defaults `fail_count=1, total_count=1, sample_rows=[]`. The UI was rendering
these as "1/1 rows failing (100%)" and hiding the actual rule-specific
evidence.

**Fix:**
- `FindingDetailDrawer.tsx` — added an **Evidence** section that renders
  rule-specific evidence keys (`matched_pattern`, `data_type`, `actual_type`,
  `expected_types`, `sample_columns`, …) as a key/value table. Filters out
  the standard contract keys (`fail_count`/`total_count`/`sample_rows`) so
  it only shows the interesting stuff. Covers every current + future
  dynamic rule automatically.
- `FindingDetailDrawer.tsx` — suppressed the "Current fails" summary tile
  and Run history table on metadata-shape rules (detected via
  `sample_rows==[] && total<=1 && fail<=1`).
- `Findings.tsx` — suppressed "1/1 rows failing (100%)" chip and the
  sparkline on metadata rules (flat line at fail_count=1 conveys nothing).
- `DataHealthPanel.tsx` — gated the "N/M rows (%)" chip behind
  `total_count > 1`.

### 2. Schema drift detection (Tier 1)

**Problem:** Table/column changes were silent until a downstream rule broke.
Structural drift needed to be a first-class finding.

**Shipped:**
- New module: `backend/app/services/schema_drift.py`
- Handlers: `column_added` (low), `column_removed` (high),
  `column_type_changed` (high), `column_nullability_changed` (medium).
- `table_removed` handler defined but not emitted (needs a schema-sweep
  pass, not a per-table scan — future work).
- No `TABLE_ADDED` — a scan implies existence.
- `snapshot_columns()` reads prior ASSETS state BEFORE the scan upsert;
  `detect_column_drift()` diffs vs live and emits findings anchored to
  the table asset.
- `_ensure_per_table_drift_instance()` auto-provisions per-table
  RULE_INSTANCES on demand — unlike normal python_handler rules, drift is
  always-on with no human approval required.
- Type comparison normalised to canonical head (`NUMBER(38,0)` →
  `NUMBER`) so precision-only re-declares don't fire.

**Wiring:**
- Agentic path: `scan_service.scan_metadata_only` stashes drift on
  `scan.drift_findings`; `FindingsAgent.run` merges into `findings_data`
  and adds drift iids to `executed_instance_ids` so the finalizer's
  PASS branch auto-resolves them when schema stabilises.
- Legacy path: `scan_service.scan_table` computes drift after upsert and
  folds into `findings_data` before `finalize_scan`.
- FindingsAgent logs `RULE_EXECUTIONS` `passed` row only when a prior
  open drift incident exists — avoids per-scan noise.

**Behaviour:** First scan returns 0 drift findings (no prior snapshot).
Re-added removed column auto-resolves. Re-drop within 7d REOPEN window
reopens the same incident (flapping detection via existing lifecycle).

**Memory:** `dq_tool_schema_drift.md`.

---

## Declined this session

### Tier 2 rename detection

Drop+add rename heuristic (Levenshtein + type match). Declined:
- False rename attributions are worse than two clean findings — users
  chase phantom renames instead of real drops.
- Two separate findings surface the same info; users infer rename from
  context.
- Monte Carlo and Soda don't do deterministic rename detection either.

Revisit if a real user asks.

### Column-role classification (as proposed by another AI)

The suggestion was to classify columns as Identifier / Business key /
Timestamp / Amount / Currency / Status / Country / Email / PII /
Categorical / Free-text.

Declined because we already do this **implicitly**:
- `pk_shaped_candidates` — identifier / business key
- `freshness_signals` — timestamp
- `closed_set_columns` — categorical
- `PII_KEYWORDS` / `PII_PATTERNS` in `dynamic_rules.py` — PII
- `NAME_TYPE_RULES` — amount/date-like

RuleIntelligenceAgent already feeds these to Claude, which uses them to
skip nonsensical checks. The AI's list overlaps 80% with what's already
reasoned about. Persisting explicit labels would be nice-to-have for
filtering ("show findings on identifier columns") but not load-bearing.

---

## Open — table-kind classification

**Current state:** RuleIntelligence already classifies tables as
`fact | dimension | staging | config | audit | reference | unknown` with
confidence + reason. Output lives in `RULE_INTELLIGENCE_LOGS.table_type`.
Used only inside the AI proposal loop.

**Gaps identified:**

1. **Not on ASSETS.** Lives on a per-run log — hard to query "give me all
   fact tables" without joining the latest log per table. Should be
   denormalised onto `ASSETS.raw_metadata.table_type` on every
   RuleIntelligence run.

2. **Deterministic rules don't gate on it.** `dynamic_rules.py` runs every
   check on every column regardless of table kind. Noise examples:
   - `PII_COLUMN_NO_MASKING` on staging tables (often transient).
   - `NULLABLE_ID_COLUMN` on audit tables (audit rows may intentionally
     have nullable references).
   Should add a `_kind_allows(rule_code, table_type)` gate.

3. **Not surfaced in the UI.** Findings page / Data Health could show
   table kind as a badge and let users filter by it — a common triage
   move.

4. **Column roles not persisted** (see declined section above — implicit
   inference is sufficient for now).

**Recommended small-scope next move:**
- Denormalise `table_type` + confidence onto `ASSETS.raw_metadata` on
  every RuleIntelligence run.
- Add a `TableKindBadge` to the Findings card / Data Health row.
- Add a `table_type` filter dropdown.
- Skip the `_kind_allows` gate for now — revisit after seeing real
  scan data, since suppressing a real PII issue on a "staging" table
  that's actually consumer-facing is a bigger risk than the noise.

---

## Backlog (from `dq_tool_next_features.md`)

Higher-priority features still on the list:
- Anomaly detection (row-count / null-rate seasonality)
- Volume checks (freshness + row-count SLAs)
- Great-Expectations parity items

---

## Views support (raised, not decided)

Both data source adapters currently enumerate only base tables:
- Snowflake: `SHOW TABLES` (excludes views by design).
- Postgres: filters `pg_class.relkind='r'` (excludes `'v'` and `'m'`).

Views/materialized views never enter ASSETS → no scans, no rules, no
findings.

**Arguments for supporting:** users query curated data through views;
row-level rules (nulls, uniqueness, ranges) work fine on views.

**Arguments against:** views can be expensive to scan; metadata-shape
rules are less meaningful (underlying tables are the source of truth).

**Minimal scope if built:** extend `list_tables` on both adapters to
return views with a `kind: 'view' | 'table'` flag, store on ASSETS,
skip schema-shape dynamic checks when `kind='view'`. Larger scope:
separate `SHOW MATERIALIZED VIEWS` handling for volume/freshness.

If we add view support, also track view DDL hash — a view redefinition
is drift.

---

## Rule recommendation inputs — coverage audit (2026-07-17)

Mapping the "what should a recommender consider?" checklist to what
RuleIntelligenceAgent currently ingests:

| Signal | Status | Where |
|---|---|---|
| Schema | ✅ | MetadataAgent → ASSETS |
| Data types | ✅ | `column_stats` + live INFORMATION_SCHEMA reads in `dynamic_rules._fetch_live_column_metadata` |
| Column names | ✅ | `PII_KEYWORDS`, `NAME_TYPE_RULES`, and Claude sees raw names in the RI prompt |
| Profile statistics | ✅ | `ProfilingAgent` → `column_stats`, `pk_shaped_candidates`, `freshness_signals`, `closed_set_columns` |
| Historical behaviour | ⚠️ partial | `RULE_INTELLIGENCE_LOGS` feeds prior classification + prior proposals as memo lines (rule_intelligence_agent.py:1578). Missing: execution-history conditioning, seasonality baseline |
| Table classification | ✅ | RI outputs `table_type` + confidence — see open item above |
| Relationships between tables | ✅ | `relationship_discovery.get_or_refresh_catalog` (24h TTL) |
| Business descriptions | ⚠️ partial | ASSETS `comment` + column COMMENT are fed to Claude. Missing: external glossary/data-catalog integration (Alation, Collibra, dbt docs) |
| Existing checks | ✅ | `existing_instances` passed explicitly — RI instructed to avoid duplicating (rule_intelligence_agent.py:445+) |
| Query & pipeline metadata | ❌ | Nothing. No QUERY_HISTORY mining, no dbt/Airflow lineage |

### Gaps worth adding

1. **Query history mining (Snowflake ACCOUNT_USAGE.QUERY_HISTORY).**
   Highest-signal missing input. Tells us which columns are actually
   used, in what predicates, by how many pipelines. A NEVER-referenced
   column shouldn't get expensive uniqueness checks. A column used in
   every JOIN predicate probably deserves a uniqueness / not-null rule
   even if the profiler didn't flag it as pk-shaped.

2. **Execution-history conditioning.** Flapping rules
   (`reopened_count > 3`) should be down-ranked or auto-suggested for
   mute/retirement. Rules passing for 90+ days could be surfaced as
   "graduate to a stricter threshold." Data lives in `RULE_EXECUTIONS`
   + `FINDINGS.reopened_count` — just not consumed by RI today.

3. **dbt / Airflow lineage ingestion.** Out of scope for the tool as a
   self-contained product; belongs behind a config flag once a user
   has these systems and wants to point at them.

### Recommendation

Current recommender is genuinely competitive — beats Great Expectations
(no AI), matches Soda's coverage on this axis. Two builds worth doing
next, both as additions to RuleIntelligenceAgent (no new pipelines):

1. Query-history mining (Snowflake first; Postgres can use
   `pg_stat_statements` later).
2. Execution-history conditioning (in-repo data, quick win).

---

## Rule library — coverage audit (2026-07-17)

Mapping the "reusable rule definitions" checklist to what the platform
can propose today via templates + `draft_sql`.

| Category | Check | Status | How it's proposed |
|---|---|---|---|
| **Completeness** | Null count / percentage | ✅ | `not_null` template — RI proposes per-column with % threshold |
|  | Empty-string detection | ⚠️ | Via `draft_sql` — no dedicated template |
|  | Required-column validation | ✅ | Covered by schema drift `column_removed` (Tier 1) |
| **Uniqueness** | Duplicate count / unique-key | ✅ | `uniqueness` template |
|  | Composite-key uniqueness | ✅ | `duplicate_key` template (multi-column) |
| **Validity** | Accepted values | ✅ | `accepted_values` template — auto-proposed via `closed_set_columns` |
|  | Regex matching | ✅ | `regex_match` template |
|  | Value ranges | ✅ | `range` template |
|  | Data-type validation | ✅ | `COLUMN_TYPE_MISMATCH` dynamic check |
|  | Date validity | ⚠️ | Via `draft_sql` when needed |
| **Consistency** | Cross-column conditions | ✅ | Via `draft_sql` — no template, but Claude routinely writes these |
|  | Business logic checks | ✅ | Same — `draft_sql` path designed for this |
|  | Conditional requirements | ✅ | Same (e.g. "if status=SETTLED then settlement_date NOT NULL") |
| **Referential integrity** | FK existence / orphans | ✅ | `referential_integrity` template, seeded by `relationship_discovery` |
|  | Parent-child consistency | ⚠️ | Via `draft_sql` |
| **Timeliness** | Freshness | ✅ | `freshness` template, seeded by `freshness_signals` profiler output |
|  | Data-arrival deadline | ⚠️ | Same as freshness — `max_age_hours` param |
|  | Processing delay / missing batches | ❌ | Not modeled — needs a batch cadence concept |
| **Volume** | Row-count thresholds | ❌ | Backlog |
|  | Historical-volume comparison / sudden change | ❌ | Backlog — needs baseline storage + anomaly detection |
| **Reconciliation** | Source vs target / financial totals / file vs load | ❌ | Not modeled — needs a separate "reconciliation job" concept |
| **Distribution** | Mean/median shifts, quantile drift, category proportions | ❌ | Backlog — anomaly detection item |
| **Schema** | Missing / unexpected columns / type change | ✅ | Schema drift Tier 1 (shipped) |
|  | Column-order changes | ❌ | Deliberately excluded — Snowflake doesn't preserve semantic column order |

### Will AI propose these when needed?

**Yes**, for anything that maps to an existing template shape or that
Claude can express in `draft_sql`. RuleIntelligenceAgent gets the
template shape list in its prompt plus profiler signals that seed
proposals:
- `freshness_signals` timestamp → freshness rule
- `closed_set_columns` categorical → accepted_values
- `pk_shaped_candidates` column → uniqueness

The `draft_sql` path covers everything else (cross-column business
rules, conditional requirements, non-templated validity) with the SQL
validator + repair loop keeping it safe.

### What AI CANNOT propose today

Not "didn't think of it" gaps — "engine can't run it" gaps:
- Volume anomalies (no baseline table)
- Distribution drift (no distribution snapshots)
- Reconciliation (no source/target job model)
- Missing batches (no batch cadence concept)
- Processing delay (no pipeline event ingestion)

All four map to the anomaly-detection + volume-checks backlog item.

---

## Historical baseline & anomaly detection (2026-07-17)

Not shipped. Biggest gap vs Monte Carlo / Anomalo.

### What we have that's adjacent

- `RULE_EXECUTIONS` logs `evidence.fail_count / total_count` per run —
  an accidental time series, not consumed.
- `ProfilingAgent` computes `column_stats` (min/max/mean/null%/distinct%)
  fresh every scan — not persisted across scans.
- `freshness_signals` gives per-scan "latest timestamp" per candidate
  column — not persisted longitudinally.

### What we don't have

- **Metrics table.** No `METRIC_HISTORY` — no substrate for baselines.
- **Baseline computation.** No mean/MAD, no rolling window, no seasonal
  decomposition.
- **Seasonality handling.** No day-of-week, month-end, holiday awareness.
- **Environment segmentation.** No prod-vs-dev baseline concept.

### Build plan

**Tier A — must-have (~2–3 days):**

1. `METRIC_SNAPSHOTS` table: `(asset_id, metric_name, scan_id, value, ts)`.
   Populate on every scan for: row_count, null_pct per column,
   distinct_count per column, freshness_lag_hours, mean/p50/p95 per
   numeric column.
2. Baseline computation: rolling 30-day window per metric, mean + MAD
   (median absolute deviation — more robust than stddev). Refresh
   nightly into `METRIC_BASELINES`.
3. Three new template shapes:
   - `metric_anomaly` — flag when current value is >N MADs from baseline
   - `metric_relative_change` — flag % change vs same-day-last-week
   - `category_disappeared` — flag when a value in the closed-set for
     30d suddenly isn't
4. RuleIntelligence auto-proposes these on tables with ≥14 days of
   history (below that, no reliable baseline).

**Tier B — nice-to-have (~2–3 days):**

5. Day-of-week seasonality — bucket baseline by DoW; a Sunday drop
   compares only to prior Sundays.
6. Month-end awareness — detect "last N business days of month" spike
   patterns from history; don't fire on expected month-end volume.
7. Business events / holidays — user-configured `QUIET_PERIODS` table +
   a UI to mark "don't alert during this window."

**Tier C — declined:**

- Full seasonal decomposition (STL, Prophet). MAD + DoW covers 90% of
  the same failure modes at 5% of the complexity.
- Environment segmentation — solved by pointing separate connections at
  separate envs; no in-tool work needed.

### Recommendation

Ship Tier A only. Let real anomalies tell us which Tier B refinements
matter. Do NOT bolt metric snapshots onto `RULE_EXECUTIONS` — build the
proper table, it'll haunt us otherwise.

---

## Root-cause analysis (2026-07-17)

Partial today. We have the *shape* of RCA but not the depth.

### What we have

- `FindingsExplanationAgent` — generates `evidence.ai_explanation` with
  `{root_cause, affected_scope, fix_action, confidence}`, rendered on
  the Findings card. Uses profiler stats + failing rows sample.
- `evidence.sample_rows` — Claude sees actual failing rows.
- Incident lifecycle timestamps (`first_detected_at`, `reopened_count`,
  `fail_history`) — Claude knows new vs flapping vs worsening.

### What we don't have

| Signal | Status |
|---|---|
| Recent schema changes | ⚠️ drift findings shipped (Tier 1), but explainer doesn't correlate |
| Pipeline failures | ❌ no pipeline event ingestion |
| Upstream table incidents | ❌ no lineage → no "upstream" concept |
| Recent deployments | ❌ no deploy event stream |
| Query-history changes | ❌ same gap as recommendation-inputs audit |
| Volume changes | ❌ no metric baselines (anomaly detection backlog) |
| Changed column distributions | ❌ same |
| Delayed source files | ❌ no file-arrival tracking |
| Failed Airflow tasks | ❌ no Airflow integration |

Current explanation is "here's what the failure looks like in the data"
— not "here's why it happened." Producing the AI's example output
("Most affected records came from source ICE_OP, upstream parsing task
completed with 18% fewer output records than usual") requires
correlation across three separate signal streams.

### Cheap-and-worth-building now

1. **Drift correlation.** If a rule on column X fails and a
   `column_type_changed` (or _removed_/_nullability_) finding for X
   exists in the same scan → explainer says so, links to the drift
   finding. ~1-hour add to `FindingsExplanationAgent`; in-repo data.
2. **Lifecycle correlation.** "Resolved 3 days ago, now failing again
   — same root cause as reopen #2 (link)." Same idea, in-repo data.
3. **Group-by dimension analysis on the failing sample.** If sample
   rows have a `SOURCE_SYSTEM` column and 80% of failures come from
   one value → "18/20 failing rows have SOURCE_SYSTEM='ICE_OP'."
   No new pipeline, just smarter analysis over `sample_rows`. Gets us
   halfway to the AI's example output.

### Expensive

4. **dbt / Airflow / lineage ingestion.** Real work — needs a
   `PIPELINE_EVENTS` table with a stable schema, per-platform adapters.
   Behind a config flag. Defer until users ask.
5. **Volume / distribution deltas.** Depends on anomaly-detection
   Tier A (metric snapshots + baselines). Once that lands, RCA can
   say "row count fell 40% vs 30-day MAD."

### Recommendation

Items 1–3 now (cheap, meaningful uplift). Item 5 rides on
anomaly-detection Tier A. Item 4 is a real integration project best
deferred.

---

## Lineage & impact analysis (2026-07-17)

Not shipped. Biggest missing capability after anomaly detection.

### What we have

- `relationship_discovery.get_or_refresh_catalog` — infers FK
  relationships **within a single schema** via name matching + live
  orphan-rate verification. Cached 24h.

That's it. No upstream/downstream propagation, no source ingestion, no
dashboard/report catalog, no pipeline integration.

### Gap map

| AI's item | Status |
|---|---|
| Which source produced the data | ❌ no source-system field, no ingestion metadata |
| Which pipelines transformed it | ❌ no dbt / Airflow / Fivetran ingestion |
| Which tables depend on it | ⚠️ FK catalog: in-schema only, one hop, no transitive closure |
| Which dashboards / reports use it | ❌ no BI-tool integration |
| Which downstream checks may also fail | ⚠️ derivable *if* we had lineage |
| Impact preview before change/disable | ❌ no UI, no compute |

### Why this is hard

Lineage is an *integration* problem, not algorithmic. Real lineage comes
from parsing dbt manifests, ingesting Airflow DAGs, or mining
QUERY_HISTORY. Monte Carlo / Atlan / Alation have entire teams on this.
We shouldn't pretend to compete broadly — pick a narrow slice.

### Worth building

1. **Snowflake ACCESS_HISTORY lineage (Tier 1).** Mine
   `SNOWFLAKE.ACCOUNT_USAGE.ACCESS_HISTORY` — Snowflake parses
   column-level lineage between tables (last 365 days). One query →
   "table A is read by queries that write to table B" edges. Persist as
   `LINEAGE_EDGES`. Deterministic, no LLM, no parsing. Postgres users
   get nothing here (no equivalent).
2. **Rule-impact preview.** On disable/edit, traverse LINEAGE_EDGES
   transitive closure (capped at N hops) and list downstream tables +
   active rules + open findings. Small UI on the rule detail page.
3. **RCA correlation with lineage.** Combined with RCA cheap-now items,
   a finding's explainer can say "upstream table X had a schema drift
   finding 6 hours ago; you likely inherited the problem."

### Defer

4. dbt / Airflow / Fivetran integration — real project, config-flagged,
   only when users ask.
5. BI-tool integration (Tableau/Looker/Power BI) — same. Ownership
   unclear anyway; most orgs don't want DQ tool hitting BI servers.

### Recommendation

Ship #1 + #2 (Snowflake-only, deterministic, useful within a week of a
scan). #3 comes free once #1 lands. #4 and #5 are integration projects,
not core.

---

## Agentic capabilities (2026-07-17)

Mapping the "specialized agents" checklist to what we have.

### Agent coverage

| AI's agent | Ours | Status |
|---|---|---|
| Discovery | `MetadataAgent` + schema drift | ⚠️ column-level discovery + drift shipped; no cross-schema asset discovery |
| Profiling | `ProfilingAgent` | ✅ full |
| Classification | `RuleIntelligenceAgent` (table_type output) | ✅ (see open item on persisting/surfacing) |
| Recommendation | `RuleIntelligenceAgent` | ✅ template shapes + draft_sql |
| Critic | `VerificationAgent` | ✅ re-runs proposals live; drops non-reproducing instances. Not yet adversarial. |
| Cost | ❌ | not modeled |
| Incident | `FindingsExplanationAgent` | ⚠️ explains what, not why (see RCA section) |
| Governance | ❌ | ownership fields exist, nothing audits them |
| Maintenance | ❌ | **biggest gap — nothing evaluates whether existing rules are still useful** |

### Other AI-specific asks

- **False-positive marking with textual reason** — ⚠️ status
  `false_positive` exists, no free-text "why", no retrieval loop.
- **Reject-a-proposal with textual feedback** — ⚠️ `FeedbackSynthesisAgent`
  + memo replay in `rule_intelligence_agent.py:1578`; reject reasoning
  not first-class in UI.
- **Retrieval-based feedback learning** — ⚠️ memo replay is a crude
  version; no embedding index.
- **Run multiple workflows together** — ✅ coordinator already fans out
  (metadata / profiling / rules_fetch / relationship_discovery in
  parallel). Batch-of-tables parallelism is a scheduling choice.

### Maintenance agent design (highest leverage)

Runs weekly or on-demand. For every active instance:

- **Retire candidate** — no failures in 90d AND no reopens ever → propose `pause`.
- **Flapping** — `reopened_count ≥ 4` → propose `modify` (loosen threshold or mute) or `retire`.
- **Superseded** — a newer instance covers the same target + shape → propose `retire` on the older one.
- **Obsolete target** — asset gone (table dropped) → propose `retire`.
- **Stale threshold** — thresholds set >180d ago on a template rule → propose review.

Emits `MAINTENANCE_PROPOSALS` rows with `{instance_id, action, reason,
evidence}` — users review + accept in a new UI queue, same shape as the
rule approval flow. Deterministic first; LLM narrative later. No new
prompting philosophy — runs against existing `RULE_INSTANCES` +
`RULE_EXECUTIONS` + `FINDINGS`.

### Cost agent

Snowflake `QUERY_HISTORY` has `BYTES_SCANNED` / `TOTAL_ELAPSED_TIME` —
a periodic sweep across our own `RULE_SQL` history could flag "this
rule scans 400GB per run; consider a partition filter." One-day build
once QUERY_HISTORY mining lands (see recommendation-inputs section).

### Governance agent

Simple checks (unassigned owner, no approval trail, uncovered PII
column) — value proportional to whether an org enforces governance.
Skip until asked.

### Retrieval-based feedback learning

Crude memo replay works well enough. Building an embedding index to
remember "user rejected 'freshness on ORDERS' last month" is
over-engineering. Keep memo path, extend to include reject reasons
(free-text on the proposal-decision record).

### Recommendation (ranked)

1. **Maintenance agent** — real gap, deterministic, in-repo data. Next.
2. **Reject-reason capture** — one field + memo threading. Half a day.
3. **Cost agent** — build with QUERY_HISTORY mining when that lands.
4. Defer governance + retrieval-index work.

---

## Chat-assisted rule authoring (2026-07-17)

Current manual rule creation is decent for power users, weak for
everyone else — assumes knowledge of template shapes, threshold config
keys, JSON shape for accepted_values, etc. Auto-proposal via
RuleIntelligence covers the common cases but users still need a way to
express custom checks in their own words.

### Why open-ended chat is the wrong answer

- Free-text prompts drift ("check that orders are correct") — model
  guesses; user can't tell if the guess is right until they see failures.
- No structured output → hard to preview + edit before saving.
- Every session starts from zero context; model can't verify column
  existence, data types, or profile stats without tool calls anyway.

### Guided authoring wizard (recommended shape)

1. User types intent in plain English:
   *"Flag when settlement_date is null but status is SETTLED."*
2. AI turns it into a structured draft — rule shape (draft_sql for
   cross-column), target table, proposed SQL, threshold, severity.
   Renders as an editable form + live SQL preview.
3. AI asks 1–3 clarifying questions ONLY when ambiguous:
   - "Which STATUS column? `ORDER_STATUS` or `TRADE_STATUS`?"
   - "How many failures per scan before this fires — 1, or a percentage?"
   - "Historical rows too, or only new records?"
4. AI dry-runs against live data — shows expected fail count. If 100%
   of rows fail, warns "your SQL matches everything — likely inverted."
5. User accepts / edits / rejects. Reject captures free-text reason
   (feeds memo path in RuleIntelligence).

### Why this beats form OR free-chat

- User writes intent; AI does mechanical translation.
- Clarifying questions bounded (not "let's chat") → no unbounded dialog.
- Live SQL preview + dry-run count = trust builder.
- Structured draft is editable, versionable, uses existing approval path.

### Cost / scope

- ~2–3 days.
- Reuses `draft_sql` validator + `_repair_draft_sql`.
- Reuses proposal-review UI.
- New: `RuleAuthoringAgent` (thin Claude wrapper with fixed tool set —
  `get_sample_rows`, `list_columns`, `dry_run_sql`).

### Recommendation

Build. Guided wizard with structured output + live validation. Anything
more conversational than that is friction.

---

## Data quality vs data observability (2026-07-17)

**Data quality:** is the data itself correct? Nulls, uniqueness, ranges,
referential integrity, business rules. Row-level truth.

**Data observability:** is the *system producing the data* healthy?
Monte Carlo's five pillars — **freshness, volume, distribution, schema,
lineage**. Metadata + operational signals about whether pipelines are
behaving. Data quality is one input to observability, not the whole
thing.

### Coverage map

| Pillar | Data quality | Data observability |
|---|---|---|
| Row-level correctness | ✅ templates + draft_sql + dynamic_rules | n/a |
| Freshness | ✅ `freshness` template | ⚠️ per-rule only; no SLA dashboard, no missed-batch detection |
| Volume | ❌ | ❌ no row-count baselines, no anomaly detection |
| Distribution | ❌ | ❌ no distribution snapshots, no drift detection |
| Schema | ✅ COLUMN_TYPE_MISMATCH, NULLABLE_ID_COLUMN, drift Tier 1 | ✅ drift findings ARE observability signals |
| Lineage | n/a | ⚠️ in-schema FK catalog only — no upstream/downstream |
| Incidents / lifecycle | ✅ UPDATE/RESOLVE/REOPEN/CREATE, mutes, fleet health | ✅ same lifecycle covers observability findings |
| RCA / explanations | ✅ FindingsExplanationAgent | ⚠️ no correlation with drift, upstream, deploys |

### Where we sit

- **Data quality:** at parity with Soda / Great Expectations. Templates
  + draft_sql + RuleIntelligence auto-proposal + VerificationAgent +
  incident lifecycle + fleet health.
- **Data observability:** ~30% of Monte Carlo. We have schema drift
  (shipped today), row-level freshness rules, and the incident
  lifecycle. Missing volume anomalies, distribution drift, and lineage
  — the three pillars that make observability *observability*.

### Highest-leverage next builds (already captured above)

1. Anomaly detection Tier A — metric snapshots + MAD baselines → covers
   volume + distribution pillars.
2. Snowflake ACCESS_HISTORY lineage — deterministic; unlocks lineage +
   impact preview + upstream RCA.
3. Batch cadence / missed-batch detection — extends freshness into full
   observability.

Solid **DQ tool with early observability features**. Metric-baseline
layer is what separates a DQ tool from a Monte Carlo — highest-leverage
next build.

---

## ML integration (2026-07-17)

Honest take: mostly **don't** — we're already deeply LLM-integrated
where it matters. Classical ML has narrow, well-scoped roles;
"add ML to the system" as a broad direction is a trap.

### LLM-shaped ML we already have

Claude drives RuleIntelligenceAgent, VerificationAgent,
FindingsExplanationAgent, FeedbackSynthesisAgent — proposal,
verification, RCA narrative, memory replay. That's the ML surface area
that pays off.

### Classical ML worth considering (narrow, ranked)

1. **Anomaly detection algorithms** — MAD in Tier A is *statistics*,
   not ML, deliberately: robust, interpretable, no training data.
   Upgrade to isolation forest / Prophet / LSTM only after real
   complaints about MAD's false-positive rate. Deferred.
2. **Column semantic-type classifier** — small scikit-learn model
   inferring purpose (email/phone/SSN/currency/timestamp/free-text/
   categorical) from *values*, not names. Would catch PII named
   `USR_STR_04`. ~500 lines. Only build after users report
   name-based misses.
3. **Duplicate-rule detection** — embedding clustering for the
   Maintenance-agent "superseded" branch. Same-target-and-shape
   heuristic gets 90% of the way; skip until real duplication seen.
4. **Threshold auto-tuning** — learn per-rule thresholds from history.
   Statistics again, comes free with Tier A metric snapshots.

### Do NOT build

- Neural PII classifier / NER for free-text — LLM already handles via
  draft_sql + sample rows. Duplicates effort, adds deployment burden.
- Learned rule proposer to replace Claude — Claude is better, we lack
  the labeled dataset that would make a custom model competitive.
- Per-table trained anomaly models — cold-start, opaque, per-table
  management. MAD + DoW covers 90% with zero training.
- Vector embeddings for RCA / feedback retrieval — memo replay works;
  embedding index adds ops surface area for marginal quality lift.

### Where LLM integration could deepen

- `RuleAuthoringAgent` for guided wizard (see chat-authoring section).
- Claude narrative on lineage RCA once ACCESS_HISTORY lineage lands.
- Structured Outputs / stricter tool-use JSON schemas per template
  shape → eliminate draft_sql repair-loop edge cases.

### The one honest ML gap

Column semantic-type classifier (item 2). Only classical-ML build
that's genuinely additive and not overlapping with LLM work.
Everything else labeled "ML" is either statistics we're already
planning, or Claude's job, or over-engineered.

### Recommendation

- Don't build a general "ML platform."
- Build semantic-type classifier only after users report PII misses.
- Everything else called "ML" (anomaly, threshold tuning, RCA) is
  statistics or LLM work already on the roadmap.

---

## Table health N+1 query fix + syntax error (2026-07-17, shipped)

Investigated why loading table health was slow. `get_table_health`
(`backend/app/api/table_health.py`) looped over every active rule
instance on the table and fired 3 separate Snowflake queries per
instance (`list_executions_for_instance`, `find_open_finding`,
`is_muted`) — N+1, up to ~90 round-trips for a 30-rule table. The fleet
overview endpoint next to it does the equivalent work in 3 aggregate
queries total, which is why only the per-table view was slow.

**Fixed:** added 3 batched `storage.py` helpers —
`list_executions_for_instances` (QUALIFY ROW_NUMBER partition per
instance), `find_open_findings`, `muted_instance_ids` — each a single
`IN (...)` query, and rewired the loop in `table_health.py` to read from
pre-fetched dicts. Down to ~3-4 queries regardless of table size.

**Also found (pre-existing, unrelated to the above):** the same file had
5 ternary expressions missing a space after `if` (e.g. `'Z'
ifoldest_by_table.get(key) else None`) — likely a prior automated
find/replace that appended `+ 'Z'` to timestamps and ate the space. This
meant the module couldn't even be imported. Fixed all 5; confirmed via
`ast.parse` that both `table_health.py` and `storage.py` compile.

## Profiling stats to add (2026-07-17, deferred — implement later)

AI suggested a longer list of profiling stats (percentiles, zero/
negative %, text min/max length + empty-string %, char-set detection,
missing-dates/time-gaps/arrival-frequency, data volume trends).

**Checked against RuleIntelligence safety first**: RuleIntelligence
never reads `profiling_service.py`'s raw per-column output directly —
it only consumes `column_stats` / `pk_shaped_candidates` /
`freshness_signals` / `closed_set_columns`, which `ProfilingAgent.
_derive_facts()` (`profiling_agent.py:140-182`) builds independently by
pulling exactly `total, nulls, null_pct, distinct, top_values,
tail_values` off each column. So adding new fields to the profile output
is purely additive — nothing does positional unpacking or "all keys"
iteration on the profile dict. Safe to add without touching the
2026-07-16 duplication-bug-fixed, regression-tested RuleIntelligence
path, as long as new fields aren't also wired into `_derive_facts`
(a separate, deliberate decision).

**Already covered, don't rebuild:** min/max, mean, stddev, an outlier
hint, top-N values, null%, unique%, duplicate count (id-category only
today), cardinality, freshness days, email/phone pattern-match %.

**Cheap to add** (fit into the existing batched aggregate query in
`column_stats()` — `snowflake_source.py` / `postgres_source.py` — no
extra round-trips):
- Median / percentiles (numeric). Open question: exact
  (`PERCENTILE_CONT`, full sort per column — real cost, and this table
  already hit a `NUMBER(38,0)` overflow profiling a 15.7M-row table) vs.
  approximate (`APPROX_PERCENTILE`, t-digest, no sort). Leaning
  approximate since percentiles are a display/context stat here, not a
  rule threshold — not decided, ask before implementing.
- Zero / negative-value % (numeric).
- Min/max length, empty-string % (text).
- Future-dates % (date).

**Bigger projects, not just an extra column** — fold into the
anomaly-detection Tier A work above rather than build standalone:
- Missing dates / time gaps / arrival frequency — needs calendar-gap
  logic, same shape as the batch-cadence gap noted in the rule-library
  audit.
- Character-set detection — needs its own classification pass.
- Data volume trends — needs `METRIC_SNAPSHOTS`, exactly Tier A above.

**Skip as standalone features** — the rest of the AI's list overlaps
what's already computed (see "already covered" above).
