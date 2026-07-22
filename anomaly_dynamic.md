# Making anomaly metrics dynamic

Today the anomaly-detection substrate is deterministic and mostly hardcoded.
Metrics captured, their template shapes, severities, and thresholds all live
in Python constants (`metric_snapshots._extract_metrics` and
`anomaly_proposal_agent._METRIC_TO_SHAPE`). This doc lays out the layers of
hardcoding and how each one can be opened up — cheapest first.

## Current state (baseline)

`record_metric_snapshots` runs after every ProfilingAgent pass and captures
a fixed list of metrics into `METRIC_SNAPSHOTS`:

| Metric                | Scope  | Source (facts key)      | Derivation                                       |
|-----------------------|--------|-------------------------|--------------------------------------------------|
| `row_count`           | table  | `column_stats.total`    | Max of `total` across columns                    |
| `freshness_lag_hours` | table  | `freshness_signals`     | Min `age_days` × 24                              |
| `null_pct`            | column | `column_stats.null_pct` | Direct copy, clamped 0–100                       |
| `distinct_count`      | column | `column_stats.distinct` | Direct copy                                      |
| `observed_categories` | column | `closed_set_columns`    | Union of values seen (stored under `metric_meta`)|

`refresh_baseline` then computes rolling-30d median + MAD (or a union set for
`observed_categories`) into `METRIC_BASELINES`. Once `sample_count >= 14`,
`AnomalyProposalAgent` proposes rules using a hardcoded
metric → (shape, severity) map:

```python
_METRIC_TO_SHAPE = {
  "row_count":            {"shape": "metric_anomaly",       "severity": "high"},
  "freshness_lag_hours":  {"shape": "metric_anomaly",       "severity": "high"},
  "null_pct":             {"shape": "metric_anomaly",       "severity": "medium"},
  "distinct_count":       {"shape": "metric_anomaly",       "severity": "low"},
  "observed_categories":  {"shape": "category_disappeared", "severity": "medium"},
}
```

Default thresholds `deviations=3.0` and `max_pct_change=25.0` are also
Python constants.

## Layer 1 — per-rule threshold editing (already possible)

Each approved anomaly rule stores its `threshold_config` on the
`RULE_INSTANCES` row. It's already user-editable via the existing
"edit threshold" endpoints. All that's missing is UI polish:

- Surface `deviations` / `max_pct_change` on the anomaly rule detail page.
- Add a small histogram of the last 30 days' snapshots with the current
  MAD band overlaid, so users can pick a threshold visually.

**Effort:** 1–2 days of frontend work; no backend or schema changes.
**Payoff:** covers the ~80% case where users just want to loosen or tighten
one specific rule.

## Layer 2 — global metric config table

Move `_METRIC_TO_SHAPE` and the default thresholds out of Python and into
a table so an admin can toggle metrics, change severities, or shift
defaults without a code deploy.

```sql
CREATE TABLE ANOMALY_METRIC_CONFIG (
  METRIC_NAME       VARCHAR(100) NOT NULL,
  TEMPLATE_SHAPE    VARCHAR(50)  NOT NULL,
  DEFAULT_SEVERITY  VARCHAR(20)  NOT NULL,
  DEFAULT_THRESHOLDS VARIANT,
  ENABLED           BOOLEAN DEFAULT TRUE,
  PRIMARY KEY (METRIC_NAME)
);
```

`AnomalyProposalAgent` reads this table once per run. Rows disabled here
never surface as proposals; changing `DEFAULT_THRESHOLDS` here only
affects *new* proposals — existing rules keep their per-instance thresholds.

**Effort:** ~1 day (migration + a small admin page).
**Payoff:** rollout of new metric behaviour without a deploy, plus a clear
"what does this system watch out of the box" surface.

## Layer 3 — per-asset overrides

Same table, one more column:

```sql
ALTER TABLE ANOMALY_METRIC_CONFIG ADD COLUMN ASSET_ID VARCHAR(36);
```

Lookup order at proposal time:
1. `(asset_id, metric_name)` — table-specific.
2. `(NULL, metric_name)` — global default.
3. Hardcoded fallback (kept as a safety net).

A billing table might have `deviations=2` (tighter); a log table might have
`deviations=5` (looser). Same metric, different sensitivity.

**Effort:** ~2 days including the "override this metric for this table" UI.
**Payoff:** stops the false-positive complaints on high-variance tables
without requiring users to hand-tune every generated rule.

