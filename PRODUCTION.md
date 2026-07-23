# Production Readiness Plan

This document is a concrete, opinionated plan for turning the Data Quality
Tool from a single-user local app into a production-grade multi-user system
for an organization. It assumes you are willing to redesign parts of the
current codebase and reset existing data — nothing in the current running
deployment is treated as sacred.

The plan is broken into: **what we're solving**, **the target architecture**,
**the concrete work items in build order**, and **the decisions that block
us from just doing it**.

---

## 1. What we're solving

The tool works well as a single-person local app: one operator boots the
backend, authenticates once via Snowflake SSO in a browser popup, and drives
the tool through the frontend. When you try to give the same URL to ten
teammates, four things break:

### 1.1 One identity for everyone
The backend caches whichever human ran the initial SSO. Every subsequent
browser session — regardless of who opened it — shares that one identity.
The frontend shows "Signed in: Alice" to Bob. Every write attributed to
Alice regardless of who clicked the button. Snowflake audit logs show only
one user.

### 1.2 No login, no roles, no authorization
There is no login page. There is no notion of admin vs. editor vs. viewer.
Anyone with the URL can approve rules, delete connections, run scans, or
resolve findings.

### 1.3 Connection-ID data lifecycle is fragile
Every scan, run, finding, lineage edge, and schedule is stamped with a
`connection_id` UUID. If an admin deletes a connection and adds it back
(even with identical details), the new row gets a new UUID. All historical
rows still reference the deleted UUID and disappear from the UI's filters.
Nothing is technically lost, but the history looks vanished to everyone
using the app.

### 1.4 The single-connection singleton doesn't scale
`SnowflakeSession` is a module-level singleton holding one connection.
Under a multi-worker Uvicorn deployment it becomes N mutually-inconsistent
singletons, one per worker. Under load the internal `_exec_lock` serializes
every role-switching execution across the process.

There are secondary gaps too — secrets on disk, no HTTPS story, no
structured logs, no CI/CD, no audit trail — but the four above are the
load-bearing ones. Everything else follows once those are fixed.

---

## 2. Target architecture

Here is the shape of the app after this project lands. Each layer is a
paragraph; details live in section 3.

### 2.1 Identity — OIDC through your IdP
Users open the app and are redirected to your corporate IdP (Okta, Entra
ID, Google Workspace, etc.). The IdP returns a signed JWT containing the
user's email, name, and stable subject ID. FastAPI validates the token on
every request via a dependency. First-time users are auto-provisioned into
a new `USERS` table. There is no password stored in the app.

### 2.2 Authorization — RBAC in the app DB
Three roles: **admin**, **editor**, **viewer**. Stored per user in `USERS`.
FastAPI dependencies gate every state-changing endpoint. The frontend
receives the caller's role and hides UI affordances the caller can't
exercise, but the backend is the authoritative enforcer (a viewer who
crafts a raw PATCH still gets 403). Admins are bootstrapped through an
`ADMIN_EMAILS` env var; after the first admin exists, admins can promote
others through the UI.

### 2.3 Snowflake identity — service account
The backend runs under one dedicated Snowflake service user (e.g.
`DQ_APP_SERVICE`) with a narrow role. That role has SELECT on the source
schemas the tool is allowed to scan and full DML on `DQ_APP`. Nothing else.
No individual user's SSO is involved in query execution. Snowflake-side
audit logs show `DQ_APP_SERVICE`; app-side audit logs show the actual
human. This is the model most orgs use for tools like this.

### 2.4 Secrets — external secrets manager
Nothing sensitive lives in `.env` in production. Snowflake service-account
credentials, the IdP client secret, and all user-added Postgres/Snowflake
connection secrets move to AWS Secrets Manager / HashiCorp Vault / Azure
Key Vault (pick what your org already uses). The abstraction already
exists in `services/secrets_manager.py` — this is a matter of swapping
the storage backend.

### 2.5 Connection model — soft-delete + stable slug
Connections gain a `SLUG` column that is the human-readable stable business
key (`"prod-snowflake"`, `"analytics-postgres"`). The physical UUID stays as
the FK on all seven referencing tables — no migration to slug-as-FK — but
the UI never lets a user physically DELETE a connection with references.
Deletes become soft (`IS_ACTIVE=FALSE`); rebinds are done by editing the
existing row via PATCH. A separate admin-only rebind endpoint atomically
reassigns FKs when a genuine replacement is needed.

