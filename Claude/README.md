Business Goal:

The main user can be data engineer or admin team or someone else also

The pain that we are solving is to avoid manually writing DQ Rules which takes time and misght miss on lot of needed rules causing governance issue.

The output will be a dashboard which will give alerts based on the rules created and test on database and if failed. User can also see the passed rules in another page.

Database:

We will make it for snowflake database tool, it will be able to support any kind of data inside the snowfalke, one working properly we can scale it to other data storages.

Our system will only get read-only access of the source database from which it will be reading, but for the database created for the system where it will store its things it will have ful access

It can run sql queries on database

Its yet to decide if we will give LLM only metadata or actual data rows, I believe to use hybrid approach of data is too much we can use metadata, statistics etc but if not too high then we can go with full data or if data is very critical and needs proper evaluation then can go with full data

We can do sampling also for huge data

It can contain sensitive information like PII, customer data etc.

We can allow agent to inspect raw values

It should scan the whole account for all database and its sub objects it can access everything.

User can also manually run scan for a subset of objects.

System should learn from table : we will predefine some things but rest it should see and decide on own basd on table some decided checks etc.

Data Quality Rules:

We will have some predefined DQ Rules which are quite common. Then our orchestrator agent after profiling can decide which rules from already existing should we check against the scanned data, and simultaneously it should also create new DQ rules which are missing and run checks along with the predefined ones.

- Common Ones:
    - **Completeness**: column should not be null.
    - **Uniqueness**: ID should be unique.
    - **Validity**: value should match format/range.
    - **Freshness**: table should be updated on time.
    - **Volume**: row count should not drop/spike.
    - **Distribution**: values should not drift too much.
    - **Referential integrity**: IDs should exist in another table.
    - **Accuracy**: value should match trusted source.
    - **Consistency**: same field should match across tables.
    - **Timeliness**: data should arrive before expected time.
    - **Schema drift**: columns/types should not unexpectedly change.

Rules be generated per column, per table, per database, per pipeline, per business object all be decided by agent

We will have simple, intermediate and advanced rules.

Now we also need thresholds, the system will propose thresholds automatically and the user can modify them whenever he want. The thresholds need to be explainable by system it can give ntural language output to understand it.

Ever rule can have: Rule name, descripiton, SQL logic, Severity, Confidence Score, Reasoning

Agent decided the priority as Critical, Warning, Info or some other as per necessity

Avoid redundant rules

Propose as many possible rules we, if some are like very basic then we can tag them accordingly and give appropriate priority so attention given at very last.

Rule Recommendation:

AI creates rule by itself, profile data then ask LLM to recommend rules. We will also have some predefined rules. LLM does rank rules by tagging their priority. We can have both determinstic and fully ai generated approach we follow hybrid. 

We will have one agent called rule validator whose role is to validate the generated rule and if any problem then fix it then either approve or reject it.

Rule recommendation can use whatever it want : Metadata , profiling statistics, sample data, historical data, business glossary, existing rules, user feedback

System should learn from approved and rejections.

In rule there will be explaination so user can understand why the rule is useful

User can also include why a rule can be risky in explaination if needed

Agentic Behavior:

- Possible agent responsibilities:
    - Connect to database
    - Discover schemas/tables
    - Profile data
    - Classify columns
    - Detect patterns
    - Recommend rules
    - Generate SQL checks
    - Validate generated checks
    - Ask clarification questions
    - Rank rules
    - Create alert definitions
    - Summarize violations
    - Suggest dashboard insights

We will have specialized agents multiple, we are making multi-agent system.

- Some of possible specialized agents are:
    - Metadata discovery agent
    - Data profiling agent
    - Column classification agent
    - Rule recommendation agent
    - SQL generation agent
    - Rule validation agent
    - Alert configuration agent
    - Dashboard explanation agent

Agents can run on schedule like whenever orchestrator calls them, and orchestrator is either schedules or can be run manually also.

As of now user cannot chat with the system to ask doubt that will be implemented in later stages. Currently system will have explanation where needed.

Agent can ask user questions when needed, Human in the Loop concept.

- Agent can call tools: For example:
    - SQL execution tool
    - Metadata fetch tool
    - Profiler tool
    - Rule store tool
    - Alert store tool

