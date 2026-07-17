# Rule Architecture — Correctness Fixes

This document records the correctness and robustness fixes made to the rule
pipeline (`RuleIntelligenceAgent` → `WorkflowCoordinator` → `FindingsAgent`,
plus the frontend review panel) and the reasoning behind each. Most were
latent bugs that produced *silently wrong* results rather than loud failures,
which is why they warranted fixing even though the pipeline "worked" day to
day.

| # | Issue | File(s) | Severity |
|---|-------|---------|----------|
| 1 | Severity override leaked / raced on the shared instance row | `findings_agent.py` | High |
| 2 | Default Cortex path ran without the system prompt | `rule_intelligence_agent.py` | High |
| 3 | Unaddressed deterministic signals were invisible to the reviewer | `coordinator.py`, `AgentWorkflow.tsx`, `client.ts` | Medium |
| 4 | No cost guard on AI-authored `draft_sql` | `rule_intelligence_agent.py`, `snowflake_session.py` | Medium |
| 5 | `_word_overlap_score` duplicated with drifting stopword lists | `text_similarity.py` (new), `rule_intelligence_agent.py`, `rules.py` | Low |
| 6 | Dead-ahead `'*'`-scoped `sql_template` branch could run wrong SQL | `rule_engine.py` | Low |
| 7 | LLM parse-failure indistinguishable from "no proposals" | `rule_intelligence_agent.py`, `coordinator.py`, `AgentWorkflow.tsx`, `client.ts` | Medium |
| 8 | Relationship discovery trusted implicit cross-type joins | `relationship_discovery.py` | High |

---

## Fix #1 — Severity overrides no longer mutate the shared instance row

### The bug

`FindingsAgent.run` applied per-run severity overrides by **writing the new
severity onto the persisted `RULE_INSTANCES` row**, running the checks, then
restoring the original value afterward:

```python
self._apply_severity_overrides(severity_overrides)      # UPDATE RULE_INSTANCES ...
findings_data = self.rule_engine.execute_all_rules(...)  # if this raises...
self._restore_severity_overrides(severity_overrides)     # ...this never runs
```

Two failure modes fell out of this:

1. **Leak on failure.** The restore call sat *after* `execute_all_rules`, not
   in a `finally`. If execution raised partway through (e.g. a dropped
   Snowflake connection), the instance was left **permanently** at the
   overridden severity for every future scan.

2. **Cross-thread race.** Batch tables advance in parallel daemon threads
   (`WorkflowCoordinator._advance_batch`). Global (`'*'`-scoped) instances are
   shared across tables. While Thread A held its temporary override on a shared
   instance, Thread B could read that instance mid-scan and label its findings
   with the wrong severity.

#### Example

A shared `uniqueness` instance is normally `high`. A reviewer bumps it to
`critical` for one run.

- `_apply` writes `critical` to the row.
- `execute_all_rules` throws on table #3.
- `_restore` never runs → the instance stays `critical` forever, silently, for
  every table and every future scan.

### The fix

Overrides are now applied **in memory to the produced finding dicts**, keyed by
`instance_id`. The shared row is never touched, so there is nothing to restore
and nothing a concurrent run can observe.

```python
findings_data = self.rule_engine.execute_all_rules(...)
self._apply_severity_overrides(findings_data, severity_overrides)
```

```python
def _apply_severity_overrides(self, findings_data, overrides):
    if not overrides:
        return
    for fd in findings_data:
        override = overrides.get(fd.get("instance_id"))
        if override:
            fd["severity"] = override
```

The old `_restore_severity_overrides` method and the `_severity_backup`
attribute were removed — they no longer have any purpose.

This is safe because every finding dict already carries both `instance_id` and
`severity` (see `RuleEngine._execute_sql_instance` / `_execute_instance`), so
the override map can be applied directly without a DB round-trip.

---

## Fix #2 — Cortex path now receives the system prompt

### The bug

`RuleIntelligenceAgent._call_model` tries Snowflake Cortex first and falls back
to Bedrock only on exception:

