# Rules System Architecture
> Status: Design document — approved direction before implementation begins.
> Supersedes the rules sections of architecture.md and mvp-scope.md where they differ.
> Last updated: 2026-07-07

---

## 1. Purpose of This Document

This document describes:
- What the current rules system already has (agents, skills, tools, storage)
- What is wrong with the current design and why
- The full target architecture: every layer, every component, every data entity, every lifecycle
- Explicit decisions on every ambiguity so implementation requires no revisits

This is the document to review and sign off before any code is written. Nothing in here is code.

---

## 2. What Exists Today

### 2.1 Agents

| Agent | File | What it does |
|---|---|---|
| Metadata Agent | `agents/metadata_agent.py` | Fetches table/column metadata from Snowflake INFORMATION_SCHEMA. Produces column list with data types, comments, nullability. |
| Profiling Agent | `agents/profiling_agent.py` | Runs statistical profiling on each column: null%, distinct count, min, max, top values, data type. Produces `column_profiles` and `table_profile` (row count). |
| PII Agent | `agents/pii_agent.py` | Two-tier classification — deterministic regex/heuristics first, Claude for ambiguous cases. Assigns `is_pii` and `llm_sharing_policy` (ALLOW_RAW / ALLOW_MASKED / ALLOW_STATS_ONLY) to each column. |
| Rule Recommendation Agent | `agents/rule_recommendation_agent.py` | Runs 6 deterministic skills to produce template rules, then calls Claude once per table to propose additional business/domain rules. Deduplicates, scores, applies feedback loop. |
| SQL Generation Agent | `agents/sql_generation_agent.py` | Ensures every rule has a `generated_sql`. Template-first via `render_sql_for_rule()`; Claude-sourced rules that already carry SQL pass through; unsupported rule types get `generated_sql=None`. |
| SQL Validation Agent | `agents/sql_validation_agent.py` | Runs every rule's SQL through the safety validator (SELECT-only, no forbidden keywords, allowed-table check). Sets `validation_status` VALID/INVALID. |
| Rule Test Execution Agent | `agents/rule_test_execution_agent.py` | Runs every VALID rule's SQL against source data before human approval. Records `test_status` (PASSED/FAILED/ERROR) and `test_result` including sample failed rows. |
| Rule Explanation Agent | `agents/rule_explanation_agent.py` | Calls Claude to produce `business_explanation`, `business_impact`, and `false_positive_risk` for each rule before it reaches the approval screen. |
| Rule Execution Agent | `agents/rule_execution_agent.py` | Runs one approved rule end-to-end: fetch → re-validate → execute → store history → alert if failed. The execution path after human approval. |
| Alert Agent | `agents/alert_agent.py` | Creates one ALERTS row on a FAILED rule execution. Calls Alert Explanation Agent for business-friendly text. Stores violation samples. |
| Alert Explanation Agent | `agents/alert_explanation_agent.py` | Claude call producing business_explanation/business_impact/false_positive_risk text for a failed alert. |

### 2.2 Skills (deterministic rule suggesters)

All skills are pure functions — no I/O, no SQL execution. They loop over `column_profiles` and return candidate rule dicts.

| Skill | File | What it suggests |
|---|---|---|
| Completeness | `skills/completeness_skill.py` | NOT NULL rules for columns whose name contains ID/DATE/AMOUNT/STATUS/CODE tokens and whose null% is already low. One rule per matching column. |
| Uniqueness | `skills/uniqueness_skill.py` | Duplicate-value checks for columns whose name suggests a unique key (ID tokens, low distinct-to-row ratio). |
| Validity | `skills/validity_skill.py` | Three sub-types: email format (EMAIL token), positive amount (AMOUNT/PRICE + numeric type), accepted values (STATUS token + few distinct values). |
| Freshness | `skills/freshness_skill.py` | Table-freshness check for columns named CREATED/UPDATED/LOAD/MODIFIED/REFRESHED that are actually DATE/TIMESTAMP type. Default threshold 24 hours. |
| Volume | `skills/volume_skill.py` | Table-level row count check. Uses static `> 0` rule if fewer than 3 prior scans exist; switches to historical-average deviation check once enough history is present. `column_name = NULL` — only truly table-level rule today. |
| Governance | `skills/governance_skill.py` | Structural/naming issues: date column stored as VARCHAR, boolean column stored as VARCHAR, key column stored as non-numeric type. |

### 2.3 Tools

