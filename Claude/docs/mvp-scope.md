# MVP Scope — Agentic Data Quality Platform

> Companion to `architecture.md`. Defines what ships in each phase and — just as
> importantly — what does **not**. Reflects decisions made on 2026-07-02.

## Guiding principle

Keep the demo powerful but the build realistic. Every phase must be independently
demoable end-to-end. We cut *breadth* (how many objects, how automatic) long before we
cut *safety* (SELECT-only validation, PII masking) — those two are in from day one.

---

## MVP 1 — "It works end to end on what I point it at"

**Goal:** connect to Snowflake, scan a *selected* database/schema/table, get recommended
rules with evidence, approve them, run them, see alerts on a dashboard.

### In scope
- Snowflake connection (SSO external-browser auth for dev).
- Metadata scan for all objects **within a selected scope** (DB / schema / tables the user
  picks — not the whole account).
- Data profiling for the selected / high-priority tables. **Sample-first**; profiling depth
  is a visible setting.
- PII/sensitivity detection + **masking layer enforced** (raw only for LOW columns; masked
  or stats for MEDIUM/HIGH). Raw-to-LLM allowed for non-sensitive columns during testing.
- Predefined DQ rule templates (completeness, uniqueness, validity, freshness, volume,
  basic drift/range/accepted-values).
- LLM rule recommendations (structured JSON) layered on top of templates.
- SQL generation: template-first, LLM only when a template can't express the rule.
- **SQL validator (hard gate): SELECT-only + safety.**
- Rule test-execution: expected pass/fail, fail count/%, masked sample violations.
- Semantic dedup + priority/confidence/severity scoring.
- Human approval: two-column `⇒` UI, approve / edit / reject (+ optional reason).
- Manual rule execution.
- Alerts page + basic alerts dashboard; separate "passing rules" view.
- Store everything in the app-owned Snowflake DB.
- Agent progress/log stream visible in the UI (SSE).

### Out of scope (MVP1)
- Scheduling, account-wide scan, trend analytics, feedback learning, chat, external
  notifications, RBAC, audit logs, rule versioning.

### Done when
A user connects, scans a schema, reviews recommended rules with evidence, approves a set,
runs them manually, and sees failures as alerts and passes on the passing-rules page —
all without any non-SELECT SQL ever reaching the source, and no raw PII leaving the boundary.

---

## MVP 2 — "It runs itself and gets smarter"

**Goal:** scheduled execution, history-driven thresholds, and feedback that improves
recommendations.

### In scope
- Scheduled scans and scheduled rule execution (orchestrator on a timer + manual).
- Historical thresholds: thresholds computed from profiling/execution **history**, not a
  single snapshot. Numbers from code; LLM writes the natural-language explanation.
- Trend dashboard (row-count trends, pass-rate over time, alert frequency).
- **Feedback learning via retrieval** (not fine-tuning): store approved/rejected rules +
  reasons; inject relevant past decisions as few-shot examples into the recommendation
  prompt.
- Table health score.
- Rule priority scoring refined with historical failure frequency + false-positive rate.
- PII-safe prompting hardened (masked/stats as the default path, raw only opt-in).

### Out of scope (MVP2)
- Account-wide scan, multi-step approval, external notifications, chat, RBAC, audit logs.

---

## MVP 3 — "Enterprise-ready surface"

- Account-wide scans across all accessible databases.
- Multi-step approval flow (admin approval short-circuits it).
- Slack / email / PagerDuty notifications.
- Chat with the system (ask questions about rules/alerts/data).
- Cross-database and eventually cross-warehouse support.
- Advanced domain learning (query history, business glossary integration).
- Audit logs, RBAC, multi-tenant isolation.

---

## Production hardening (parallel track, explain-don't-build for internship)

RBAC · audit logs · secret manager · query cost controls · warehouse limits · timeouts ·
sampling strategy · PII masking policy · data-access policy · rule versioning · alert
lifecycle · incident integrations · retry handling · agent-run observability · prompt/
version tracking · human-approval history · multi-tenant isolation · LLM fallback ·
evaluation framework.

---

## Cross-cutting invariants (true in every phase)

1. **Source DB is read-only.** SELECT-only, enforced by validator *and* Snowflake role.
2. **No raw PII to the LLM.** Masking middleware is the floor; agents can only request
   stricter, never looser.
3. **Scans are idempotent.** Re-running never duplicates rules.
4. **Numbers from code, words from the LLM.** Thresholds/stats are computed
   deterministically; the LLM explains them.
5. **Every rule is test-run before a human sees it.** No approving blind.
6. **Approval is async.** The recommendation graph finishes and persists; it does not
   block waiting on a human.

---

## Suggested build order for MVP 1 (when we start coding)

1. Snowflake connection + read-only role + app-DB schema SQL (`infra/snowflake/`).
2. Metadata scan tool + persistence.
3. Profiling tool (sample-first) + persistence.
4. PII detector + masking middleware (before any LLM call exists).
5. Rule templates + template SQL generation.
6. SQL validator (hard gate).
7. Rule test-execution.
8. LLM recommendation agent (structured JSON) wired through masking.
9. Dedup + scoring.
10. LangGraph recommendation flow tying 2–9 together.
11. FastAPI routes + SSE progress.
12. React: connection → explorer → scan progress → recommended rules → approval `⇒` →
    active rules → manual run → alerts.

Safety pieces (4, 6) come *before* the first LLM/execution call — not bolted on after.