As of now we wont have dynamic agent creation. Dynamic rules creation will be there but not the dynamic agent as in production it becomes very hard for debugging

If agent is unsure it can ask user the questions, and it should not wait too long for response, it should start on next task when he gets input he will complete ongoing and start the left task.

Human Approval:

We will only have admin type user. For a rule approval we can have a multi-approval flow and if the admin approves it then no other approval needed.

Rule approval screen will show a two columns separated by a small gap which will have ⇒ button in left we will have a list of rules name we will make it like a collapsible if needed user can expand or click and go to a page like rules details page and then come back and if he click ⇒ the rule will be moved into right column. After adding necessary rules those rules will become active. If the rules would have failed its alert will be created and if passed it can be seen in passed rules page.

User can also edit a rule and all its sub fields and then decide.

Store rejected rules so system can improve. User can also add comment why he rejected the rule and can also leave blank.

It can help to improve and get better future recommendations.

Approved rule can be disabled later.

In next scan system can also use this approved rules also and if they pass show them as it pased and can be removed from alert.

No audit trail as of now will implement that later.

Alert Creation:

Alert will be basically once the rules are ran and checked and if it fails and then user approve it a alert will be created. We will have alerts page and one alert dashboard to look at things easily.

We will be having alerts such a way that system will classify them, group them and all such so user can have ease.

If in next scan this rules passed alert will no longer be there.

Its just in app alerts as of now, later we can make it slack notification, email notification, raising a pager duty and all such things.

Alerts should check whether the rule which we have or created by system is it passing or failing it can be on parameters like pass/fail : It can have further things like number of failed and passed records, sample of some of records of them, percnetage failed, severity level which is again decided by system along with agent generated explaination

As of now just keep brief history can expand it later.

User can mark a alert as a false positive also as part of feedback system.

Dashboard:

Decide whats best to show on dashboard

We should have lot of filters and sorting options

Show some of the metrics and trends also.

Dashboard should be business friendly

User can open any rule/alert from here by clicking on it

User can also test a rule from ui not necessary from dashboard but some other page

No need of RBAC as of now

Storage:

For logging into snowflake we can use SSO external browser

System can store its needed things in one database created in snowflake having full access

Ensure no changes in source database like altering deleting drop update etc, it can do whatever select it want

Security and Governance:

Avoid expensive query is possible but if needed dont hesitate to use them if it gives good quality output. Claude LLM api will be used. We can send raw data to LLM right now for testing also we can ahve a differnet approach of sending masked data or statistics and such but that will be improvement phase one it work good on raw data then we can start teaking

PII should be avoided sending. We can have different logic from them

System can also propose DQ rules for security lapses if found

Audit logs will be implemented in last stage

Performance and Cost:

The tables can be 100s of GB and even TB sometimes, it can have lots of row so canning depends on table size and which approach to take. Full or profiling or sample size or incremental etc. UI can see logs overview/brief of what agent is performing at the moments so user can feel and see the progress. System can cache metadata/data for better cost and performance optimization

Acceptable cost will be decided once we start testing application

High priority tables to be analyzed first.

Accuracy and Trust:

We will have rule validator which validates and evaluates if generated rule is good enough or not. And we have feedback so a knowledgeable person can help as he flags accept and reject our system will learn from it.  We already have confidence scores/ Will decide for agent reasoning later. 

Database: Snowflake

Data Access: Metadata + Profiling + Sample rows + Full data as per requirement

Rule type: Simple + Basic + Advanced

Human Workflow: Accept/Reject/Edit full governance

Alert execution: dashboard as of now

Agent Style: Multi agent

Rule SQL: Templates + LLM Generated Fully + LLM generated parameters into template and validated

Storage: Snowflake

UI: Made using react

Backend can be Node.js or Python

A lot of all above what is say are like differentiators from existing apps.

We want it to be agentic because manager asked and also LLM bsaed rule recommmendation and multi step workflow is there

It should be reliable as a product.

