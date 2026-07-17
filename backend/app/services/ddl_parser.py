"""
DDL Parser — converts a CREATE TABLE SQL string into in-memory asset objects
that the existing dynamic-rule checks can consume without any live table.

Supports Snowflake DDL conventions:
  CREATE [OR REPLACE] [TRANSIENT] TABLE [IF NOT EXISTS] [[db.]schema.]table (
      column_name  DATA_TYPE[(precision,scale)]  [NOT NULL]  [DEFAULT x]  [COMMENT 'x'],
      ...
  ) [COMMENT = 'table comment'];

The assets are plain `SimpleNamespace` objects shaped to match the current
storage layer (see storage._asset_from_row) — the harsh merge removed the old
SQLAlchemy `Asset` ORM model, so the check functions in dynamic_rules.py now
expect this duck-typed shape (`.fqn`, `.asset_type`, `.column_name`,
`.raw_metadata`, `.comment`, `.owner`, etc.). Assets are in-memory only and are
never written to storage; a sentinel `id` marks them as non-persistent.
"""
import re
import logging
from types import SimpleNamespace
from typing import Tuple, List, Optional

logger = logging.getLogger(__name__)

# Sentinel asset id — DDL-validation assets are never persisted, but the check
# functions read `asset.id` when building finding dicts, so give them a marker
# that is obviously not a real ASSETS row id.
_DDL_ASSET_ID = "__ddl_validate__"


class DDLParseError(ValueError):
    pass


def _make_asset(**kwargs) -> SimpleNamespace:
    """Build an in-memory asset matching storage._asset_from_row's shape.
    Only the fields the dynamic-rule checks actually read are populated with
    real values; the rest are None so attribute access never raises."""
    base = dict(
        id=_DDL_ASSET_ID,
        asset_type=None,
        database_name=None,
        schema_name=None,
        table_name=None,
        column_name=None,
        fqn=None,
        owner=None,
        comment=None,
        row_count=None,
        size_bytes=None,
        raw_metadata=None,
        created_at=None,
        updated_at=None,
        last_scanned_at=None,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def parse_create_table(sql: str) -> Tuple[SimpleNamespace, List[SimpleNamespace]]:
    """
    Parse a CREATE TABLE statement into (table_asset, [column_assets]).
    Assets are in-memory only — never added to any storage.
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

    depth = 0
    paren_end = -1
    for i, ch in enumerate(sql[paren_start:], paren_start):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                paren_end = i
                break
    if paren_end == -1:
        raise DDLParseError("Unmatched parentheses in CREATE TABLE")

    col_block = sql[paren_start + 1 : paren_end]

    # ── Extract table-level COMMENT (after the closing paren) ─────────────────
    after_paren = sql[paren_end + 1 :]
    table_comment_match = re.search(
        r"COMMENT\s*[=]?\s*['\"](.+?)['\"]", after_paren, re.IGNORECASE | re.DOTALL
    )
    table_comment = table_comment_match.group(1).strip() if table_comment_match else None

    # ── Parse columns ─────────────────────────────────────────────────────────
    column_assets: List[SimpleNamespace] = []
    for raw_line in _split_column_lines(col_block):
        col = _parse_column_line(raw_line, table_fqn, database_name, schema_name, table_name)
        if col:
            column_assets.append(col)

    if not column_assets:
        raise DDLParseError(f"No columns found in CREATE TABLE for {table_fqn}")

    # ── Build table asset ─────────────────────────────────────────────────────
    table_asset = _make_asset(
        fqn=table_fqn,
        asset_type="table",
        database_name=database_name,
        schema_name=schema_name,
        table_name=table_name,
        comment=table_comment,
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
) -> Optional[SimpleNamespace]:
    """Parse a single column definition line into a column asset."""
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
    data_type = m.group(2).upper().replace(" ", "_")  # e.g. TIMESTAMP NTZ
    rest      = m.group("rest") or ""

    # Normalise multi-word Snowflake types
    data_type = re.sub(r"TIMESTAMP\s+NTZ", "TIMESTAMP_NTZ", data_type, flags=re.I)
    data_type = re.sub(r"TIMESTAMP\s+LTZ", "TIMESTAMP_LTZ", data_type, flags=re.I)
    data_type = re.sub(r"TIMESTAMP\s+TZ",  "TIMESTAMP_TZ",  data_type, flags=re.I)
    data_type = re.sub(r"DOUBLE\s+PRECISION", "DOUBLE_PRECISION", data_type, flags=re.I)

    is_nullable = "NO" if re.search(r"\bNOT\s+NULL\b", rest, re.IGNORECASE) else "YES"

    col_comment_m = re.search(r"COMMENT\s*['\"](.+?)['\"]", rest, re.IGNORECASE)
    col_comment = col_comment_m.group(1).strip() if col_comment_m else None

    return _make_asset(
        fqn=f"{table_fqn}.{col_name}",
        asset_type="column",
        database_name=database_name,
        schema_name=schema_name,
        table_name=table_name,
        column_name=col_name,
        comment=col_comment,
        raw_metadata={
            "data_type":   data_type,
            "is_nullable": is_nullable,
        },
    )
