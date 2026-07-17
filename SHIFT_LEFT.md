# Shift-Left Data Quality Validation

Catch data-quality problems in table definitions **before** they reach Snowflake,
and keep watching the data **after** they land. Two complementary gates:

| Gate | When | Runs on | Rule types | Blocks? |
|------|------|---------|------------|---------|
| **Gate 1 — Shift-Left DDL** | Pre-commit / on PR | Parsed `CREATE TABLE` (no live table) | Metadata / convention (naming, PII, nullable IDs, type-for-name, missing PK) | No (advisory) |
| **Gate 2 — Scheduled scan** | Recurring, post-deploy | Live Snowflake tables | Data-level (uniqueness, ranges, null rates, freshness) + AI per-table rules | n/a |

**Why two gates?** Gate 1 enforces the rules that hold for *every* table — the
"convention floor" — using only the DDL text, so no table needs to exist yet.
Gate 2 discovers and enforces the rules that are unique to *this* table's data
(the per-table ceiling), which requires a live table and real rows. Gate 1
deliberately **skips `sql_template` / data-level rules** — those are Gate 2's job.

---

## Gate 1 — the endpoint

`POST /api/v1/validate/ddl`

Request:
```json
{ "sql": "CREATE TABLE DB.SCHEMA.T (...);", "fail_on": ["critical", "high"] }
```

Response (`DDLValidateResponse`):
```json
{
  "passed": false,
  "table_name": "ORDERS",
  "columns_parsed": 5,
  "rules_checked": 21,
  "findings_count": 2,
  "blocked_by": 2,
  "fail_on": ["critical", "high"],
  "findings": [
    { "rule_code": "NULLABLE_ID_COLUMN", "severity": "high",
      "column_name": "ORDER_ID", "title": "ID column ORDER_ID allows NULL values",
      "description": "..." }
  ]
}
```

How it works (`backend/app/api/validate.py` + `backend/app/services/ddl_parser.py`):
- The parser turns the `CREATE TABLE` into in-memory `SimpleNamespace` assets (no ORM, no DB writes).
- The route calls the metadata/convention check functions in `dynamic_rules.py`
  **directly** — not `run_dynamic_checks`, which drops findings for a table that
  has no approved rule instances yet (a brand-new table never does).
- `sql_template` / data-level rules are intentionally not run (they need a live table).

The checks run: missing primary key, nullable ID/PK, PII-without-masking,
date-stored-as-VARCHAR, boolean-stored-as-VARCHAR.

---

## Gate 1 — local pre-commit hook

Runs the validator on staged `*.sql` files at `git commit` time.

**Install** (once):
```bash
bash ci/hooks/install-hooks.sh     # sets core.hooksPath = ci/hooks
```

**Behavior:** soft by default — findings are printed as a warning, the commit is
**allowed**. Set `DQ_BLOCK=1` to abort commits that have blocking findings.

**Config (environment variables):**

| Var | Default | Meaning |
|-----|---------|---------|
| `DQ_URL` | `http://localhost:8000` | Backend base URL |
| `DQ_FAIL_ON` | `critical high` | Severities that count as blocking |
| `DQ_BLOCK` | `0` | `1` = abort commit on blocking findings; else warn only |

**Prerequisite:** the backend must be running (`localhost:8000`). If it's
unreachable the hook uses `--allow-offline` and skips (warns) rather than
blocking a developer who doesn't have the backend up.

**Bypass (emergency):** `git commit --no-verify`

**Run manually:**
```bash
python ci/validate_ddl.py --sql path/to/table.sql --fail-on critical high
```

---

## Gate 1 — CI advisory gate (GitHub Actions)

Lives in the migrations repo (e.g. `dq-test`), not here. On every PR/push that
changes a changelog, it validates each file and **posts a findings PR comment**
(updated in place) plus `::warning::` annotations. It is **advisory** — the
check stays green and never blocks the merge; the reviewer decides to fix or
accept before merging.

Requires a **self-hosted runner** on a machine that can reach the backend at
`localhost:8000`, plus workflow permission **Read and write** (for the comment).

---

## Gotchas (learned the hard way — do not remove)

1. **Workflow YAML must be ASCII-only.** The Windows self-hosted runner reads
   the file as cp1252; any Unicode (em-dash `—`, `✓`, `✗`, box-drawing) becomes
   mojibake that breaks PowerShell string quoting and fails the job at *parse*
   time (with misleading "missing brace" errors). Emoji is fine inside the
   Python CLI — it reconfigures stdout to UTF-8.
2. **PowerShell `Out-File -Encoding utf8` writes a BOM** that Python `json.load`
   rejects ("Unexpected UTF-8 BOM"). Read such files with `encoding="utf-8-sig"`.
3. **PR-comment posting needs the PR number**, which is empty on `push`-triggered
   runs (set only on `pull_request` events). The comment poster resolves it from
   the branch via the GitHub API when the env var is empty.
4. **`workflow_dispatch` / the "Run workflow" button default to the `main`
   branch.** Always merge workflow changes to `main`, or you run a stale file.
5. **One `CREATE TABLE` per `.sql` file.** The validator parses only the first
   `CREATE TABLE` — matches the Liquibase one-file-per-table convention.

---

## Gate 2 — scheduled scans (already built)

Pair each deployed table with a schedule so the data-level rules Gate 1 can't
check run continuously. See `backend/app/services/schedule_runner.py` and
`POST /api/v1/schedules`.