### 2.6 Concurrency — connection pool
`SnowflakeSession` is replaced by a proper connection pool. Requests
borrow a connection, run queries, return it. Long-running Cortex calls
get a longer borrow. The service-account credentials are loaded once from
Secrets Manager at worker startup. The `_exec_lock` global goes away.

### 2.7 Deployment — containerized, HTTPS, multi-worker
Docker image containing the FastAPI backend. Frontend built to static
assets and served by Nginx / Caddy / the reverse proxy of your infra.
Uvicorn + Gunicorn with N workers behind the proxy for TLS termination
and request routing. Health check endpoint (`/health`) already exists;
readiness endpoint added.

### 2.8 Observability — logs, metrics, errors, audit
Structured JSON logs to stdout. Prometheus metrics via
`prometheus-fastapi-instrumentator`. Sentry (or equivalent) for exception
capture. A dedicated `AUDIT_LOG` table capturing who did what when,
replacing the scattered `APPROVED_BY` / `MUTED_BY` / `REJECTED_BY` columns
with one uniform pattern.

### 2.9 Data safety
Snowflake `DATA_RETENTION_TIME_IN_DAYS` set explicitly on `DQ_APP` so
Time Travel restore is guaranteed available. Documented recovery drill,
tested quarterly. Secret rotation procedure.

### 2.10 CI/CD
Every PR runs `pytest`, `tsc --noEmit`, and `docker build`. Merge to main
auto-deploys to staging. Manual promotion to prod. Dependabot / Snyk / your
org's equivalent scans dependencies weekly.

---

## 3. Work plan (build order)

Each phase is standalone and shippable. The order is chosen so each phase
unblocks the next — you can pause after any phase without leaving the app
in a broken state.

### Phase 1 — Connection model rewire (2 days)

**Goal:** eliminate the orphaning failure mode at the schema level.

- Add `CONNECTIONS.SLUG` unique text column, populate for existing rows
  from a normalized `NAME`.
- Change `IS_ACTIVE` semantics to be a soft-delete flag (already present,
  now enforced everywhere).
- Modify `DELETE /connections/{id}` to refuse when any row in `SCANS`,
  `AGENT_RUNS`, `LINEAGE_EDGES`, `LINEAGE_REFRESH_STATE`,
  `LINEAGE_CATALOG`, `LINEAGE_CAPABILITY_CACHE`, or `SCHEDULES` references
  it. Suggest soft-disable instead in the error body.
- Add admin-only `POST /connections/{id}/rebind?into=<slug>` that
  atomically UPDATEs the FK in all seven tables and marks the source
  inactive. Wrapped in a Snowflake transaction.
- Frontend: rename the "Delete" button to "Disable" when a connection has
  references; show reference counts inline.

**Deliverables:** migration file, updated `api/connections.py`,
`storage.py::rebind_connection`, updated Connections page in the frontend.

**Not in scope:** changing FK column type. UUIDs stay.

### Phase 2 — Service account + connection pool (2 days)

**Goal:** replace the "one human's SSO holds the app up" model with a
headless service account that scales across workers.

- Provision Snowflake service user `DQ_APP_SERVICE` and role `DQ_APP_ROLE`.
- Add key-pair auth support in `snowflake_session.py` alongside the
  existing SSO / password paths.
- Introduce a pool: `SnowflakePool` class holding N connections, all
  service-account. `get_connection()` becomes `borrow()` / `return_()`.
  Existing `session.query(...)` / `session.execute(...)` call sites keep
  working via a thin facade so the migration is mechanical.
- Remove the `_exec_lock` global; per-connection locking only.
- Load service account credentials from Secrets Manager at pool startup.
- Delete `SNOWFLAKE_AUTH_METHOD=externalbrowser` from production `.env`.

**Deliverables:** `snowflake_pool.py`, updated startup lifespan hook,
pool configuration in `settings`, key-pair auth helper.

**Blast radius:** every backend request path touches this. Do it in a
branch; run the full test suite; hit each API surface manually before
merge.

### Phase 3 — OIDC login + USERS + RBAC (4 days)

**Goal:** every request identifies the actual human calling it.