| Tool | File | What it does |
|---|---|---|
| Rule Template Tools | `tools/rule_template_tools.py` | Single source of truth for all rule SQL. One function per check shape (completeness, uniqueness, accepted_values, positive_amount, freshness, email_format, volume, volume_historical, date_as_varchar, boolean_as_varchar, column_id_wrong_type). Also: `render_sql_for_rule()` dispatcher and `render_sample_sql_for_rule()` for failed-row sampling. |
| Claude Tools | `tools/claude_tools.py` | Bedrock/Claude calls: `recommend_rules_with_claude()` (rule suggestion), `classify_pii_with_claude()` (PII ambiguous cases), `explain_rule_with_claude()` (rule explanation), `explain_alert_with_claude()` (alert explanation). Includes `build_claude_input()` which assembles the full recommendation context including PII masking. |
| Storage Tools | `tools/storage_tools.py` | All reads/writes to the app-owned Snowflake DB. Covers: scan runs, profiles, recommended rules, approved rules, rejected rules, execution history, alerts, violation samples, feedback, agent logs, table health. |
| SQL Validation Tools | `tools/sql_validation_tools.py` | Safety validator: SELECT-only, no forbidden keywords, single statement, allowed-tables check. Uses sqlglot for parsing. |
| Snowflake Connection | `tools/snowflake_connection.py` | Source connection (SSO/external browser) and app-DB connection. Module-level caching. `run_query()` for source, `run_app_query()` for app DB. |
| Snowflake Metadata Tools | `tools/snowflake_metadata_tools.py` | `list_databases()`, `list_schemas()`, `list_tables()`, `describe_table()`. Safe identifier quoting via `_safe_identifier()`. |
| Snowflake Profiling Tools | `tools/snowflake_profiling_tools.py` | `profile_and_store_table()` — runs column-level stats queries and stores results. |
| Sample Query Tools | `tools/sample_query_tools.py` | `build_sample_failed_rows()` — runs the sample SQL for a rule on a real execution failure, respects PII masking policy per column. |
| PII Detection Tools | `tools/pii_detection_tools.py` | Deterministic tier for PII classification: regex patterns for email, phone, PAN, Aadhaar, names, financial identifiers. |

### 2.4 Orchestration

| File | Role |
|---|---|
| `graphs/dq_workflow_graph.py` | LangGraph StateGraph wiring all recommendation-flow agents in sequence. The production entry point. Each node catches exceptions and records errors without aborting the run. |
| `agents/scan_pipeline.py` | Older plain-Python entry point. Still exists but not the primary path. Can be removed. |
| `scan_operations.py` | Shared business logic: `recommend_rules_for_table()`, `recommend_rules_for_tables()`, `run_all_approved_rules()`. Used by both HTTP routes and the scheduler. |
| `scheduler.py` | APScheduler-based background jobs: periodic RESCAN and periodic RULE_EXECUTION. |
| `src/main.py` | FastAPI routes: all scan, rule, approval, execution, alert, health, and schedule endpoints. |

### 2.5 Current Storage Tables

```
CORE.SCAN_RUNS                   — every scan run (status, target, timing)
PROFILING.TABLE_PROFILES         — per-scan row counts and table-level stats
PROFILING.COLUMN_PROFILES        — per-scan per-column statistics
RULES.RECOMMENDED_RULES          — rules produced by the recommendation pipeline, awaiting human decision
RULES.APPROVED_RULES             — rules a human approved; the active execution set
RULES.REJECTED_RULES             — rules a human rejected, with optional reason
RULES.RULE_EXECUTION_HISTORY     — one row per approved rule per run (PASSED/FAILED/ERROR/SKIPPED)
RULES.USER_FEEDBACK              — REJECT/EDIT/FALSE_POSITIVE signals for the feedback loop
ALERTS.ALERTS                    — one alert per FAILED execution
ALERTS.ALERT_VIOLATION_SAMPLES   — sample failed rows for one alert
LOGS.AGENT_RUN_LOGS              — step-by-step agent activity log per scan
```

### 2.6 Key fields on current RECOMMENDED_RULES / APPROVED_RULES

Every rule row today: `rule_id, scan_id, rule_name, rule_type, database_name, schema_name, table_name, column_name (nullable), description, reason, evidence, severity, confidence, priority, threshold_config, generated_sql, validation_status, test_status, test_result, rule_fingerprint, business_explanation, business_impact, false_positive_risk`.

---

## 3. What Is Wrong With the Current Design

### 3.1 No rule library — Claude starts from zero every scan

Claude has no memory of what kinds of checks have already been established in the system. It reinvents the same patterns every time. There is no way to say "we already have a not-null check pattern — just extend it to these new columns."

### 3.2 Approved rules are invisible to Claude on re-scan

`get_pending_rule_fingerprints()` explicitly excludes approved rules. Claude cannot see what is already running. It re-proposes things that are already active. The pending queue fills with duplicates of live rules.

### 3.3 Rules are column-only, one check shape

Every rule row has one `column_name`. There is no structural support for multi-column checks, cross-table checks, table-level checks beyond volume, schema-level or database-level rules, or conditional rules.

### 3.4 No grouping — approval is always one rule at a time

A scan on a schema with 20 tables produces 100+ individual rules. Each requires a separate approval click. There is no concept of "approve all NOT_NULL rules for ID columns in this schema."

### 3.5 rule_fingerprint is not a real fingerprint

The field holds `"source:template"` or `"source:claude"` — a source tag, not an identity hash. Dedup in `scan_operations.py` uses a loose `(table, column, rule_type)` tuple which cannot distinguish two different VALIDITY rules on the same column.

### 3.6 No deactivation route

`APPROVED_RULES.IS_ACTIVE` exists and the execution agent respects it, but there is no API route to flip it. Once approved, a rule runs forever.

### 3.7 Error test status is silently collapsed

`rule_test_execution_agent.py` emits ERROR when a rule's SQL fails at execution time. `_TEST_STATUS_MAP` maps ERROR → FAILED before storage. "This rule's SQL is broken" looks identical to "this rule found real data violations" in the approval screen.

### 3.8 User feedback has ambiguous rule_id references

Feedback rows reference a `rule_id` which is sometimes a RECOMMENDED_RULES id and sometimes an APPROVED_RULES id. These are different ID namespaces. The feedback query works today only because it ignores `rule_id` entirely and matches by `(rule_type, column_name, table)` — but the stored ID is misleading.