```python
def _call_model(self, prompt: str) -> str:
    try:
        return sf_session.ask_cortex(prompt, model="claude-opus-4-8")   # no system
    except Exception as e:
        logger.warning(...)
    return ask_claude(prompt, system=SYSTEM_PROMPT, max_tokens=4096)      # has system
```

`ask_cortex` has **no separate system-prompt channel** — it just wraps the
string in `SNOWFLAKE.CORTEX.COMPLETE(...)`. So on the normal (green) path, the
model ran with **none** of the rules in `SYSTEM_PROMPT`:

- "Respond with valid JSON only."
- "Never invent a relationship not in the given list."
- "Every accepted-value must come from the observed set."
- "Every signal must get an entry in `signals_evaluated`."

The emphatic, constrained behavior only kicked in when Cortex *failed* and
Bedrock took over — i.e. the fallback was better-behaved than the primary, and
every successful run was the degraded one.

#### Example

Cortex succeeds (the common case). The model receives only the user prompt (the
data + the JSON skeleton) and is free to wrap its answer in markdown, invent a
`ref_table`, or skip signals — none of which the user prompt forbids on its own.

### The fix

The system prompt is prepended to the Cortex prompt so both paths are
equivalent:

```python
def _call_model(self, prompt: str) -> str:
    try:
        cortex_prompt = f"{SYSTEM_PROMPT}\n\n{prompt}"
        return sf_session.ask_cortex(cortex_prompt, model="claude-opus-4-8")
    except Exception as e:
        logger.warning(f"[RuleIntelligence] Cortex failed ({e}), using Claude/Bedrock")
    return ask_claude(prompt, system=SYSTEM_PROMPT, max_tokens=4096)
```

---

## Fix #3 — Unaddressed signals are now surfaced to the reviewer

### The bug

The profiler emits **deterministic signals** (PK-shaped-column uniqueness,
timestamp freshness). The system prompt requires Claude to acknowledge every
signal in `signals_evaluated`. `RuleIntelligenceAgent` computes which signals
Claude ignored:

```python
signals_missed = sorted(expected_signal_ids - set(classification["signals_evaluated"].keys()))
if signals_missed:
    logger.info(f"[RuleIntelligence] Signals not addressed by Claude: {signals_missed}")
```

…but it only **logged** them. `signals_missed` reached the coordinator's task
*output* but was never placed into `instance_review_state` — the structure the
review UI reads. So a reviewer never saw it.

Freshness is the exposed flank. Unlike uniqueness/referential-integrity,
freshness has **no deterministic backstop** — a staleness threshold is a
business judgment call, so it is deliberately never auto-proposed
(`_build_deterministic_candidates` excludes it). The *only* path to a freshness
check is Claude proposing one. If Claude omits the freshness signal:

- It's logged to a file nobody reads.
- No freshness instance is proposed.
- The reviewer sees a clean screen with **no hint** the signal was dropped.

#### Example

`ORDERS.SHIPPED_AT` is 400 days stale. The profiler emits
`freshness:SHIPPED_AT`. Claude's response omits it. Result: no freshness check,
and a review screen that looks complete. The exact "memory gap" the profiler
was built to close, reopened at the final step.

### The fix

`signals_missed` is now written into `instance_review_state` alongside the
active/skipped entries, so the review UI can render "N signals unaddressed":

```python
signals_missed = intel_result.get("signals_missed", [])
storage.update_agent_run(
    self.run_id,
    ai_rules_count=len([p for p in proposed_instances if p["kind"] == "new"]),
    instance_review_state={
        "active": active_entries,
        "skipped": skipped_entries,
        "signals_missed": signals_missed,
    },
    status="awaiting_rule_review",
)
```

The frontend now reads `instance_review_state.signals_missed` and renders an
amber warning banner at the top of the review panel when it is non-empty,
listing each unaddressed `signal_id` (e.g. `freshness:SHIPPED_AT`). See
`AgentWorkflow.tsx` (the `signalsMissed` banner) and the extended
`instance_review_state` type in `client.ts`.

---

---

## Fix #4 — Cost guard on AI-authored `draft_sql`

### The bug

`validate_sql` (`sql_validation.py`) proves a check's SQL is SELECT-only,
single-statement, keyword-clean, and table-scoped. It does **not** prove the
query is *cheap*. Template-shape SQL is safe by construction (hand-written
strings), but Claude's free-form `draft_sql` is not:

```sql
SELECT COUNT(*) AS FAILED_COUNT, (SELECT COUNT(*) FROM DB.SCH.EVENTS) AS TOTAL_COUNT
FROM DB.SCH.EVENTS a, DB.SCH.EVENTS b
WHERE a.user_id = b.session_id   -- accidental near-cartesian
```

This is a single SELECT, no forbidden keywords, references only the allowed
table → it **passes validation** and executes against production Snowflake at
proposal time (`_execute_check_sql`) as a multi-billion-row cross product.

### The fix

`SnowflakeSession.query` gained an optional `timeout` (seconds) that maps to
the connector's server-side statement timeout, and `_execute_check_sql` now
passes `_CHECK_SQL_TIMEOUT_SECONDS = 120`:

```python
rows = sf_session.query(rule_sql, timeout=_CHECK_SQL_TIMEOUT_SECONDS)
```

A query that exceeds the timeout is cancelled by Snowflake; the execution
returns `None`, and `_process_candidate` already discards any candidate whose
SQL fails to execute — so a runaway `draft_sql` is dropped, not proposed, and
never gets the chance to saturate the warehouse.

---

## Fix #5 — Single source of truth for word-overlap similarity

### The bug

`_word_overlap_score` plus a ~40-word stopword list was **copy-pasted** in two
places — `rule_intelligence_agent.py` (definition-dedup) and `rules.py`
(manual-rule duplicate-catch) — each gating on a hardcoded `0.55`. Tuning the
stopword set or threshold in one file silently diverged the two dedup gates:
a concept the manual API rejected as a duplicate could be recreated as new by
the agent, or vice-versa.

### The fix

Extracted `app/services/text_similarity.py` with `word_overlap_score` and
`DEFAULT_SIMILARITY_THRESHOLD`. Both call sites import from it; the local
copies were deleted. A docstring notes word overlap is a deliberately simple,
phrasing-sensitive signal and that embedding similarity is the intended
longer-term replacement.

---

## Fix #6 — Removed the dead-ahead `'*'`-scoped `sql_template` branch

### The bug

`RuleEngine.execute_sql_instances` matched instances where
`instance.database_name in (table_asset.database_name, "*")`, with a comment
admitting global `'*'` sql_template instances "aren't a thing today." But
sql_template instances **always** have a concrete `db.schema.table` baked into
their `rule_sql` at proposal time (`rule_sql_templates._fqn`). If a `'*'`
instance ever appeared (the CRUD API in `rules.py` *does* create `'*'`
instances, currently only `python_handler`), this branch would run one table's
baked-in SQL against every table in the batch — either erroring or, worse,
scanning the wrong table.

### The fix

The match is now `instance.database_name != table_asset.database_name`
(concrete only). Global `'*'` instances are a `python_handler` concept handled
by `execute_rules`, not here. The comment explains why a global sql_template
instance can't exist coherently.

---

## Fix #7 — LLM parse-failure is no longer mistaken for "no proposals"

### The bug

`_extract_json` tries three regex strategies then returns `{}` on failure. The
caller treated `{}` as `new_instances = []`. So a **truncated or malformed**
response (e.g. hitting `max_tokens` mid-JSON on a wide table) was
indistinguishable from a model that ran fine and proposed nothing — the
reviewer saw "0 new rules" on a screen that looked complete, and coverage
silently dropped to zero.

### The fix

Three layers:

1. **Retry once with a repair instruction** appended ("your previous response
   could not be parsed as JSON; respond again with ONLY a valid JSON
   object...").
2. If the retry still fails, log at **`error`** (not `info`) and set a
   `parse_failed` flag on the agent's result.
3. `parse_failed` flows through the coordinator into both the intel task
   output and `instance_review_state`, and the review panel renders a **red
   banner** telling the reviewer the list may be incomplete and to consider
   re-running — instead of trusting a silent zero.

---

## Fix #8 — Relationship discovery no longer trusts implicit cross-type joins

### The bug

`relationship_discovery` name-matches an FK-shaped column against a PK-shaped
column of the same name in another table (`CUSTOMER_ID` → `CUSTOMER_ID`), then
"verifies" the candidate with a live orphan-rate `LEFT JOIN` on
`t.from_col = r.to_col`. A candidate was only rejected if that query threw.

But if the two columns have **different but coercible** types — e.g.
`ORDERS.CUSTOMER_ID` is `VARCHAR('00123')` and `CUSTOMERS.CUSTOMER_ID` is
`NUMBER(123)` — Snowflake **implicitly coerces** them and the join *succeeds*,
returning a plausible-but-meaningless orphan rate. The failure chain from
there was the dangerous part:

1. The coerced orphan rate looks reasonable (say 8%), below the reject
   threshold → the row is stored `status="confirmed", confidence="verified"`.
2. A confirmed+verified relationship becomes a **deterministic**
   referential-integrity candidate in `RuleIntelligenceAgent`
   (`_build_deterministic_candidates`) — the path explicitly branded "objective
   fact, no LLM judgment needed."
3. It reaches the reviewer with an **"Auto-detected"** badge and violation
   evidence — exactly the framing most likely to be approved.

So a type-coercion artifact got laundered into a "verified fact" and
rubber-stamped. This is the only discovery bug that corrupts *data a human then
acts on*, which is why it's rated High.

### The fix

A **type-compatibility gate runs before** the live orphan-rate join:

- `_fetch_schema_columns` now also returns each column's `DATA_TYPE` (same
  single `INFORMATION_SCHEMA.COLUMNS` query, no extra round trip), keyed by
  `(TABLE.upper(), COLUMN.upper())`.
- `_type_family` maps a Snowflake type to a join family (`numeric`, `text`,
  `temporal`, `boolean`, `binary`), stripping any precision suffix.
- `_types_compatible` returns `False` only when **both** types are known and in
  **different** families. Unknown/missing metadata is treated as compatible, so
  a real FK is never over-skipped just because a type couldn't be resolved —
  the gate targets the *definite* mismatch (VARCHAR vs NUMBER), not the unknown.
- A definite mismatch is recorded as `status="rejected",
  confidence="type_mismatch"` and logged, rather than silently dropped — and it
  doesn't consume one of the capped live-verification slots.

Because all three downstream consumers require `status == "confirmed"` (and the
deterministic path additionally requires `confidence == "verified"`), a
`type_mismatch` row can never reach a proposal.

```python
from_type = column_types.get((cand["from_table"].upper(), cand["from_column"].upper()))
to_type   = column_types.get((cand["to_table"].upper(), cand["to_column"].upper()))
if not _types_compatible(from_type, to_type):
    results.append(storage.upsert_relationship(..., status="rejected", confidence="type_mismatch"))
    continue
```

The type-family logic was unit-checked in isolation (including the exact
`VARCHAR` vs `NUMBER` bug case, precision-suffix stripping, and the
unknown-type "compatible" fallback).

---

## Verification

All edited backend files byte-compile cleanly:

```
python -m py_compile \
  app/services/text_similarity.py \
  app/services/snowflake_session.py \
  app/services/rule_engine.py \
  app/services/agents/findings_agent.py \
  app/services/agents/rule_intelligence_agent.py \
  app/services/agents/coordinator.py \
  app/api/rules.py
```

Frontend `tsc --noEmit` introduces no new type errors (the only errors
reported are pre-existing unused-import warnings in files untouched by these
changes).

> **Runtime note:** the Snowflake/Cortex/Bedrock paths cannot be exercised
> without a live connection, so these fixes were verified by compilation and
> type-checking, not an end-to-end run.

## Not addressed here (tracked separately)

Larger items identified but out of scope for these contained fixes:

- **Every check executes twice** (proposal-time in `RuleIntelligenceAgent` +
  findings-time in `RuleEngine`). A caching/skip strategy for large tables is
  the intended follow-up.
- **`WorkflowCoordinator` is a ~500-line procedural god-object** — the
  proposal-persistence / review-state block is the first candidate for
  extraction into a `ReviewStateBuilder`.
- **Word overlap is a weak dedup signal** — now centralized (Fix #5), but an
  embedding-based similarity would be materially more robust.