- Things system can learn from table:
    - Row count
    - Column names
    - Data types
    - Null percentage
    - Distinct count
    - Min/max
    - Average
    - Standard deviation
    - Top values
    - Pattern detection
    - Freshness
    - Duplicates
    - Relationships with other tables
    - Infer business meaning from column names
    - Use comments if available
    - Understand domain
    - Ask user questions if something is unclear
    - System should learn from previous decisions of user
    - Detect column categories
        - ID column
        - Date column
        - Amount column
        - Status column
        - Email column
        - Phone column
        - Categorical column
        - Measure column


# Your Ideal Architecture Direction

At a high level:

```
React UI
   |
Node.js Backend API
   |
Agent Orchestrator
   |
------------------------------------------------
| Metadata Agent                                |
| Data Profiling Agent                          |
| PII / Sensitivity Agent                       |
| Domain Understanding Agent                    |
| Rule Recommendation Agent                     |
| SQL Generation Agent                          |
| Rule Validation Agent                         |
| Human Approval Workflow                       |
| Rule Execution / Alert Agent                  |
| Feedback Learning Agent                       |
------------------------------------------------
   |
Snowflake Source Databases  +  App-Owned Snowflake DB
```

Your app-owned Snowflake DB stores:

```
APP_CONNECTIONS
METADATA_SNAPSHOTS
TABLE_PROFILES
COLUMN_PROFILES
RECOMMENDED_RULES
APPROVED_RULES
REJECTED_RULES
RULE_EXECUTION_HISTORY
ALERTS
ALERT_VIOLATION_SAMPLES
USER_FEEDBACK
AGENT_RUN_LOGS
```

Source Snowflake databases are only queried with read-only access.

# How the Agentic Workflow Should Work

The clean workflow should be:

```
1. User connects Snowflake
2. System scans metadata
3. System profiles selected/all tables
4. PII/sensitive columns are detected
5. Agents classify tables and columns
6. Rule Recommendation Agent proposes rules
7. SQL Generation Agent creates executable checks
8. SQL Validator Agent validates rule SQL safely
9. System test-runs rule on sample/history
10. UI shows recommended rules with evidence
11. Admin approves/rejects/edits rules
12. Approved rules become active
13. Rule Execution Agent runs checks daily/manual
14. Failures create alerts
15. Dashboard shows alerts, trends, violations
16. Feedback improves future recommendations
```

This is agentic because the system performs a **multi-step reasoning and tool-using workflow**, not just one LLM call.

# What the Rule Recommendation Should Contain

Every recommended rule should have:

```
Rule Name
Rule Type
Database / Schema / Table / Column
Business Meaning
Rule Description
Rule SQL
Severity
Confidence Score
Priority Score
Reasoning / Explanation
Evidence
Threshold
Expected Failure Count
Historical Failure Frequency
False Positive Risk
Sample Violations
Approval Status
User Editable Fields
```

# How to Rank Rules: Priority vs Confidence

You asked whether to show high-priority rules first or high-confidence rules first.

Use both.

Create three scores:

```
Confidence Score = how sure the system is that the rule is logically correct

Severity = how bad it would be if this rule fails

Priority Score = combination of severity + confidence + business importance + historical
```

# The Biggest Risk in Your Current Scope

Your scope is very ambitious.

These parts are too heavy for first MVP:

```
Scan entire Snowflake account
Understand full domain automatically
Handle all object types
Generate all basic/intermediate/advanced rules
Dynamic agent creation
Full dashboard customization
Learning feedback loop
Governance workflow
Historical trend analysis
Run checks on 100GB+ tables
```

So the smart MVP should be:

## MVP 1

```
Snowflake only
Selected database/schema/table scan first
Metadata scan for all objects
Profiling for selected/high-priority tables
Predefined DQ templates
LLM rule recommendations
SQL validator
Human approve/reject/edit
Manual rule execution
Dashboard alerts
Store results in app Snowflake DB
```

## MVP 2

```
Scheduled execution
Historical thresholds
Trend dashboard
Feedback learning
PII-safe prompting
Table health score
Rule priority scoring
```

## MVP 3

```
Account-wide scans
Multi-step approval
Slack/PagerDuty
Chat with system
Cross-database support
Advanced domain learning
Audit logs
RBAC
```

This is how you keep the project realistic while still sounding powerful.

# What Changes in Production-Level Version