---

## 4. Target Architecture

### 4.1 The Three Layers

```
LAYER 1 — RULE_DEFINITIONS  (the library: what a check means)
          A named, parameterized, reusable check pattern.
          Does not execute directly. Only instances execute.

LAYER 2 — RULE_INSTANCES    (approved, live, executable applications)
          "Apply Not Null to column CUSTOMER_ID on table ORDERS."
          This is the human-approved, executable unit.
          Physical table: RULES.RULE_INSTANCES  (replaces APPROVED_RULES)

          RECOMMENDED_INSTANCES  (candidate instances awaiting human decision)
          Physical table: RULES.RECOMMENDED_INSTANCES  (replaces RECOMMENDED_RULES)

LAYER 3 — EXECUTIONS + ALERTS  (runtime results, no design change)
          One row per instance per run. Alerts on FAILED.
```

**The core insight:** Layer 1 captures the *what* (the check concept). Layer 2 captures the *where and with what parameters*. Layer 3 captures the *when and what happened*. Today there is no Layer 1 — everything is crammed into Layer 2.

### 4.2 Physical Table Naming — The Definitive Answer

This is the decision that resolves the RULE_INSTANCES vs APPROVED_RULES question:

| Old table | New table | Fate |
|---|---|---|
| `RULES.RECOMMENDED_RULES` | `RULES.RECOMMENDED_INSTANCES` | Renamed. New columns added. |
| `RULES.APPROVED_RULES` | `RULES.RULE_INSTANCES` | Renamed. New columns added. Old name kept as compatibility view. |
| `RULES.REJECTED_RULES` | `RULES.REJECTED_INSTANCES` | Renamed. No structural change. |
| *(new)* | `RULES.RULE_DEFINITIONS` | Created. The library. |
| *(new)* | `RULES.RULE_GROUPS` | Created. Display/approval grouping only. |

Everywhere in this document, "instance" means a row in `RULE_INSTANCES` (approved) or `RECOMMENDED_INSTANCES` (pending). The word "rule" alone is ambiguous and is avoided in new code and documentation.

### 4.3 Layer 1: Rule Definitions

A Rule Definition is a named, parameterized check pattern. It owns the *concept* of a check. It does not store any target, any scope binding, or any executable SQL directly — only a SQL template with named placeholders.

**Fields:**

| Field | Type | Purpose |
|---|---|---|
| definition_id | STRING | Stable identity |
| name | STRING | Human-readable name: "Not Null Check", "Email Format Check" |
| category | STRING | Display/filter grouping: COMPLETENESS / UNIQUENESS / VALIDITY / FRESHNESS / VOLUME / GOVERNANCE / CUSTOM. This is a label only — never used for SQL dispatch. |
| description | STRING | What this check catches, in plain English |
| check_logic | STRING | Prose description of the check logic — what the SQL tests and why |
| parameters_schema | VARIANT | JSON Schema for the threshold_config this definition accepts |
| default_threshold_config | VARIANT | Default parameter values (can be overridden per instance) |
| default_severity | STRING | WARNING / CRITICAL / INFO — overridable per instance |
| allowed_scopes | VARIANT | JSON array of which instance scopes this definition supports. Only COLUMN, MULTI_COLUMN, TABLE, CROSS_TABLE, CONDITIONAL are valid. See §4.4 on why SCHEMA and DATABASE are absent. |
| sql_template | STRING | Parameterized SQL template with `{database}`, `{schema}`, `{table}`, `{target.*}`, `{params.*}` placeholders. NULL for CUSTOM definitions where Claude provides draft SQL per instance. |
| source | STRING | SYSTEM / CLAUDE / USER |
| status | STRING | See §4.3.1 for the full lifecycle |
| instance_count | NUMBER | Count of live RULE_INSTANCES using this definition. Maintained by code, not a join. |
| approval_count | NUMBER | How many times instances of this definition have been approved. Used to rank definitions in Claude's context — frequently approved definitions appear first. |
| created_at | TIMESTAMP | Creation time |
| created_by | STRING | NULL for SYSTEM-seeded definitions |

#### 4.3.1 Definition Status Lifecycle

A definition has exactly four statuses:

| Status | Meaning | Who sets it |
|---|---|---|
| PROPOSED | Claude suggested this definition during a scan. It exists only as `proposed_definition` JSON on a RECOMMENDED_INSTANCES row — it is NOT yet a row in RULE_DEFINITIONS. | Set by the recommendation agent |
| ACTIVE | The definition exists as a row in RULE_DEFINITIONS with status=ACTIVE. This happens the moment a human approves any instance that uses this definition. ACTIVE means the system will propose new instances of this definition on future scans. | Set on first instance approval |
| DISABLED | A human explicitly disabled this definition. No new instances will be suggested from it on future scans. Existing live instances continue executing. | Set by human via API |
| *(no REJECTED status)* | There is no "rejected definition" status. Rejecting every instance of a definition does not automatically affect the definition. To prevent future suggestions, a human must explicitly DISABLE the definition. This distinction matters: an instance rejection is about a specific target ("don't check CUSTOMER_ID here"); a definition disable is about the concept ("never suggest this kind of check again"). | N/A |

