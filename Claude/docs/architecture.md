# Architecture — Agentic Data Quality Platform for Snowflake

> Status: living design doc. Reflects decisions made on 2026-07-02.
> This supersedes the "Ideal Architecture Direction" section of the root README where they differ.

## 1. What we are building (one paragraph)

An agentic Data Quality (DQ) platform for Snowflake. Instead of a data engineer or
admin hand-writing DQ rules — slow, error-prone, and easy to leave gaps — a multi-agent
system connects to Snowflake (read-only on the source), scans metadata, profiles tables
and columns, detects sensitive data, infers what each table means, and then **recommends
DQ rules** (predefined templates + LLM-generated), each with severity, confidence,
priority, reasoning, evidence, and a proposed threshold. Every rule is compiled to
**SELECT-only SQL**, validated for safety, and test-run so the admin can see expected
pass/fail *before* approving. Approved rules run (manual now, scheduled later); failures
become **alerts** on a business-friendly dashboard. Feedback (approve / reject / edit /
false-positive) improves future recommendations. It is "agentic" because it is a
multi-step, tool-using, human-in-the-loop reasoning workflow — not a single LLM call.

## 2. Key decisions (locked)

| Decision | Choice | Why |
| --- | --- | --- |
| Backend shape | **Python-only** (FastAPI + LangGraph) | One service, mature Snowflake Python connector, drops React→Node→Python glue. Node BFF deferred until there's a real reason. |
| Orchestration | **LangGraph** | State, checkpoints, retries, human-in-the-loop are first-class. |
| LLM | **Claude** (latest: Opus 4.8 / Sonnet 5 as cost/quality dictates) | Per project mandate. |
| Source DB access | **Read-only**, SELECT-only, enforced by SQL validator | Never mutate source. |
| App state DB | **Snowflake** (app-owned, full access) for MVP | Keeps stack simple. See §9 caveat — operational state may move to Postgres later. |
| PII to LLM | **Masking/stats layer, per-column policy** | Raw allowed only for non-sensitive columns; PII is masked or reduced to stats. See §7. |
| Data access strategy | Metadata + profiling + sample + full, **sample-first by default** | TB-scale tables make full scans a cost trap. |
| Frontend | React + TypeScript | Per README. |

## 3. Component diagram (MVP)

```
┌─────────────────────────────────────────────┐
│              React + TS Frontend             │
│  Connections · Explorer · Scan progress ·    │
│  Recommended rules · Approval (⇒) · Active   │
│  rules · Alerts dashboard · Alert detail     │
└───────────────────────┬─────────────────────┘
                        │ REST + SSE (progress stream)
                        ▼
┌─────────────────────────────────────────────┐
│         Python Backend (FastAPI)             │
│  - API routes (rules, alerts, scans, dash)   │
│  - Auth/session (Snowflake SSO ext-browser)  │
│  - Kicks off + tracks LangGraph runs         │
│  - Repositories (read/write app Snowflake DB)│
├─────────────────────────────────────────────┤
│         LangGraph Agent Orchestrator         │
│  (runs in-process for MVP; can split later)  │
│  Metadata → Profiling → PII → Domain →       │
│  Recommend → SQL-gen → SQL-validate →        │
│  Test-run → (persist PENDING) → Alerting     │
├─────────────────────────────────────────────┤
│                 Tool Layer                   │
│  Snowflake metadata · profiler · safe-SQL    │
│  executor · SQL validator · PII detector ·   │
│  rule-template engine · masking · LLM client │
└───────────────────────┬─────────────────────┘
                        ▼
┌─────────────────────────────────────────────┐
│                 Snowflake                    │
│  Source account (READ-ONLY)                  │
│    INFORMATION_SCHEMA · ACCOUNT_USAGE ·      │
│    tables/views · sample data                │
│  App-owned DB (FULL access)                  │
│    metadata snapshots · profiles · rules     │
│    (recommended/approved/rejected) ·         │
│    execution history · alerts · feedback ·   │
│    agent run logs                            │
└─────────────────────────────────────────────┘
```

FastAPI and LangGraph run **in the same process** for the MVP. This is deliberate: no
inter-service network hop, simplest deployment. If we ever need to scale the agent
workers independently, LangGraph can be pulled into its own service without touching the
frontend contract.

## 4. The agent workflow

Two distinct flows. Do **not** cram them into one graph.

### 4a. Recommendation flow (scan → pending rules)

```
START
  → Metadata Discovery      (SHOW / INFORMATION_SCHEMA / ACCOUNT_USAGE)
  → Object Prioritization    (high-priority tables first; user can override)
  → Data Profiling           (SAMPLE-FIRST; depth is an explicit setting)
  → PII / Sensitivity        (assigns per-column LLM sharing policy)
  → Domain Understanding     (what does this table/column mean)
  → Rule Recommendation      (templates + LLM; structured JSON out)
  → SQL Generation           (template-first, LLM only when needed)
  → SQL Validation           (HARD gate: SELECT-only, safe)
  → Rule Test Execution      (run on sample/history; expected pass/fail)
  → Dedup / rank             (semantic dedup + priority scoring)
  → PERSIST as PENDING_APPROVAL
END
```

Runs to completion for **all** rules, then stops. Approval is **not** a live graph pause
(see §8). Mid-graph interrupts are reserved only for clarification questions the agent
cannot resolve on its own.

### 4b. Execution flow (approved rules → alerts)

```
START
  → Load active (approved, enabled) rules
  → Execute each rule's validated SQL (read-only)
  → Compare result to threshold
  → PASS  → record in execution history (shows on "passing rules" page)
  → FAIL  → Alert Creation Agent → explanation → ALERTS table
  → Resolve alerts that now pass (auto-close)
END
```