For production, you would need:

```
RBAC
Audit logs
Secret manager
Query cost controls
Warehouse limits
Timeouts
Sampling strategy
PII masking
Data access policy
Rule versioning
Alert lifecycle
Incident integrations
Retry handling
Agent run observability
Prompt/version tracking
Human approval history
Multi-tenant isolation
LLM fallback strategy
Evaluation framework
```

For internship MVP, you can explain these as future production requirements without building all of them.

# Recommended Tech Stack

## Best stack for your project

| Layer | Tech |
| --- | --- |
| Frontend | React + TypeScript |
| UI Library | Material UI / Ant Design / Tailwind + shadcn |
| Backend API | Node.js + Express/NestJS |
| Agent Service | Python + LangGraph |
| LLM | Claude |
| Source DB | Snowflake |
| App DB | Separate Snowflake database |
| Background Jobs | Python worker / queue-based jobs |
| Auth | Snowflake SSO / external browser auth for dev |
| Charts | Recharts / ECharts |
| Validation | SQL parser + dry-run queries + safety checks |
| Deployment MVP | Docker Compose |
| Production later | Kubernetes / ECS / internal platform |

# Whole System Architecture

```
┌────────────────────────────────────────────┐
│                React Frontend              │
│                                            │
│  - Connection screen                       │
│  - Database explorer                       │
│  - Scan/profiling progress                 │
│  - Recommended rules page                  │
│  - Rule approval/edit page                 │
│  - Alert dashboard                         │
│  - Rule execution history                  │
└─────────────────────┬──────────────────────┘
                      │
                      ▼
┌────────────────────────────────────────────┐
│              Node.js Backend API            │
│                                            │
│  - Auth/session handling                   │
│  - User/API routes                         │
│  - Rule CRUD APIs                          │
│  - Alert APIs                              │
│  - Dashboard APIs                          │
│  - Calls Python Agent Service              │
└─────────────────────┬──────────────────────┘
                      │
                      ▼
┌────────────────────────────────────────────┐
│          Python Agent Service               │
│              LangGraph                      │
│                                            │
│  - Agent orchestrator graph                │
│  - Metadata agent                          │
│  - Profiler agent                          │
│  - PII/sensitivity agent                   │
│  - Domain understanding agent              │
│  - Rule recommendation agent               │
│  - SQL generation agent                    │
│  - SQL validator agent                     │
│  - Rule execution agent                    │
│  - Alert explanation agent                 │
└─────────────────────┬──────────────────────┘
                      │
                      ▼
┌────────────────────────────────────────────┐
│              Tool Layer                     │
│                                            │
│  - Snowflake metadata tool                 │
│  - Snowflake profiler tool                 │
│  - Safe SQL executor tool                  │
│  - SQL validator tool                      │
│  - PII detector tool                       │
│  - Rule template engine                    │
│  - LLM client                              │
└─────────────────────┬──────────────────────┘
                      │
                      ▼
┌────────────────────────────────────────────┐
│             Snowflake Layer                 │
│                                            │
│  Source Snowflake Account                  │
│  - Read-only access                        │
│  - INFORMATION_SCHEMA                      │
│  - ACCOUNT_USAGE                           │
│  - Tables/views/sample data                │
│                                            │
│  App-Owned Snowflake DB                    │
│  - Metadata snapshots                      │
│  - Profiles                                │
│  - Recommended rules                       │
│  - Approved rules                          │
│  - Rejected rules                          │
│  - Execution results                       │
│  - Alerts                                  │
│  - Feedback                                │
└────────────────────────────────────────────┘
```

## Layer 1: React Frontend

This is what the admin/data engineer sees.

Pages:

```
1. Login / Snowflake connection
2. Database explorer
3. Scan configuration
4. Scan progress
5. Table profile page
6. Recommended rules page
7. Rule approval/edit page
8. Active rules page
9. Manual rule execution page
10. Alert dashboard
11. Alert detail page
12. Feedback/rejection reason page
```

Important UI idea:

The user should not see “agent complexity.” They should see:

```
Database scanned
Tables profiled
Rules recommended
Rules awaiting approval
Alerts generated
```

---

## Layer 2: Node.js Backend API