**Status transition diagram:**
```
PROPOSED (on RECOMMENDED_INSTANCES only, no row in RULE_DEFINITIONS)
    |
    | human approves any instance using this definition
    ↓
ACTIVE (row in RULE_DEFINITIONS)
    |
    | human explicitly disables via API
    ↓
DISABLED
    |
    | human re-enables via API
    ↑
ACTIVE
```

**Important:** A PROPOSED definition only graduates to RULE_DEFINITIONS on instance approval. If every instance of a PROPOSED definition is rejected, the definition is never created in RULE_DEFINITIONS and leaves no trace. This is correct — a rejected suggestion should not pollute the library.

#### 4.3.2 System Definitions Seeded at Startup

Each current rule sub-type becomes one named system definition. The `category` field carries the old `rule_type` as a display/filter label. SQL dispatch is entirely driven by `sql_template` + the instance's `scope` and `target_config` — never by `category` alone.

| Definition Name | Category | Allowed Scopes |
|---|---|---|
| Not Null Check | COMPLETENESS | COLUMN, TABLE |
| Unique Values | UNIQUENESS | COLUMN, MULTI_COLUMN |
| Email Format | VALIDITY | COLUMN |
| Positive Amount | VALIDITY | COLUMN |
| Accepted Values | VALIDITY | COLUMN |
| Updated Within N Hours | FRESHNESS | COLUMN, TABLE |
| Row Count Above Zero | VOLUME | TABLE |
| Row Count Within Historical Band | VOLUME | TABLE |
| Date Stored As Varchar | GOVERNANCE | COLUMN |
| Boolean Stored As Varchar | GOVERNANCE | COLUMN |
| Key Column Wrong Type | GOVERNANCE | COLUMN |

System definitions are immutable. Their `sql_template`, `allowed_scopes`, and `parameters_schema` cannot be changed. Their `default_threshold_config` and `default_severity` can be changed by an admin.

### 4.4 Instance Scopes — The Definitive List

**Instances support exactly five scopes:**

| Scope | Meaning | What target_config holds |
|---|---|---|
| COLUMN | One check on one column of one table | `{"column": "CUSTOMER_ID"}` |
| MULTI_COLUMN | One check across multiple columns of one table | `{"columns": ["START_DATE", "END_DATE"]}` |
| TABLE | One check on a whole table, no specific column | `{}` (empty — the table is identified by database/schema/table) |
| CROSS_TABLE | Check involving a column in this table and a column in another table | `{"column": "CUSTOMER_ID", "ref_database": "DB", "ref_schema": "SCH", "ref_table": "CUSTOMERS", "ref_column": "ID"}` |
| CONDITIONAL | Check that applies only when another column meets a condition | `{"column": "SHIPPED_DATE", "when_column": "STATUS", "when_operator": "=", "when_value": "SHIPPED"}` |

**SCHEMA and DATABASE are NOT instance scopes.** They are group-level concepts (see §4.6). This distinction is critical:

- An instance is always a directly executable, single-SQL check against a specific database/schema/table combination.
- A schema-wide or database-wide concern is expressed as a RULE_GROUP whose members are individual TABLE or COLUMN instances. The group is what the human approves as a unit. Each member instance executes independently.
- Implementers must never create an instance row with scope=SCHEMA or scope=DATABASE. The API must reject such attempts.

### 4.5 The Flexible Target Model

Instead of many separate nullable fields (`column_name`, `secondary_columns`, `cross_table_refs`, `condition_config`), every instance uses a single `target_config VARIANT` field together with `scope`. This avoids a growing list of nullable columns as new scopes are added.

The `scope` field tells you how to interpret `target_config`. The shapes are defined in §4.4 above. Queries that need to filter by column name use Snowflake's VARIANT access: `target_config:column::STRING`. This is idiomatic for Snowflake and cleaner than nullable scalar columns.

`database_name`, `schema_name`, and `table_name` remain as top-level scalar columns on both RECOMMENDED_INSTANCES and RULE_INSTANCES because they are indexed and used in every WHERE clause. Only the within-table targeting information moves into `target_config`.

### 4.6 Rule Groups

A Rule Group is purely an approval and display convenience. **It has no effect on execution.** It answers: "these 8 NOT_NULL column-scope instances all came from the same recommendation — let the human review and decide on them as a unit."

**Fields:** group_id, name, description, definition_id (which definition all members share), scope_level (TABLE or SCHEMA or DATABASE — the level at which the group was conceptually identified), database_name, schema_name, table_name (nullable), created_at.

**How groups are created:** The recommendation agent suggests a group name when it finds multiple instances of the same definition that belong together logically. The human sees the group header in the approval screen and can act on the whole group or expand and decide per instance.

**Group edge cases — all explicitly decided:**

| Situation | Behavior |
|---|---|
| Human approves 6 of 8 instances in a group, rejects 2 | Fully supported. Each instance is decided independently. The group_id on the 6 approved instances persists. The 2 rejected instances become REJECTED_INSTANCES with the same rejection reason if bulk-rejected, or individual reasons if rejected one at a time. The group is not "partial" — it simply has some approved and some rejected members. This is normal. |
| Human edits the threshold on one instance before approving it | The edit applies only to that instance. Other instances in the group are unaffected. The group's display shows the edited threshold for that instance. USER_FEEDBACK(EDIT) is written for that instance only. |
| Human approves all instances in a group, then later deactivates one | Deactivation acts on the instance directly (IS_ACTIVE=FALSE on RULE_INSTANCES). The group is not affected. The deactivated instance is skipped at execution time. |
| Bulk approve group action | Equivalent to approving each PENDING instance individually in sequence. Any instance that fails validation (no valid SQL) is skipped with an error; the others proceed. The action is not all-or-nothing. |
| A group has instances from multiple tables | Allowed. scope_level=SCHEMA groups will have instances across multiple tables. The group_id links them. |
| Two scans both suggest the same group | Fingerprint dedup handles this. Matching instances get refreshed (not duplicated). The group_id on RECOMMENDED_INSTANCES is matched by name + definition_id + scope_level + schema. |

