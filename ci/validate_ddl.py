#!/usr/bin/env python3
"""
validate_ddl.py — Shift-Left DDL Validation CI/CD script

Validates a CREATE TABLE SQL statement against all active data quality rules
before it reaches Snowflake. Use this in your CI pipeline to block PRs that
introduce data quality issues.

Zero external dependencies — stdlib only (urllib, json, argparse, sys).

Usage:
  python validate_ddl.py --sql path/to/migration.sql
  python validate_ddl.py --sql migration.sql --url http://dq-platform:8000
  python validate_ddl.py --sql migration.sql --fail-on critical high
  cat migration.sql | python validate_ddl.py

Exit codes:
  0 — validation passed (no blocking findings)
  1 — validation failed (blocking findings found, or connection error)

GitHub Actions example:
  - name: Validate DDL
    run: python ci/validate_ddl.py --sql migrations/latest.sql

Jenkins example:
  sh 'python ci/validate_ddl.py --sql $MIGRATION_FILE --url http://dq-server:8000'
"""
import argparse
import json
import sys
import urllib.request
import urllib.error

# Windows consoles default to cp1252, which can't encode the ✓/✗/●/─/… glyphs
# this script prints — without this, a *successful* validation would crash with
# UnicodeEncodeError on the very output that matters. Reconfigure to UTF-8 with
# a lossy fallback so a finding is always shown, never a traceback. (No-op on
# already-UTF-8 terminals; reconfigure() exists on Py3.7+ TextIO streams.)
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ANSI colours (disabled automatically when stdout is not a tty)
_TTY = sys.stdout.isatty()
RED    = "\033[91m" if _TTY else ""
GREEN  = "\033[92m" if _TTY else ""
YELLOW = "\033[93m" if _TTY else ""
BOLD   = "\033[1m"  if _TTY else ""
RESET  = "\033[0m"  if _TTY else ""

SEVERITY_COLORS = {
    "critical": RED + BOLD,
    "high":     RED,
    "medium":   YELLOW,
    "low":      "",
    "info":     "",
}


class OfflineError(Exception):
    """Raised when the DQ Platform backend is unreachable."""


