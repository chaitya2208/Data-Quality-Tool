"""Run one ad-hoc SQL query from the terminal and print the results.

Usage:
    ./.venv/Scripts/python.exe infra/snowflake/query.py "SELECT * FROM PLAYGROUND_DB.RAW.SOME_TABLE LIMIT 10"

Uses the SOURCE (read-only) connection by default. Pass --app to run against
the app-owned connection instead (for querying our own CORE/PROFILING/RULES/
ALERTS/LOGS tables). Thin CLI wrapper around run_query()/run_app_query() in
tools/snowflake_connection.py -- no connection logic lives here.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend" / "agent_Service"))

from tools.snowflake_connection import run_app_query, run_query  # noqa: E402


def main() -> None:
    args = sys.argv[1:]
    use_app = "--app" in args
    if use_app:
        args.remove("--app")

    if not args:
        print('Usage: query.py ["--app"] "SELECT ..."')
        sys.exit(1)

    sql = args[0]
    rows = run_app_query(sql) if use_app else run_query(sql)

    if not rows:
        print("OK. No rows returned.")
        return

    columns = list(rows[0].keys())

    if len(columns) <= 6:
        # Narrow result: plain aligned table.
        print(" | ".join(columns))
        for row in rows:
            print(" | ".join(str(v) for v in row.values()))
    else:
        # Wide result (e.g. SHOW TABLES has ~27 columns): one field per line,
        # grouped per row, so it's readable instead of one giant pipe-joined line.
        label_width = max(len(c) for c in columns)
        for i, row in enumerate(rows):
            print(f"--- row {i + 1} ---")
            for key, value in row.items():
                print(f"  {key.ljust(label_width)} : {value}")

    print(f"\n({len(rows)} row{'s' if len(rows) != 1 else ''})")


if __name__ == "__main__":
    main()