Node backend should handle normal application logic.

Responsibilities:

```
- User session handling
- API routes for frontend
- Snowflake connection setup
- Fetch dashboard data
- Fetch recommended rules
- Save rule approval/rejection/edit
- Trigger agent workflows
- Trigger manual rule execution
- Return agent progress logs to UI
```

Example API routes:

```
POST   /api/connections/snowflake/test
POST   /api/scans/start
GET    /api/scans/:scanId/status
GET    /api/databases
GET    /api/databases/:db/schemas
GET    /api/tables/:tableId/profile
GET    /api/rules/recommended
PATCH  /api/rules/:ruleId
POST   /api/rules/:ruleId/approve
POST   /api/rules/:ruleId/reject
POST   /api/rules/:ruleId/test
GET    /api/alerts
GET    /api/alerts/:alertId
POST   /api/alerts/:alertId/accept
POST   /api/alerts/:alertId/false-positive
```

Snowflake’s Node.js driver supports authentication flows including external browser SSO and token caching options, which matters because you mentioned SSO/external browser login.

---

## Layer 3: Python LangGraph Agent Service

This is the brain of the system.

It should expose APIs like:

```
POST /agent/scan
POST /agent/recommend-rules
POST /agent/validate-rule
POST /agent/run-rule
POST /agent/explain-alert
```

But internally, it uses LangGraph.

LangGraph should manage:

```
- Workflow state
- Which agent runs next
- When to call tools
- When to pause for human approval
- Retry/failure handling
- Progress tracking
- Agent memory/checkpoints
```

# LangGraph Agent Workflow

Your main graph should look like this:

```
START
  |
  ▼
Metadata Discovery Agent
  |
  ▼
Object Prioritization Agent
  |
  ▼
Data Profiling Agent
  |
  ▼
PII / Sensitivity Agent
  |
  ▼
Domain Understanding Agent
  |
  ▼
Rule Recommendation Agent
  |
  ▼
SQL Generation Agent
  |
  ▼
SQL Validation Agent
  |
  ▼
Rule Test Execution Agent
  |
  ▼
Human Approval Pause
  |
  ├── Approve ───────► Activate Rule
  ├── Edit ──────────► Revalidate SQL ─► Activate Rule
  └── Reject ────────► Store Feedback
  |
  ▼
Rule Execution Scheduler
  |
  ▼
Alert Creation Agent
  |
  ▼
Dashboard Update
  |
  ▼
END
```

This is better than a single agent because every part has a clear responsibility.

# Agents You Should Build

## 1. Orchestrator Agent

Main controller.

It decides:

```
- Which step runs next
- Whether enough info exists
- Whether to ask human
- Whether to retry
- Whether to skip expensive profiling
- Whether a rule is ready for approval
```

But do not let it directly run dangerous SQL.

---

## 2. Metadata Discovery Agent

Reads Snowflake metadata.

It collects:

```
- Databases
- Schemas
- Tables
- Views
- Columns
- Data types
- Comments
- Row counts if available
- Constraints if available
- Object ownership
- Last altered time
```

Uses:

```
INFORMATION_SCHEMA
ACCOUNT_USAGE
SHOW DATABASES
SHOW SCHEMAS
SHOW TABLES
DESCRIBE TABLE
```

---

## 3. Data Profiling Agent

Profiles tables and columns.

It calculates:

```
- Row count
- Null percentage
- Distinct count
- Min/max
- Average
- Standard deviation
- Top values
- Pattern frequency
- Date range
- Duplicate count
- Freshness
- Historical row count trend
```

Important: this agent should use **hybrid profiling**.

```
Small table  → full scan
Large table  → sample first
Important table → deeper scan
Very large table → approximate stats
```

---

## 4. PII / Sensitivity Agent

Detects sensitive data before sending anything to LLM.

It checks:

```
- Email-like values
- Phone-like values
- PAN-like values
- Aadhaar-like values
- Name-like columns
- Address-like columns
- Financial/account identifiers
```

Output:

```
Column sensitivity:
LOW / MEDIUM / HIGH

LLM sharing policy:
ALLOW_STATS_ONLY
ALLOW_MASKED_SAMPLE
BLOCK_RAW_SAMPLE
```