- Add `USERS` table: `id`, `email` (unique), `name`, `role`
  (`admin`/`editor`/`viewer`), `created_at`, `last_login_at`.
- Wire OIDC via Authlib (or `fastapi-users` if you want more scaffolding).
  Endpoints: `GET /auth/login` (redirect to IdP),
  `GET /auth/callback` (exchange code for token, upsert user, set signed
  cookie), `POST /auth/logout` (clear cookie).
- FastAPI dependency `get_current_user()` reads the cookie, validates the
  JWT, loads the `USERS` row.
- Dependency factory `require_role("editor")` for endpoint gating.
- Replace every `sf_session.get_cached_context().user` (currently in
  `mutes.py:54`, `rules.py:313,336,541,548`, `ai_recommendations.py`, a
  few others) with `current_user.email`.
- Bootstrap admin list from `ADMIN_EMAILS` env — on first login any email
  in that list becomes admin automatically.
- Frontend: new `/login` route, an auth context, an axios interceptor for
  401s, a user menu in the header showing the real user, and role-gated
  UI affordances.

**Deliverables:** `api/auth.py`, `services/auth_service.py`, `USERS`
migration, frontend auth context and login page, gated API endpoints.

**Not in scope:** password login, MFA (delegated to the IdP), SCIM
provisioning (users created on first login).

### Phase 4 — Audit log (1 day)

**Goal:** one uniform, queryable trail of who did what.

- Add `AUDIT_LOG` table: `id`, `actor_email`, `action`, `target_table`,
  `target_id`, `details` (VARIANT), `created_at`.
- A tiny helper `storage.append_audit(actor, action, target, details)`
  called from every state-changing endpoint (approve rule, reject rule,
  resolve finding, mute, create/edit/disable connection, save workflow,
  create schedule, run scan, execute AI fix).
- Keep the existing `APPROVED_BY` / `REJECTED_BY` / `MUTED_BY` columns —
  they're denormalized quick-access; `AUDIT_LOG` is the full history.
- Frontend: an admin-only "Audit" page filtering by actor, action, or
  target.

**Deliverables:** `AUDIT_LOG` migration, `services/audit.py`, calls added
to ~10 endpoints, frontend audit page.

### Phase 5 — Containerize + HTTPS + multi-worker (2 days)

**Goal:** the app runs the way production apps run.

- Dockerfile for backend (multi-stage: build wheel, run in slim base).
- `docker-compose.yml` for local development: backend + reverse proxy +
  Node dev server.
- Nginx / Caddy config: TLS, static frontend, `/api/*` reverse-proxy to
  the backend.
- Gunicorn config: `-k uvicorn.workers.UvicornWorker`, N workers.
- Readiness endpoint (`/ready`) that checks Snowflake pool is warm.
- Health endpoint stays as liveness (`/health`).

**Deliverables:** `Dockerfile`, `docker-compose.yml`, reverse-proxy
config, `gunicorn.conf.py`.

**Not in scope:** the deploy target itself (K8s manifests, ECS task
definitions, etc.) — that depends on where the app runs.

### Phase 6 — CI/CD + observability (2 days)

**Goal:** every change is verified automatically; every failure is
caught in production.

- GitHub Actions workflow: on PR, run `pytest`, `tsc --noEmit`,
  `docker build`. On merge to `main`, push image to registry.
- Sentry SDK wired in `main.py`, DSN from env.
- `prometheus-fastapi-instrumentator` mounted, `/metrics` endpoint.
- Structured JSON logging via `structlog` or `python-json-logger`.
- Dependabot config for `requirements.txt` and `package.json`.

**Deliverables:** `.github/workflows/ci.yml`, Sentry integration, metrics
endpoint, JSON log formatter, Dependabot config.

### Phase 7 — Data safety + secret rotation (1 day)

- `ALTER SCHEMA DQ_APP SET DATA_RETENTION_TIME_IN_DAYS = 7` (or 30 on
  Enterprise).
- Documented restore procedure using `AT (OFFSET => -N)` on `DQ_APP`
  tables. One rehearsal.
- Secret-rotation runbook: how to rotate the Snowflake key-pair, the IdP
  client secret, and the app `SECRET_KEY` without downtime.

**Deliverables:** `docs/runbooks/` with `restore.md`, `rotate-secrets.md`,
`incident-response.md`.