### 4.7 Recommended Instances

`RECOMMENDED_INSTANCES` (replacing `RECOMMENDED_RULES`) carries all the same fields as today plus:

| New field | Type | Purpose |
|---|---|---|
| scope | STRING | COLUMN / MULTI_COLUMN / TABLE / CROSS_TABLE / CONDITIONAL |
| target_config | VARIANT | Flexible target (see §4.5) |
| definition_id | STRING | FK to RULE_DEFINITIONS — NULL only if proposing a new definition |
| is_new_definition | BOOLEAN | TRUE when this instance would create a new definition |
| proposed_definition | VARIANT | Full definition dict when is_new_definition=TRUE. This is the staged PROPOSED state (§4.3.1) — it does not enter RULE_DEFINITIONS until the instance is approved. |
| suggested_group_id | STRING | FK to a RULE_GROUPS row — set when agent groups multiple instances |
| rule_fingerprint | STRING | Real identity hash: sha256(definition_id + scope + database_name + schema_name + table_name + canonical_json(target_config) + canonical_json(threshold_config)) |

`column_name` is removed from the schema. It is replaced by `target_config:column::STRING` for COLUMN-scope instances. All existing queries that filter on column_name must migrate to VARIANT access.

**Fingerprint dedup behavior (in priority order):**
1. Fingerprint matches an active RULE_INSTANCES row → **skip entirely**. Already running.
2. Fingerprint matches a PENDING RECOMMENDED_INSTANCES row → **refresh**. Update analysis fields (description, reason, evidence, confidence, priority, test_result, explanations). Preserve any human-edited fields (severity, threshold_config if the human edited them). Do not reset approval_status.
3. Fingerprint matches a REJECTED_INSTANCES row → **skip**. Respect the rejection. Exception: if the current scan has meaningfully new evidence (confidence > prior + 0.2 and evidence list is non-empty), re-propose with a note that new evidence was found.
4. No match → **insert new**.

### 4.8 Rule Instances (the live, approved table)

`RULE_INSTANCES` (replacing `APPROVED_RULES`) carries:

| Field | Type | Notes |
|---|---|---|
| instance_id | STRING | Primary key |
| original_recommended_id | STRING | FK to RECOMMENDED_INSTANCES |
| definition_id | STRING | FK to RULE_DEFINITIONS — always set |
| scope | STRING | COLUMN / MULTI_COLUMN / TABLE / CROSS_TABLE / CONDITIONAL |
| database_name | STRING | Top-level scalar for indexing |
| schema_name | STRING | Top-level scalar for indexing |
| table_name | STRING | Top-level scalar for indexing |
| target_config | VARIANT | Flexible target (see §4.5) |
| threshold_config | VARIANT | Parameter values for this instance |
| severity | STRING | CRITICAL / WARNING / INFO |
| rule_sql | STRING | The validated, executable SQL — never Claude's raw draft |
| group_id | STRING | FK to RULE_GROUPS (optional) |
| is_active | BOOLEAN | FALSE = skipped at execution, not deleted |
| approved_at | TIMESTAMP | |
| approved_by | STRING | |
| schedule_config | VARIANT | Future: scheduled execution config |

**The rule_sql field contains only validated SQL.** See §5.3 on what "validated" means and why Claude's draft SQL is never stored here directly.

### 4.9 Feedback Model

`USER_FEEDBACK` gains three clarifying fields and a strictly defined matching key.

**New fields:**

| Field | Purpose |
|---|---|
| source_type | RECOMMENDED_INSTANCE / RULE_INSTANCE / ALERT / DEFINITION — which table the `source_id` references |
| source_id | The actual ID being referenced (recommended_id, instance_id, alert_id, or definition_id depending on source_type) |
| definition_id | Denormalized copy of the definition_id for the check involved. Always set, regardless of source_type. Enables definition-level suppression without a join. |
| scope | The scope of the instance that produced this feedback. Stored denormalized for the same reason. |

**The feedback suppression query — exactly defined:**

When the recommendation agent loads feedback for a table, it runs one query:

```
SELECT feedback_type, definition_id, scope, target_config, threshold_config, comment
FROM RULES.USER_FEEDBACK
WHERE database_name = X AND schema_name = Y AND table_name = Z
ORDER BY created_at DESC
```

From this result, the agent builds three lookup sets:

1. **Suppressed instances:** all rows with feedback_type=REJECT, keyed by (definition_id + scope + canonical_json(target_config)). Any instance whose fingerprint matches a key in this set is dropped from the recommendation. The rejection reason (comment) is included in Claude's context so Claude understands why.

2. **Priority-halved instances:** all rows with feedback_type=FALSE_POSITIVE, same key. Matching candidates have their priority multiplied by 0.5.

