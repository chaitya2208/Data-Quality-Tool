"""Apply all infra/snowflake/*.sql files, in order, against the app-owned DB.

    ./.venv/Scripts/python.exe infra/snowflake/run_migrations.py

Splits each file on ';' and runs statements one at a time (Snowflake's Python
connector executes one statement per call) -- except inside a $$...$$
scripting block (e.g. EXECUTE IMMEDIATE $$ ... $$), which is kept intact as
one statement even though it contains its own semicolons (see
_split_statements()).

Tracks which files have already been applied in CORE.SCHEMA_MIGRATIONS and
skips them on a later run. This is required, not just belt-and-suspenders:
individual statements being idempotent (CREATE ... IF NOT EXISTS, ADD COLUMN
IF NOT EXISTS) is not the same as the *file sequence as a whole* staying
replayable once a later file structurally changes what an earlier file's
statements target. Confirmed directly -- re-running the full sequence after
13_rename_and_extend_rule_tables.sql renamed APPROVED_RULES to RULE_INSTANCES
(and created a view named APPROVED_RULES) broke 04_create_rule_tables.sql's
own "CREATE TABLE IF NOT EXISTS ... APPROVED_RULES" on the second full run
("Object 'APPROVED_RULES' already exists as VIEW") -- IF NOT EXISTS doesn't
help when the name now refers to the wrong *kind* of object. Once a file has
successfully applied, it is never replayed, so this class of problem can't
recur no matter what a later file does to earlier names.
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "apps" / "backend" / "agent_Service"))

from tools.snowflake_connection import get_app_connection  # noqa: E402

SQL_DIR = Path(__file__).resolve().parent
MIGRATION_FILES = sorted(SQL_DIR.glob("[0-9][0-9]_*.sql"))


def _strip_line_comments(sql_text: str) -> str:
    # Drop everything from '--' to end of line. Safe here because these DDL
    # files contain no string literals with '--' inside them.
    lines = [ln.split("--", 1)[0] for ln in sql_text.splitlines()]
    return "\n".join(lines)


def _split_statements(sql_text: str) -> list[str]:
    """Split on ';', except inside a $$...$$ Snowflake scripting block (e.g.
    EXECUTE IMMEDIATE $$ ... $$) -- those blocks (see
    13_rename_and_extend_rule_tables.sql's guarded rename) contain their own
    semicolon-terminated statements that must reach Snowflake as one single
    statement, not be shredded by a naive split. Splits the text on the '$$'
    delimiter first to find dollar-quoted spans, then only splits on ';'
    outside those spans -- confirmed against real Snowflake to keep a
    scripting block intact while still splitting ordinary statements.
    """
    statements = []
    current = ""
    in_dollar_block = False
    for part in re.split(r"(\$\$)", sql_text):
        if part == "$$":
            in_dollar_block = not in_dollar_block
            current += part
            continue
        if in_dollar_block:
            current += part
            continue
        pieces = part.split(";")
        for idx, piece in enumerate(pieces):
            if idx < len(pieces) - 1:
                current += piece + ";"
                statements.append(current)
                current = ""
            else:
                current += piece
    if current.strip():
        statements.append(current)
    return [s.strip().rstrip(";").strip() for s in statements if s.strip()]


def _ensure_migrations_table(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS PLAYGROUND_DB.CORE.SCHEMA_MIGRATIONS (
            FILENAME   STRING,
            APPLIED_AT TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
        )
        """
    )


def _applied_filenames(cur) -> set[str]:
    cur.execute("SELECT FILENAME FROM PLAYGROUND_DB.CORE.SCHEMA_MIGRATIONS")
    return {row[0] for row in cur.fetchall()}


def main() -> None:
    if not MIGRATION_FILES:
        print(f"No migration files found in {SQL_DIR}")
        return

    conn = get_app_connection()
    cur = conn.cursor()
    try:
        _ensure_migrations_table(cur)
        already_applied = _applied_filenames(cur)

        for path in MIGRATION_FILES:
            if path.name in already_applied:
                print(f"\n-- {path.name} -- (already applied, skipping)")
                continue

            print(f"\n-- {path.name} --")
            cleaned = _strip_line_comments(path.read_text())
            statements = _split_statements(cleaned)
            for stmt in statements:
                first_line = stmt.splitlines()[0][:80]
                print(f"  running: {first_line}...")
                cur.execute(stmt)

            cur.execute(
                "INSERT INTO PLAYGROUND_DB.CORE.SCHEMA_MIGRATIONS (FILENAME) VALUES (%s)",
                (path.name,),
            )
        print("\nAll migrations applied.")
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