## Layer 4 — user-defined custom metrics

The `metric_anomaly` SQL is already generic — it reads snapshots by
`metric_name` and doesn't care where they came from. The only reason we
can't watch business metrics today is that only 5 metric names are ever
populated.

Add a table describing custom metric definitions:

```sql
CREATE TABLE CUSTOM_METRICS (
  ID              VARCHAR(36) PRIMARY KEY,
  ASSET_ID        VARCHAR(36) NOT NULL,
  METRIC_NAME     VARCHAR(100) NOT NULL,
  SQL_EXPRESSION  TEXT NOT NULL,
  COLUMN_NAME     VARCHAR(255),
  ENABLED         BOOLEAN DEFAULT TRUE,
  CREATED_BY      VARCHAR(100),
  CREATED_AT      TIMESTAMP_TZ DEFAULT CURRENT_TIMESTAMP()
);
```

`SQL_EXPRESSION` is a scalar-returning SELECT (e.g.
`SELECT SUM(AMOUNT) FROM {fqn}`). At scan time, right after the built-in
capture inside `record_metric_snapshots`, iterate the enabled custom rows
for this asset, execute each expression, and insert a `METRIC_SNAPSHOTS`
row with the returned scalar.

Safety:
- Run through `sql_validation.validate_sql` with `allowed_tables` set to
  the target FQN — same gate that protects Claude-authored draft SQL.
- Enforce SELECT-only, no CTEs modifying data, no cross-database.
- Execute with a small timeout (e.g. 30s) so a bad expression can't stall
  the scan.

Once snapshots exist under a custom name, baselines and proposals happen
automatically. No further wiring.

**Effort:** ~3–5 days (schema + capture loop + validation + UI to author).
**Payoff:** business-facing anomaly detection ("watch `sum(amount)` daily,
`count(distinct customer_id)` weekly") without touching Python code.

## Layer 5 — LLM-suggested metrics + thresholds

A companion agent to RuleIntelligence that reads column semantics
(profile + table description + past reviews) and proposes both:

1. Custom metrics worth watching (populates `CUSTOM_METRICS`).
2. Overrides to global thresholds for this asset (populates
   `ANOMALY_METRIC_CONFIG` at the per-asset layer).

Example output for a payments table:

```json
{
  "custom_metrics": [
    {"metric_name": "total_amount",  "sql": "SELECT SUM(AMOUNT) FROM {fqn}"},
    {"metric_name": "failed_count",  "sql": "SELECT COUNT(*) FROM {fqn} WHERE STATUS='FAILED'"}
  ],
  "overrides": [
    {"metric_name": "row_count",  "deviations": 2.0, "reason": "billing tables are load-stable"}
  ]
}
```

Runs once per table on onboarding, and re-runs on schema drift or when
review lessons accumulate. After that, the deterministic pipeline in
Layers 1–4 takes over.

**Effort:** ~1–2 weeks (agent + prompt design + evaluation harness +
gating so LLM output can't create runaway proposals).
**Payoff:** turns anomaly setup from "here's what we watch" into
"here's what a data engineer would watch for a table like yours."

## Recommendation

Ship **Layers 1 + 2 + 3** as one project. That's the smallest change that
turns the current fixed pipeline into something users can adjust in the
UI without asking for a deploy. It also lays the config-table plumbing
that Layers 4 and 5 both build on.

Layer 4 is the natural follow-up when users start asking "can we watch
`sum(revenue)`?" — the answer today is no; after Layer 4 it's a form.

Layer 5 is worth it only if you want LLM in this path at all. The
existing pipeline is fully deterministic and there's a case for keeping
it that way — anomaly detection is one of the places where "boring +
predictable" is a feature.

## Migration notes

- Keep the hardcoded `_METRIC_TO_SHAPE` map as a fallback even after
  Layer 2 lands. On first deploy the config table is empty; the fallback
  is what seeds it (one-time migration).
- `RULE_INSTANCES.threshold_config` is source-of-truth for **existing**
  approved rules. Changing config-table defaults must never mutate
  already-approved instances silently — only new proposals.
- Custom metrics need a garbage-collection story: if the user disables
  a `CUSTOM_METRICS` row, the associated `METRIC_BASELINES` becomes
  stale. Either mark the baseline `enabled=false` at the same time or
  let it age out of the rolling window.