def call_api(base_url: str, sql: str, fail_on: list) -> dict:
    url     = base_url.rstrip("/") + "/api/v1/validate/ddl"
    payload = json.dumps({"sql": sql, "fail_on": fail_on}).encode("utf-8")
    req     = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(body).get("detail", body)
        except Exception:
            detail = body
        # A 400 means the SQL itself is malformed/unparseable — that's a real
        # validation failure the developer must fix, not a connectivity issue,
        # so it always blocks regardless of --allow-offline.
        print(f"{RED}ERROR{RESET} API returned HTTP {e.code}: {detail}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        # Connectivity problem (backend down, wrong URL). Distinct from a
        # validation failure — the caller decides whether to hard-fail or
        # warn-and-skip based on --allow-offline.
        raise OfflineError(str(e.reason))


def print_findings_table(findings: list, fail_on: list) -> None:
    if not findings:
        return

    col_w = {"severity": 10, "rule_code": 35, "column": 20, "title": 50}

    header = (
        f"{'SEVERITY':<{col_w['severity']}}  "
        f"{'RULE CODE':<{col_w['rule_code']}}  "
        f"{'COLUMN':<{col_w['column']}}  "
        f"{'TITLE':<{col_w['title']}}"
    )
    separator = "─" * len(header)

    print()
    print(BOLD + header + RESET)
    print(separator)

    for f in findings:
        sev      = f["severity"].upper()
        color    = SEVERITY_COLORS.get(f["severity"], "")
        blocking = f["severity"] in fail_on
        marker   = (RED + "●" + RESET) if blocking else " "
        rule     = f["rule_code"][:col_w["rule_code"]]
        col      = (f.get("column_name") or "")[:col_w["column"]]
        title    = f["title"][:col_w["title"]]

        print(
            f"{marker} {color}{sev:<{col_w['severity']-2}}{RESET}  "
            f"{rule:<{col_w['rule_code']}}  "
            f"{col:<{col_w['column']}}  "
            f"{title}"
        )

    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate a CREATE TABLE statement against active data quality rules.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--sql", metavar="FILE",
        help="Path to SQL file containing CREATE TABLE statement. "
             "Reads from stdin if omitted.",
    )
    parser.add_argument(
        "--url", default="http://localhost:8000",
        help="Base URL of the Data Quality Platform API (default: http://localhost:8000)",
    )
    parser.add_argument(
        "--fail-on", nargs="+", default=["critical"],
        metavar="SEVERITY",
        help="Severity levels that fail the build. "
             "Options: critical high medium low info. "
             "Default: critical",
    )
    parser.add_argument(
        "--allow-offline", action="store_true",
        help="If the Data Quality Platform is unreachable, warn and pass "
             "(exit 0) instead of failing. Use in local pre-commit hooks so "
             "developers who don't have the backend running aren't blocked. "
             "Malformed SQL still fails regardless of this flag.",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit the raw validation result as a single line of JSON to "
             "stdout (instead of the human-readable table). The JSON gets a "
             "'source_file' field added so callers can attribute findings. "
             "Use in CI to build PR comments / annotations. Exit code is "
             "unchanged (0 pass / 1 blocking).",
    )
    args = parser.parse_args()

    # Read SQL
    if args.sql:
        try:
            with open(args.sql, "r", encoding="utf-8") as fh:
                sql = fh.read()
        except OSError as e:
            print(f"{RED}ERROR{RESET} Cannot read file '{args.sql}': {e}", file=sys.stderr)
            sys.exit(1)
    elif not sys.stdin.isatty():
        sql = sys.stdin.read()
    else:
        print(f"{RED}ERROR{RESET} Provide --sql FILE or pipe SQL via stdin.", file=sys.stderr)
        parser.print_usage(sys.stderr)
        sys.exit(1)

    if not sql.strip():
        print(f"{RED}ERROR{RESET} SQL input is empty.", file=sys.stderr)
        sys.exit(1)

    fail_on = [s.lower() for s in args.fail_on]

    # In --json mode this progress line must NOT pollute stdout (the caller
    # parses stdout as JSON), so route it to stderr.
    print(f"Validating DDL against {args.url} …",
          file=sys.stderr if args.json else sys.stdout)

    try:
        result = call_api(args.url, sql, fail_on)
    except OfflineError as e:
        msg = (f"Could not connect to {args.url}: {e}. "
               "Is the Data Quality Platform running?")
        if args.allow_offline:
            print(f"{YELLOW}WARN{RESET} {msg}", file=sys.stderr)
            print(f"{YELLOW}Skipping DDL validation (--allow-offline).{RESET}",
                  file=sys.stderr)
            sys.exit(0)
        print(f"{RED}ERROR{RESET} {msg}", file=sys.stderr)
        sys.exit(1)

    # Machine-readable path: emit the raw result (annotated with the source
    # file) and exit. CI uses this to build PR comments / annotations.
    if args.json:
        result["source_file"] = args.sql or "<stdin>"
        print(json.dumps(result))
        sys.exit(0 if result.get("passed", False) else 1)

    table      = result.get("table_name", "UNKNOWN")
    cols       = result.get("columns_parsed", 0)
    rules_chk  = result.get("rules_checked", 0)
    total      = result.get("findings_count", 0)
    blocked    = result.get("blocked_by", 0)
    passed     = result.get("passed", False)
    findings   = result.get("findings", [])

    print(f"Table: {BOLD}{table}{RESET}  |  "
          f"Columns parsed: {cols}  |  Rules checked: {rules_chk}")

    if passed:
        print(f"\n{GREEN}{BOLD}✓ DDL validation passed{RESET} — "
              f"{total} finding(s), none blocking")
        sys.exit(0)
    else:
        print(f"\n{RED}{BOLD}✗ DDL validation FAILED — {blocked} blocking finding(s){RESET}")
        print_findings_table(findings, fail_on)
        print(
            f"{blocked} finding(s) blocked the build "
            f"(fail-on: {', '.join(fail_on)})"
        )
        if "critical" not in fail_on:
            pass
        elif total > blocked:
            non_blocking = total - blocked
            print(
                f"  ({non_blocking} additional finding(s) below the threshold — "
                f"run with --fail-on critical high to catch them too)"
            )
        sys.exit(1)


if __name__ == "__main__":
    main()
