- Pass Rate (per rule): passed_runs / (passed + failed + error) over the last 20 executions
  for that specific rule instance. Simple ratio, unweighted.
  - Health Score (table-level): a severity-weighted average of all rules' pass rates —
  critical rules get weight 5, high=3, medium=2, low/info=1.

  So if you have one critical rule failing and several low-severity rules passing, the pass
  rate on individual rules may look fine but the health score tanks because the critical rule
  dominates. That's the intended behavior — health score punishes high-severity failures more.

 All finding statuses:
  - detected — newly found, not yet reviewed
  - validated — confirmed as real, open
  - in_progress — someone is actively fixing it  
  - resolved — fixed / rule no longer fires
  - false_positive — dismissed as not a real issue
  - wont_fix — acknowledged but accepted
  - closed — generic closed
  - superseded — replaced by a newer finding

  In short: the "failing 6h" badge only appears for rules that have an open (unresolved)      
  finding right now. The other rules either passed their last run, or their finding was
  previously resolved/closed, so there's no open finding to pull the timestamp from. This is
  correct behavior — it's showing you which rule

  Fleet & Fleet Health

  Fleet = every monitored table across all your database connections, treated as one system.  
  It's the top-level rollup above individual tables.

  Fleet Health (computed in table_health.py → GET /fleet/overview) aggregates:
  - Fleet health score — severity-weighted pass rate across all rule executions in the last 30
  days (critical rules weighted 5×, high 3×, medium 2×, low/info 1×)
  - Open incidents — total open findings across all tables
  - Flapping incidents — open findings that have been reopened at least once (unstable data
  quality)
  - Oldest open — how long your longest-standing unresolved finding has been open


  Whether that's a bug depends on intent. Arguments for supporting views:
  - Users often expose curated data through views; DQ that ignores them misses the layer      
  consumers actually query.
  - Row-level rules (nulls, uniqueness, ranges) work fine on views — the engine just runs SQL.

  Arguments against:
  - Views can be expensive to scan (recomputed each run).
  - Metadata-shape rules (PII column names, FK constraints, nullable IDs) are less meaningful 
  on views — the underlying tables are the source of truth.
  - Materialized views need volume/freshness rules; regular views don't.
shou