This is important because your company may not allow raw data to be sent outside the environment.

---

## 5. Domain Understanding Agent

Infers what the table means.

Example:

```
CUSTOMER table → customer master data
ORDERS table → transaction/order data
TRADE_PRICE table → market/trading data
EMPLOYEE table → HR data
```

It uses:

```
- Table names
- Column names
- Comments
- Relationships
- Data profile
- Query history later
- User clarification if needed
```

---

## 6. Rule Recommendation Agent

This is the most important agent.

It recommends rules using two sources:

```
1. Predefined rule templates
2. LLM-generated domain-specific rules
```

Example rule types:

```
Completeness
Uniqueness
Validity
Freshness
Volume
Distribution drift
Referential integrity
Schema drift
Range checks
Accepted values
Business consistency
Historical anomaly checks
```

Output should be structured JSON, not free text.

Example:

```
{
  "rule_name":"CUSTOMER_ID should not be null",
  "rule_type":"COMPLETENESS",
  "database":"DEV_DB",
  "schema":"RAW",
  "table":"CUSTOMER",
  "column":"CUSTOMER_ID",
  "severity":"CRITICAL",
  "confidence":0.94,
  "priority":0.91,
  "reason":"CUSTOMER_ID appears to be a primary business identifier.",
  "evidence": [
"Column name ends with _ID",
"Null percentage is currently 0%",
"Distinct count is close to row count"
  ],
  "requires_human_review":false
}
```

---

## 7. SQL Generation Agent

This agent converts rule definitions into SQL.

But do not let it generate everything freely.

Use this order:

```
1. Template SQL if rule is common
2. LLM SQL only if template cannot handle it
3. Validator checks all SQL before execution
```

Example template rule:

```
SELECTCOUNT(*)AS failed_count
FROM {{table_fqn}}
WHERE {{column_name}}ISNULL;
```

For complex rules, LLM can help generate SQL, but the SQL must be validated before execution.

---

## 8. SQL Validator Agent

This is mandatory.

It checks:

```
- SQL is SELECT-only
- No INSERT
- No UPDATE
- No DELETE
- No MERGE
- No DROP
- No ALTER
- No CREATE
- No COPY INTO
- No CALL unsafe procedure
- Table names are allowed
- Query has timeout/limit where needed
- Query can be dry-run/tested
```

This protects your source database.

---

## 9. Rule Test Execution Agent

Before showing a rule for approval, test it.

It should calculate:

```
- Would this rule pass/fail now?
- How many rows fail?
- Failure percentage
- Sample failed rows, masked if needed
- Estimated alert frequency if history exists
```

This makes your project much stronger than simple rule recommendation.

---

## 10. Human Approval Node

This is where LangGraph is useful.

The graph can pause and wait for the admin.

Admin can:

```
Approve
Reject
Edit
Change severity
Change threshold
Change schedule
Mark false positive
Give rejection reason
```

LangGraph supports human-in-the-loop interruptions, where execution can pause and wait for external input before continuing.

---

## 11. Alert Creation Agent

Once approved rules run, failures become alerts.

Alert fields:

```
Alert ID
Rule ID
Database
Schema
Table
Column
Severity
Status
Failed count
Failure percentage
Sample failed records
Generated explanation
Created timestamp
Last checked timestamp
```

Alert status for MVP:

```
OPEN
ACCEPTED
REJECTED
FALSE_POSITIVE
```

Threshold should be decided mainly by the **Rule Recommendation Agent**, but not alone.

Best design:

```
Data Profiling Agent gives evidence
        ↓
Rule Recommendation Agent proposes threshold
        ↓
Threshold Scoring/Validation logic checks it
        ↓
SQL Validation + Test Execution Agent tests it
        ↓
Human approves/edits final threshold
```

So the final answer is:

> **Rule Recommendation Agent proposes the threshold, but Data Profiling Agent provides the data, Rule Test Execution Agent validates it, and human admin has final control.**
> 

# Folder Structure

Use a monorepo.