3. **Threshold seeds:** all rows with feedback_type=EDIT, keyed by (definition_id + scope + canonical_json(target_config)), most recent only. Matching candidates use the stored threshold_config as their starting point instead of the definition's default.

No other query shapes are needed. Feedback matching is always by (definition_id + scope + target_config_hash), never by rule name or SQL text.

---

## 5. The Full Recommendation Pipeline

### 5.1 Stage 1: Metadata Discovery (unchanged)

Same `metadata_agent.py`. Produces column list, data types, comments.

### 5.2 Stage 2: Profiling (one addition)

Same `profiling_agent.py`. Produces column statistics and table row count.

**Addition:** Query `INFORMATION_SCHEMA.KEY_COLUMN_USAGE` and `REFERENTIAL_CONSTRAINTS` for any declared FK relationships. Also infer likely FK columns from name patterns (`*_ID` columns that match another table's name). Store as `inferred_relationships` in the scan context. This enables CROSS_TABLE instance suggestions.

### 5.3 Stage 3: PII Classification (unchanged)

Same `pii_agent.py`. Assigns `is_pii` and `llm_sharing_policy` per column.

### 5.4 Stage 4: Rule Recommendation Agent

The recommendation agent has two sub-tasks: library-aware instance suggestion and new-definition proposal.

**What the agent receives (full context):**

| Input | Source | Purpose |
|---|---|---|
| column_profiles (PII-masked) | profiling_agent | Column stats |
| table_profile | profiling_agent | Row count and history |
| table_classification | Claude (existing) | fact/dimension/staging/etc |
| inferred_relationships | INFORMATION_SCHEMA | Enables CROSS_TABLE suggestions |
| rule_definition_library | RULE_DEFINITIONS | All ACTIVE definitions, ordered by approval_count DESC. This is what Claude works from. |
| existing_approved_instances | RULE_INSTANCES | What is already running on this target. Claude must not re-propose these. |
| existing_pending_instances | RECOMMENDED_INSTANCES | What is awaiting human review. Claude must not re-propose these. |
| rejected_instances_with_reasons | REJECTED_INSTANCES + USER_FEEDBACK | What was rejected and the human's stated reasons. Claude must not re-propose rejected concepts. |
| feedback_signals | USER_FEEDBACK | EDIT threshold seeds and FALSE_POSITIVE priority signals. |

**Deterministic skills run first (unchanged):**

All 6 skills (completeness, uniqueness, validity, freshness, volume, governance) run exactly as today. Their output is `template_suggestions`. These are matched to existing definitions by (category + check shape) before Claude is called, so Claude sees them as "already found" and does not re-suggest them.

**Claude's job (new system prompt direction):**

Claude receives all of the above and is instructed to:
1. Look at the definition library first. For each definition that applies to this table but has no active or pending instance yet, propose a new instance.
2. Identify groups: when the same definition applies to multiple columns or tables, propose them with a shared suggested_group_name so the human can review them as a unit.
3. Propose new definitions only when a check concept is genuinely absent from the library. Do not re-invent what already exists.
4. Never propose anything that is already running, already pending, or was explicitly rejected.

**Claude's structured output contains two parts:**

```
new_definitions:
  - One entry per check concept with no matching existing definition
  - Fields: name, category, description, check_logic, allowed_scopes,
    default_severity, draft_sql_template (or null)
  - Claude labels this output as DRAFT — it is never directly executable

instance_suggestions:
  - One entry per specific application Claude recommends
  - Fields: definition_id (existing) OR new_definition_index (into new_definitions above),
    scope, target_config, threshold_config, severity, confidence, reason, evidence,
    draft_generated_sql (for CUSTOM/new definitions only — see §5.5),
    suggested_group_name (optional)
```

**Agent post-processing (code, not Claude):**

1. Match each `new_definition` against existing RULE_DEFINITIONS by (category + check_logic similarity). If a match is found, map to the existing definition. If genuinely new, stage as PROPOSED.
2. Merge `template_suggestions` + `instance_suggestions`. Deduplicate on fingerprint.
3. Compute priority from code: `confidence × severity_weight`. Never use Claude's priority value.
4. Apply feedback suppression (§4.9).
5. Group instances by `suggested_group_name`. Create RULE_GROUPS rows for new group names.
6. Return: `{new_definitions, recommended_instances, groups, claude_error}`

### 5.5 Claude SQL Is Always a Draft — The SQL Trust Chain

This is a hard rule that must never be violated:

**Claude never produces directly executable SQL.** When Claude provides SQL (for new definition templates or for CUSTOM instance suggestions), that SQL is stored as `draft_sql` on the RECOMMENDED_INSTANCES row. It is not stored as `rule_sql` on RULE_INSTANCES.

The SQL trust chain is:

```
Source                         Trust level    Stored as
─────────────────────────────────────────────────────────
System sql_template +          TRUSTED        Generated by code at SQL Generation stage
  rendered with code

Claude draft_sql               UNTRUSTED      Stored as draft_sql on RECOMMENDED_INSTANCES
  (new definition or CUSTOM)   DRAFT ONLY     only. Never directly becomes rule_sql.
                                              Must pass SQL Generation + Validation first.

Human-edited SQL               CONDITIONALLY  Must re-run SQL Validation before
  (from approval screen edit)  TRUSTED        becoming rule_sql on RULE_INSTANCES.
```

**At the SQL Generation stage (§5.6):**
- If the instance has a system definition with a `sql_template`: render the template with code. Use this as `generated_sql`. Ignore any `draft_sql` from Claude.
- If the instance has a CUSTOM/new definition with no `sql_template`: use Claude's `draft_sql` as a starting point, pass it through the SQL Validation gate. Only if it passes does it become `generated_sql`.
- In both cases: `generated_sql` on `RECOMMENDED_INSTANCES` is what the human sees in the approval screen. `rule_sql` on `RULE_INSTANCES` is set only at the moment of approval from the validated `generated_sql`.

The approval screen must visually distinguish: "SQL from template (validated)" vs "SQL from Claude draft (validated)" vs "SQL from Claude draft (not yet validated)" vs "SQL edited by human (validated)" so the reviewer understands what they are approving.

### 5.6 Stage 5: SQL Generation (expanded)

`render_sql_for_rule()` becomes `render_sql_for_instance()` dispatching on definition + scope + target_config:

| Scope | SQL generation approach |
|---|---|
| COLUMN | Existing templates, unchanged. Column name read from `target_config:column`. |
| MULTI_COLUMN | Multi-column predicate in one SELECT. Column names from `target_config:columns`. |
| TABLE | True table-level aggregate. No column reference. |
| CROSS_TABLE | JOIN-based SELECT. Primary table + ref table from `target_config`. |
| CONDITIONAL | `COUNT_IF(when_condition AND NOT check_condition)` shape. All fields from `target_config`. |

For CUSTOM definitions with `draft_sql`: pass the draft through SQL Validation. If it passes, use it as `generated_sql`. If it fails, store `generated_sql=NULL` and `validation_status=FAILED` with the errors visible to the human.

### 5.7 Stage 6: SQL Validation (minor expansion)

Same `sql_validation_agent.py` core logic — SELECT-only, no forbidden keywords, single statement, allowed-tables check.

One change: for CROSS_TABLE scope, `allowed_tables` is expanded to include both the primary table and all tables referenced in `target_config` (the ref_database/ref_schema/ref_table fields).

### 5.8 Stage 7: Rule Test Execution (one fix)

Same `rule_test_execution_agent.py`. Runs every instance with validation_status=PASSED.

**Fix:** ERROR is stored as ERROR, not mapped to FAILED. In the approval screen, ERROR means "the SQL itself failed to execute" (broken query, timeout, missing table). FAILED means "the query ran and found violations." These are different problems and must be visually distinct.

### 5.9 Stage 8: Rule Explanation Agent (unchanged)

Same `rule_explanation_agent.py`. Produces business_explanation, business_impact, false_positive_risk per instance.

### 5.10 Stage 9: Persist Recommendations

Fingerprint-based dedup as defined in §4.7. Then:
- For instances with `is_new_definition=TRUE`: store `proposed_definition` as a VARIANT field on the RECOMMENDED_INSTANCES row. Do not yet insert into RULE_DEFINITIONS. The definition only enters RULE_DEFINITIONS when the human approves.
- For instances with `suggested_group_id`: create or match the RULE_GROUPS row first, then store the group_id on the RECOMMENDED_INSTANCES row.

---

## 6. Human Approval

### 6.1 Individual instance actions

**Approve:** RECOMMENDED_INSTANCES (PENDING) → RULE_INSTANCES (active).
- If `is_new_definition=TRUE`: first insert definition into RULE_DEFINITIONS with status=ACTIVE using the `proposed_definition` data. Then insert the RULE_INSTANCES row with the new definition_id.
- The `rule_sql` on the new RULE_INSTANCES row is taken from `generated_sql` on RECOMMENDED_INSTANCES — this is already validated SQL, never Claude's raw draft.
- Increment `definition.approval_count` by 1.

**Reject:** RECOMMENDED_INSTANCES → REJECTED_INSTANCES. USER_FEEDBACK(REJECT, source_type=RECOMMENDED_INSTANCE) written with optional reason.

**Edit then approve:** Human modifies severity, threshold_config, or generated_sql on a PENDING instance.
- Any change to `generated_sql` triggers re-validation before the edit is saved.
- Any change to `threshold_config` writes USER_FEEDBACK(EDIT) so future recommendations seed from this value.
- After editing, the instance remains PENDING until explicitly approved.

### 6.2 Bulk group actions

**Bulk approve group:** For each PENDING instance in the group with validation_status=PASSED, run the individual approve logic. Instances with validation_status=FAILED are skipped and noted in the response — the human must edit them first. This is not all-or-nothing.

**Bulk reject group:** For each PENDING instance in the group, run the individual reject logic with the same shared reason. If some instances were already individually approved, they are not affected — bulk reject only touches PENDING instances.

### 6.3 Active instance management

**Deactivate:** Flip IS_ACTIVE=FALSE on RULE_INSTANCES. No deletion. The instance is skipped at execution time (SKIPPED status in history). The group it belongs to is not affected.

**Reactivate:** Flip IS_ACTIVE=TRUE.

**Disable definition:** Flip status=DISABLED on RULE_DEFINITIONS. Existing live RULE_INSTANCES continue executing. Future scans will not propose new instances of this definition. Definition can be re-enabled.

### 6.4 Approval screen layout

Instances are shown in two views:
- **Grouped view:** Group header (definition name, scope_level, instance count, aggregate confidence). Expand to see individual instances. Bulk approve/reject button on the header.
- **Ungrouped view:** Individual instances exactly as today for instances with no group.

The SQL shown for each instance is labeled with its trust level: "Template SQL" / "Claude draft (validated)" / "Human-edited SQL". ERROR test status is shown as "SQL execution failed" distinct from FAILED "violations found."

---

## 7. Rule Execution (minimal change)

`run_rule_execution_agent()` reads from `RULE_INSTANCES` instead of `APPROVED_RULES`. All execution behavior is unchanged. `RULE_EXECUTION_HISTORY` and `ALERTS` each gain an `instance_id` column alongside the retained `rule_id` for backward compatibility.

Auto-resolve on PASSED, alert creation on FAILED, SKIPPED for inactive/invalid — all unchanged.

---

## 8. What the System Learns Over Time

| Signal | How it is used next scan |
|---|---|
| REJECT instance | That (definition_id + scope + target_config) is suppressed. Rejection reason shown to Claude. |
| REJECT all instances of a definition | The definition is not automatically disabled. A human must explicitly DISABLE it if they want no future suggestions. |
| EDIT threshold | Future recommendations of same definition + target seed from the human's value. |
| FALSE_POSITIVE alert | Future recommendations of same definition + target have halved priority. |
| APPROVE instance | definition.approval_count increments. Higher-count definitions rank higher in Claude's library context. |
| New definition approved | Enters RULE_DEFINITIONS as ACTIVE. Proposed on future scans across all relevant targets. |

---

## 9. Supported Rule Types

| Rule Type | Example | Scope |
|---|---|---|
| Single column | CUSTOMER_ID must not be null | COLUMN |
| Multi-column | START_DATE must be before END_DATE | MULTI_COLUMN |
| Conditional column | If STATUS = SHIPPED then SHIPPED_DATE must not be null | CONDITIONAL |
| Cross-table referential | ORDERS.CUSTOMER_ID must exist in CUSTOMERS.ID | CROSS_TABLE |
| Table-level aggregate | Row count must stay within 30% of 30-day average | TABLE |
| Table freshness | Table must have been updated within 24 hours | TABLE |
| Schema-wide pattern | All ID columns in schema must not be null | SCHEMA-level GROUP of COLUMN instances |
| Database-wide governance | All date-named columns must use DATE/TIMESTAMP types | DATABASE-level GROUP of COLUMN instances |
| Distribution check | CUSTOMER_SEGMENT distribution within 10% of historical | TABLE / CUSTOM |
| Custom domain rule | TRANSACTION_AMOUNT matches SUM of LINE_ITEMS | CROSS_TABLE / CUSTOM |

---

## 10. What Stays Unchanged

- `metadata_agent.py`
- `profiling_agent.py` (optional FK query addition)
- `pii_agent.py`
- `sql_validation_agent.py` (one change: expanded allowed_tables for CROSS_TABLE)
- `rule_test_execution_agent.py` (one change: ERROR stored as ERROR not FAILED)
- `rule_execution_agent.py` (reads RULE_INSTANCES instead of APPROVED_RULES)
- `alert_agent.py` (writes instance_id alongside rule_id)
- `alert_explanation_agent.py`
- `rule_explanation_agent.py`
- `dq_workflow_graph.py` (wiring unchanged, state shape gets minor additions)
- All existing API route URLs (new routes added, nothing removed or renamed)

---

## 11. What Is Built New

| Component | Type | Purpose |
|---|---|---|
| `RULES.RULE_DEFINITIONS` | New table | The rule library |
| `RULES.RULE_GROUPS` | New table | Display/approval grouping |
| `RULES.RECOMMENDED_INSTANCES` | Renamed + new columns | Replaces RECOMMENDED_RULES |
| `RULES.RULE_INSTANCES` | Renamed + new columns | Replaces APPROVED_RULES |
| `RULES.REJECTED_INSTANCES` | Renamed | Replaces REJECTED_RULES |
| Seed data for RULE_DEFINITIONS | Data | All existing check types as SYSTEM definitions |
| `storage_tools.py` additions | Code | CRUD for definitions, groups, instances |
| `rule_recommendation_agent.py` | Rework | Library-aware context, new output schema, real fingerprint |
| `claude_tools.py` | Update | New system prompt, new structured output schema |
| `render_sql_for_instance()` | New function | Handles all 5 scopes |
| `scan_operations.py` | Update | Real fingerprint hash, definition_id on store |
| Bulk approve/reject | API routes | `POST /api/rules/bulk-approve`, `POST /api/rules/bulk-reject` |
| Deactivate/activate | API routes | `PATCH /api/rules/{id}/deactivate`, `PATCH /api/rules/{id}/activate` |
| Definitions CRUD | API routes | `GET/POST/PATCH /api/rules/definitions` |
| Groups read | API routes | `GET /api/rules/groups`, `GET /api/rules/groups/{id}/instances` |
| Approval screen group UI | Frontend | Group header with bulk actions, instance expand |

---

## 12. Explicitly Out of Scope

- True single-SQL cross-schema or cross-database aggregation (SCHEMA/DATABASE = group of instances only)
- Rule versioning history
- Scheduled per-instance execution
- Business glossary integration
- Query usage pattern analysis
- LLM fine-tuning from feedback
- Multi-step approval workflows
- RBAC and audit logs
- Slack / PagerDuty / email routing