Manual trigger for MVP1; scheduled in MVP2.

## 5. Agents and their responsibilities

| Agent | Job | Guardrail |
| --- | --- | --- |
| Orchestrator | Decide next step, whether to ask human, retry, skip expensive profiling | Never runs raw SQL directly |
| Metadata Discovery | Databases/schemas/tables/columns/types/comments/row counts/last-altered | Read-only |
| Data Profiling | Nulls, distincts, min/max, avg, stddev, top values, patterns, freshness, dupes | Sample-first; depth is a setting |
| PII / Sensitivity | Classify columns LOW/MED/HIGH; assign sharing policy | **Deterministic detectors + LLM assist; enforced by masking layer** |
| Domain Understanding | Infer table/column business meaning | Uses names, comments, profile, relationships |
| Rule Recommendation | Propose rules (templates + LLM), each as structured JSON | Numbers from profiling code, not LLM |
| SQL Generation | Compile rule → SQL | Template-first; LLM only when template can't express it |
| SQL Validation | Enforce SELECT-only + safety | **Mandatory hard gate** (see §6) |
| Rule Test Execution | Run rule now; expected fail count, %, masked samples | Read-only, with LIMIT/timeout |
| Alert Creation | Turn failures into explained, grouped alerts | — |
| Feedback (MVP2) | Retrieve past approvals/rejections into recommendation prompt | Retrieval, not fine-tuning |

## 6. SQL safety (non-negotiable)

Every generated SQL statement passes the validator **before** execution:

- Parse with a real SQL parser (e.g. `sqlglot`) — not regex alone.
- Allow exactly one top-level `SELECT`. Reject `INSERT/UPDATE/DELETE/MERGE/DROP/ALTER/
  CREATE/TRUNCATE/COPY INTO/GRANT/CALL` and multi-statement bodies.
- Only tables/schemas that were discovered and are in scope may be referenced.
- Enforce statement timeout and a row `LIMIT` on sample/test runs.
- Run under a Snowflake role that **only has read access** — the validator is defense in
  depth, the role is the real wall. Both must hold.

## 7. PII / sensitivity handling

Raw data to the LLM is allowed **only for columns classified as non-sensitive**. Sensitive
data is never sent raw. This is enforced as a **deterministic layer**, not left to an
agent's judgment:

```
Column → PII detector (regex/heuristics for email, phone, PAN, Aadhaar, names,
         addresses, financial ids) + LLM assist for ambiguous cases
       → sensitivity: LOW / MEDIUM / HIGH
       → sharing policy (agent may request stricter, never looser):
            ALLOW_RAW_SAMPLE     (LOW only)
            ALLOW_MASKED_SAMPLE  (MEDIUM)
            ALLOW_STATS_ONLY     (HIGH)
```

Every LLM call routes through a **masking middleware** that applies the column's policy.
No agent can bypass it. The agent decides *how much* it needs (raw / masked / stats) but
the middleware is the floor: if policy says STATS_ONLY, no sample leaves the boundary
regardless of what the agent asked for.

## 8. Human-in-the-loop / approval

- Recommendation flow persists all rules as `PENDING_APPROVAL`, then ends. Approval is a
  **normal async API interaction**, not a paused graph. This matches the requirement that
  agents "not wait too long" — they finish and move on.
- Approval UI: two columns with a `⇒` control. Left = pending rules (collapsible, click
  through to a rule-detail page); pressing `⇒` moves a rule to the right (active) column.
  Admin can approve / edit any field / reject (with optional reason) / change severity /
  threshold / schedule / mark false-positive.
- Edited rules re-run SQL validation before activating.
- Rejections (and reasons) are stored to improve future recommendations.
- Approved rules can be disabled later; a passing rule auto-clears its alert on next run.

## 9. Data storage notes

App-owned Snowflake DB tables (MVP):

```
APP_CONNECTIONS         METADATA_SNAPSHOTS      TABLE_PROFILES
COLUMN_PROFILES         RECOMMENDED_RULES       APPROVED_RULES
REJECTED_RULES          RULE_EXECUTION_HISTORY  ALERTS
ALERT_VIOLATION_SAMPLES USER_FEEDBACK           AGENT_RUN_LOGS
```

**Caveat, decided consciously:** Snowflake is analytical, not OLTP. Frequent small writes
(rule edits, alert status flips, agent logs) are slower and pricier than on Postgres. For
the MVP this is acceptable and keeps the stack to one datastore. If operational write
volume becomes a pain point, move *operational* state (rules, alerts, run logs) to Postgres
and keep Snowflake for *analytical* data (profiles, execution history). Not doing that now.

## 10. Cost & performance guardrails

- **Sample-first profiling** by default; full scan only for small or explicitly-important
  tables. Profiling depth is a visible setting, not an agent guess.
- High-priority tables analyzed first.
- Cache metadata and profiles in the app DB; re-use across scans.
- Statement timeouts + row limits on every source query.
- A `cost_guard` utility estimates/limits query cost before expensive scans.
- Scans should be **idempotent** — re-running must not duplicate rules (dedup on a stable
  rule identity: table + column + rule_type + normalized SQL).

## 11. What each score means

- **Confidence** — how sure the system is the rule is *logically correct*.
- **Severity** — how bad it is *if the rule fails*.
- **Priority** — blend of severity + confidence + business importance + history; drives
  ordering and attention. Very-basic rules get tagged low so they don't crowd the top.

## 12. Explicitly deferred (see mvp-scope.md for phases)

Account-wide scanning · scheduling · trend/history analytics · true feedback learning ·
chat with the system · Slack/PagerDuty/email · multi-step approval · RBAC · audit logs ·
rule versioning history · secret manager · multi-tenant isolation · LLM fallback.