```
agentic-dq-platform/
│
├── README.md
├── docker-compose.yml
├── .env.example
├── package.json
├── pnpm-workspace.yaml
│
├── apps/
│   │
│   ├── web/
│   │   ├── package.json
│   │   ├── vite.config.ts
│   │   ├── tsconfig.json
│   │   └── src/
│   │       ├── main.tsx
│   │       ├── App.tsx
│   │       │
│   │       ├── pages/
│   │       │   ├── DashboardPage.tsx
│   │       │   ├── ConnectionsPage.tsx
│   │       │   ├── DatabaseExplorerPage.tsx
│   │       │   ├── ScanRunsPage.tsx
│   │       │   ├── TableProfilePage.tsx
│   │       │   ├── RecommendedRulesPage.tsx
│   │       │   ├── RuleApprovalPage.tsx
│   │       │   ├── ActiveRulesPage.tsx
│   │       │   ├── AlertsPage.tsx
│   │       │   └── AlertDetailPage.tsx
│   │       │
│   │       ├── components/
│   │       │   ├── layout/
│   │       │   ├── tables/
│   │       │   ├── charts/
│   │       │   ├── rules/
│   │       │   ├── alerts/
│   │       │   └── common/
│   │       │
│   │       ├── api/
│   │       │   ├── client.ts
│   │       │   ├── connections.api.ts
│   │       │   ├── scans.api.ts
│   │       │   ├── profiles.api.ts
│   │       │   ├── rules.api.ts
│   │       │   └── alerts.api.ts
│   │       │
│   │       ├── hooks/
│   │       ├── store/
│   │       ├── types/
│   │       └── utils/
│   │
│   ├── api/
│   │   ├── package.json
│   │   ├── tsconfig.json
│   │   └── src/
│   │       ├── server.ts
│   │       ├── app.ts
│   │       │
│   │       ├── config/
│   │       │   ├── env.ts
│   │       │   └── snowflake.config.ts
│   │       │
│   │       ├── routes/
│   │       │   ├── connections.routes.ts
│   │       │   ├── scans.routes.ts
│   │       │   ├── metadata.routes.ts
│   │       │   ├── profiles.routes.ts
│   │       │   ├── rules.routes.ts
│   │       │   ├── alerts.routes.ts
│   │       │   └── dashboard.routes.ts
│   │       │
│   │       ├── controllers/
│   │       │   ├── connections.controller.ts
│   │       │   ├── scans.controller.ts
│   │       │   ├── rules.controller.ts
│   │       │   ├── alerts.controller.ts
│   │       │   └── dashboard.controller.ts
│   │       │
│   │       ├── services/
│   │       │   ├── snowflake.service.ts
│   │       │   ├── agent.service.ts
│   │       │   ├── scan.service.ts
│   │       │   ├── rule.service.ts
│   │       │   ├── alert.service.ts
│   │       │   └── dashboard.service.ts
│   │       │
│   │       ├── repositories/
│   │       │   ├── scan.repository.ts
│   │       │   ├── profile.repository.ts
│   │       │   ├── rule.repository.ts
│   │       │   ├── alert.repository.ts
│   │       │   └── feedback.repository.ts
│   │       │
│   │       ├── middleware/
│   │       ├── validators/
│   │       ├── types/
│   │       └── utils/
│   │
│   └── agent-service/
│       ├── pyproject.toml
│       ├── requirements.txt
│       ├── Dockerfile
│       └── src/
│           ├── main.py
│           ├── config.py
│           │
│           ├── api/
│           │   ├── routes.py
│           │   ├── scan_routes.py
│           │   ├── rule_routes.py
│           │   └── alert_routes.py
│           │
│           ├── graphs/
│           │   ├── dq_scan_graph.py
│           │   ├── rule_recommendation_graph.py
│           │   ├── rule_execution_graph.py
│           │   └── human_approval_graph.py
│           │
│           ├── state/
│           │   ├── scan_state.py
│           │   ├── rule_state.py
│           │   └── alert_state.py
│           │
│           ├── agents/
│           │   ├── orchestrator_agent.py
│           │   ├── metadata_agent.py
│           │   ├── profiler_agent.py
│           │   ├── pii_agent.py
│           │   ├── domain_agent.py
│           │   ├── rule_recommendation_agent.py
│           │   ├── sql_generation_agent.py
│           │   ├── sql_validation_agent.py
│           │   ├── rule_execution_agent.py
│           │   ├── alert_agent.py
│           │   └── feedback_agent.py
│           │
│           ├── tools/
│           │   ├── snowflake_metadata_tool.py
│           │   ├── snowflake_profiler_tool.py
│           │   ├── safe_sql_executor_tool.py
│           │   ├── pii_detection_tool.py
│           │   ├── sql_validator_tool.py
│           │   ├── rule_template_tool.py
│           │   └── llm_tool.py
│           │
│           ├── prompts/
│           │   ├── rule_recommendation_prompt.py
│           │   ├── domain_understanding_prompt.py
│           │   ├── sql_generation_prompt.py
│           │   ├── sql_validation_prompt.py
│           │   └── alert_explanation_prompt.py
│           │
│           ├── rule_engine/
│           │   ├── templates/
│           │   │   ├── completeness_rules.py
│           │   │   ├── uniqueness_rules.py
│           │   │   ├── validity_rules.py
│           │   │   ├── freshness_rules.py
│           │   │   ├── volume_rules.py
│           │   │   └── drift_rules.py
│           │   │
│           │   ├── rule_ranker.py
│           │   ├── confidence_scorer.py
│           │   ├── severity_scorer.py
│           │   └── rule_deduplicator.py
│           │
│           ├── snowflake/
│           │   ├── connection.py
│           │   ├── metadata_queries.py
│           │   ├── profiling_queries.py
│           │   ├── rule_execution_queries.py
│           │   └── app_db_queries.py
│           │
│           ├── schemas/
│           │   ├── scan_models.py
│           │   ├── profile_models.py
│           │   ├── rule_models.py
│           │   ├── alert_models.py
│           │   └── feedback_models.py
│           │
│           ├── services/
│           │   ├── scan_service.py
│           │   ├── recommendation_service.py
│           │   ├── validation_service.py
│           │   ├── execution_service.py
│           │   └── alert_service.py
│           │
│           └── utils/
│               ├── logging.py
│               ├── masking.py
│               ├── sql_safety.py
│               └── cost_guard.py
│
├── packages/
│   ├── shared-types/
│   │   └── src/
│   └── shared-config/
│
├── infra/
│   ├── docker/
│   ├── snowflake/
│   │   ├── create_app_database.sql
│   │   ├── create_core_tables.sql
│   │   ├── create_profile_tables.sql
│   │   ├── create_rule_tables.sql
│   │   ├── create_alert_tables.sql
│   │   └── create_log_tables.sql
│   └── terraform/
│
├── docs/
│   ├── architecture.md
│   ├── agent-workflow.md
│   ├── rule-generation.md
│   ├── sql-safety.md
│   ├── pii-handling.md
│   ├── dashboard.md
│   └── production-roadmap.md
│
└── tests/
    ├── api/
    ├── agent-service/
    ├── integration/
    └── fixtures/
```

# Simplified Folder Structure for MVP

The full one above is production-like. For internship MVP, start smaller:

```
agentic-dq-platform/
│
├── apps/
│   ├── web/                  # React frontend
│   ├── api/                  # Node backend
│   └── agent-service/        # Python LangGraph service
│
├── infra/
│   └── snowflake/            # App DB table creation SQL
│
├── docs/
│   ├── architecture.md
│   └── mvp-scope.md
│
└── README.md
```

Inside `agent-service`, start with only these:

```
agent-service/
└── src/
    ├── main.py
    ├── graphs/
    │   └── dq_graph.py
    ├── agents/
    │   ├── metadata_agent.py
    │   ├── profiler_agent.py
    │   ├── rule_recommender_agent.py
    │   ├── sql_generator_agent.py
    │   ├── sql_validator_agent.py
    │   └── alert_agent.py
    ├── tools/
    │   ├── snowflake_tool.py
    │   ├── profiler_tool.py
    │   ├── sql_validator_tool.py
    │   └── llm_tool.py
    ├── rule_engine/
    │   ├── templates.py
    │   └── scorer.py
    └── schemas/
        ├── rule.py
        ├── profile.py
        └── alert.py
```

This is enough to build a working demo.