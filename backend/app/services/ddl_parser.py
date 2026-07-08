"""
DDL Parser — converts a CREATE TABLE SQL string into in-memory Asset objects
that the existing RuleEngine can consume without any Snowflake connection.

Supports Snowflake DDL conventions:
  CREATE [OR REPLACE] [TRANSIENT] TABLE [IF NOT EXISTS] [[db.]schema.]table (
      column_name  DATA_TYPE[(precision,scale)]  [NOT NULL]  [DEFAULT x]  [COMMENT 'x'],
      ...
  ) [COMMENT = 'table comment'];
"""
import re
import logging
from typing import Tuple, List, Optional

from app.models.asset import Asset

logger = logging.getLogger(__name__)


class DDLParseError(ValueError):
    pass


def parse_create_table(sql: str) -> Tuple[Asset, List[Asset]]:
    """
    Parse a CREATE TABLE statement into (table_asset, [column_assets]).
    Assets are in-memory only — never added to any DB session.
    Raises DDLParseError on unrecognisable input.
    """
    sql = sql.strip()

    # ── Extract fully-qualified table name ────────────────────────────────────
    header_match = re.search(
        r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:TRANSIENT\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
        r"([\"'\w]+(?:\.[\"'\w]+){0,2})",
        sql, re.IGNORECASE,
    )
    if not header_match:
        raise DDLParseError("Could not find CREATE TABLE statement in the SQL")

    raw_name = header_match.group(1).replace('"', "").replace("'", "")
    parts = raw_name.split(".")
    if len(parts) == 3:
        database_name, schema_name, table_name = parts
    elif len(parts) == 2:
        database_name, schema_name, table_name = "UNKNOWN_DB", parts[0], parts[1]
    else:
        database_name, schema_name, table_name = "UNKNOWN_DB", "UNKNOWN_SCHEMA", parts[0]

    database_name = database_name.upper()
    schema_name   = schema_name.upper()
    table_name    = table_name.upper()
    table_fqn     = f"{database_name}.{schema_name}.{table_name}"

    # ── Extract column block (between first '(' and matching ')') ─────────────
    paren_start = sql.find("(", header_match.end())
    if paren_start == -1:
        raise DDLParseError("Could not find opening parenthesis in CREATE TABLE")

    depth, pos = 0, paren_start
    for i, ch in enumerate(sql[paren_start:], paren_start):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                paren_end = i
                break
    else:
        raise DDLParseError("Unmatched parentheses in CREATE TABLE")

    col_block = sql[paren_start + 1 : paren_end]

    # ── Extract table-level COMMENT (after the closing paren) ─────────────────
    after_paren = sql[paren_end + 1 :]
    table_comment_match = re.search(
        r"COMMENT\s*[=]?\s*['\"](.+?)['\"]", after_paren, re.IGNORECASE | re.DOTALL
    )
    table_comment = table_comment_match.group(1).strip() if table_comment_match else None

    # ── Parse columns ─────────────────────────────────────────────────────────
    column_assets: List[Asset] = []
    for raw_line in _split_column_lines(col_block):
        col = _parse_column_line(raw_line, table_fqn, database_name, schema_name, table_name)
        if col:
            column_assets.append(col)

    if not column_assets:
        raise DDLParseError(f"No columns found in CREATE TABLE for {table_fqn}")

    # ── Build table Asset ─────────────────────────────────────────────────────
    table_asset = Asset(
        fqn=table_fqn,
        asset_type="table",
        database_name=database_name,
        schema_name=schema_name,
        table_name=table_name,
        comment=table_comment,
        owner=None,
        row_count=None,
        size_bytes=None,
        raw_metadata={"data_type": None, "is_nullable": None},
    )

    logger.info(
        f"[DDLParser] Parsed {table_fqn}: {len(column_assets)} columns"
        + (f", comment={repr(table_comment[:40])}" if table_comment else "")
    )
    return table_asset, column_assets


def _split_column_lines(col_block: str) -> List[str]:
    """
    Split the column block by commas, respecting nested parentheses.
    Each returned string is one column (or constraint) definition.
    """
    lines, current, depth = [], [], 0
    for ch in col_block:
        if ch == "(":
            depth += 1
            current.append(ch)
        elif ch == ")":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            line = "".join(current).strip()
            if line:
                lines.append(line)
            current = []
        else:
            current.append(ch)
    last = "".join(current).strip()
    if last:
        lines.append(last)
    return lines


_CONSTRAINT_PREFIXES = re.compile(
    r"^\s*(PRIMARY\s+KEY|FOREIGN\s+KEY|UNIQUE|CHECK|CONSTRAINT|INDEX)\b",
    re.IGNORECASE,
)

_COLUMN_RE = re.compile(
    r'^["\']?(\w+)["\']?\s+'               # column name (optionally quoted)
    r'(\w+(?:\s+\w+)?)'                    # data type (1-2 words, e.g. TIMESTAMP NTZ)
    r'(?:\s*\([^)]*\))?'                   # optional precision e.g. (38,0) or (255)
    r'(?P<rest>.*)',                        # everything else
    re.IGNORECASE,
)


def _parse_column_line(
    line: str,
    table_fqn: str,
    database_name: str,
    schema_name: str,
    table_name: str,
) -> Optional[Asset]:
    """Parse a single column definition line into a column Asset."""
    line = line.strip()
    if not line:
        return None

    # Skip table-level constraints
    if _CONSTRAINT_PREFIXES.match(line):
        return None

    m = _COLUMN_RE.match(line)
    if not m:
        logger.debug(f"[DDLParser] Skipping unrecognised line: {line[:80]}")
        return None

    col_name  = m.group(1).upper()
    data_type = m.group(2).upper().replace(" ", "_")  # e.g. TIMESTAMP_NTZ
    rest      = m.group("rest") or ""

    # Normalise multi-word Snowflake types
    data_type = re.sub(r"TIMESTAMP\s+NTZ", "TIMESTAMP_NTZ", data_type, flags=re.I)
    data_type = re.sub(r"TIMESTAMP\s+LTZ", "TIMESTAMP_LTZ", data_type, flags=re.I)
    data_type = re.sub(r"TIMESTAMP\s+TZ",  "TIMESTAMP_TZ",  data_type, flags=re.I)
    data_type = re.sub(r"DOUBLE\s+PRECISION", "DOUBLE_PRECISION", data_type, flags=re.I)

    is_nullable = "N" if re.search(r"\bNOT\s+NULL\b", rest, re.IGNORECASE) else "Y"

    col_comment_m = re.search(r"COMMENT\s*['\"](.+?)['\"]", rest, re.IGNORECASE)
    col_comment = col_comment_m.group(1).strip() if col_comment_m else None

    return Asset(
        fqn=f"{table_fqn}.{col_name}",
        asset_type="column",
        database_name=database_name,
        schema_name=schema_name,
        table_name=table_name,
        column_name=col_name,
        comment=col_comment,
        owner=None,
        raw_metadata={
            "data_type":   data_type,
            "is_nullable": is_nullable,
        },
    )
