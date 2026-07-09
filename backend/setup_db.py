"""
Database setup script.
Runs the DDL files in snowflake/ (schema, tables, default rule seed) against
the app's Snowflake connection, in order. Every statement is idempotent
(CREATE ... IF NOT EXISTS / INSERT ... WHERE NOT EXISTS), safe to re-run.
"""
from pathlib import Path
from app.services.snowflake_session import session as sf_session
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SQL_DIR = Path(__file__).parent / "snowflake"


def _strip_line_comment(line: str) -> str:
    """
    Strip a trailing "-- ..." comment from one line. None of these DDL
    files put -- inside a string literal, so a plain split on the first
    "--" is safe — the earlier whole-line-only check missed inline
    comments like `... NOT NULL,   -- "DATABASE" is reserved; column is ...`,
    whose semicolon got misread as a statement terminator.
    """
    idx = line.find("--")
    return line if idx == -1 else line[:idx]


def _run_sql_file(path: Path) -> None:
    logger.info(f"Running {path.name}...")
    sql_text = path.read_text(encoding="utf-8")
    cleaned = "\n".join(_strip_line_comment(line) for line in sql_text.splitlines())
    statements = [s.strip() for s in cleaned.split(";") if s.strip()]
    conn = sf_session.get_connection()
    cur = conn.cursor()
    try:
        for stmt in statements:
            cur.execute(stmt)
    finally:
        cur.close()


def setup_database():
    """Create the app schema, all tables, and seed default rules."""
    sf_session.connect()
    for sql_file in sorted(SQL_DIR.glob("*.sql")):
        _run_sql_file(sql_file)
    logger.info("Database setup completed!")


if __name__ == "__main__":
    setup_database()