---

## 4. Effort summary

| Phase | Days | Depends on |
|-------|-----:|------------|
| 1. Connection rewire       | 2    | — |
| 2. Service account + pool  | 2    | 1 |
| 3. OIDC + USERS + RBAC     | 4    | 2 |
| 4. Audit log               | 1    | 3 |
| 5. Container + HTTPS       | 2    | 2 |
| 6. CI/CD + observability   | 2    | 5 |
| 7. Data safety             | 1    | any |

**Total:** ~14 working days for one developer, ~8 with two working in
parallel (phases 5+6 can run alongside phases 3+4 if you have infra help).

---

## 5. Decisions that block us from starting

Three answers are needed before Phase 1 begins. Sensible defaults are
suggested in **bold** if you don't have preferences yet.

### 5.1 Identity Provider
Which IdP does your org already run? This decides the OIDC library
config, discovery URL, and the shape of the JWT claims.

- **Okta** (default recommendation if you have a choice)
- Microsoft Entra ID (formerly Azure AD)
- Google Workspace
- Auth0
- Something else — tell me which

### 5.2 Secrets Manager
Where do secrets live?

- **AWS Secrets Manager** (default if you're on AWS)
- Azure Key Vault (default if you're on Azure)
- HashiCorp Vault (default if you're multi-cloud or on-prem)
- Google Secret Manager

### 5.3 Deployment target
Where does the app actually run?

- **ECS Fargate** on AWS (default: simple, cheap, no cluster ops)
- Kubernetes (EKS/AKS/GKE) — pick this if your org already runs k8s
- Azure Container Apps
- A VM with Docker (fine for internal-only, low-scale)
- Corporate PaaS (name it)

Once these three are answered, Phase 1 can start the same day. Phases 1
and 2 don't depend on the answers — they can start now regardless.

---

## 6. What is **not** in this plan (and why)

Deliberately omitted so this document stays actionable rather than
aspirational:

- **Per-user Snowflake sessions** — the service-account model handles
  99% of what an internal tool needs. Reconsider only if a compliance
  auditor specifically demands Snowflake-side attribution per human.
- **Snowflake OAuth** — same reason. Route to the service account.
- **Multi-tenant / multi-org support** — this is a single-org tool.
  Multi-tenant is a rewrite, not a hardening pass.
- **A rewrite of the anomaly / rule-intelligence stack** — those are
  well-factored already; leave them alone.
- **UI redesign** — production readiness is a plumbing project, not a
  design project. Ship the auth + audit first; do UI improvements
  afterward.

---

## 7. Rollout / cutover

Because you said existing data can be wiped, the cutover is simple:

1. Build phases 1-7 in a branch.
2. Stand up a staging environment with the new stack.
3. Point staging at a fresh `DQ_APP` schema in a separate Snowflake
   database (e.g. `DQ_STAGING.DQ_APP`) so nothing collides with the
   current dev deployment.
4. Run through a scripted smoke test: login, create connection, run
   scan, approve rule, mute finding, resolve finding, verify audit log
   captures each step under the correct actor.
5. Have three real users hit staging concurrently for a day. Watch
   Sentry, Prometheus, and Snowflake query logs.
6. Cut prod over to the new stack. Old dev instance goes away.

There is no data migration because you've explicitly agreed data can be
reset. If that changes later, the migration is straightforward: dump
existing `DQ_APP` tables, re-import after Phase 1 slug-column population,
and backfill `USERS` from `APPROVED_BY` / `CREATED_BY` string columns.

---

## 8. What good looks like when we're done

- Ten people can hit the same URL, log in with their own corporate
  credentials, and see their own name in the header.
- The Snowflake-side query audit log shows one service account. The
  app-side audit log shows the actual human who took each action.
- Viewers can browse; editors can act; admins can configure. Nobody can
  accidentally delete a connection with history.
- The backend runs under Gunicorn with multiple workers behind Nginx.
  Restarting the backend requires no browser SSO popup. Deployments are
  a single `docker push`.
- Every PR runs the test suite and a type-check automatically. Every
  production exception lands in Sentry. Every state change is queryable
  in the audit log.
- The Snowflake schema can be restored from Time Travel with a documented
  one-page runbook.

That is the definition-of-done for "production ready for an organization"
as it applies to this tool.
