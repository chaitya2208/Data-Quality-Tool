"""
Liquibase Changelog Parser

Extracts CREATE TABLE SQL statements from Liquibase changelog files.
Supports the two most common formats your team uses:

  1. SQL changelog  — plain .sql file with CREATE TABLE statements
     (changesets separated by --changeset author:id comments)

  2. XML changelog  — standard Liquibase XML with <createTable> elements
     (Snowflake-compatible attributes: tableName, schemaName, catalogName)

Returns a list of dicts:
  [
    {
      "sql":         "CREATE TABLE ...",
      "table_name":  "ORDERS",
      "changeset_id": "author:001",   # or None for plain SQL
      "source_file": "changelogs/v1.xml",
      "line":        42,              # approximate
    },
    ...
  ]
"""
import re
import xml.etree.ElementTree as ET
from typing import List, Dict, Optional
import logging

logger = logging.getLogger(__name__)


def parse_changelog(path: str) -> List[Dict]:
    """
    Auto-detect format and extract CREATE TABLE statements from a
    Liquibase changelog file.
    """
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        content = fh.read()

    if path.lower().endswith(".xml") or content.lstrip().startswith("<"):
        return _parse_xml(content, path)
    else:
        return _parse_sql(content, path)


def parse_sql_string(sql: str) -> List[Dict]:
    """Parse raw SQL content (no file path needed)."""
    return _parse_sql(sql, "<stdin>")


# ── XML changelog parser ──────────────────────────────────────────────────────

_NS = {
    "lb": "http://www.liquibase.org/xml/ns/dbchangelog",
}

def _parse_xml(content: str, path: str) -> List[Dict]:
    results = []
    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        logger.warning(f"[LiquibaseParser] XML parse error in {path}: {e}")
        return []

    # Handle both namespaced and non-namespaced XML
    ns = ""
    tag = root.tag
    if tag.startswith("{"):
        ns = tag[1:tag.index("}")]

    def find_all(element, local_name):
        if ns:
            return element.findall(f".//{{{ns}}}{local_name}")
        return element.findall(f".//{local_name}")

    def attr(element, name):
        return element.get(name, "")

    for changeset in find_all(root, "changeSet"):
        cs_id     = f"{attr(changeset, 'author')}:{attr(changeset, 'id')}"
        cs_line   = getattr(changeset, "sourceline", None)

        for create_table in find_all(changeset, "createTable"):
            table_name  = attr(create_table, "tableName").upper()
            schema_name = attr(create_table, "schemaName") or "UNKNOWN_SCHEMA"
            db_name     = attr(create_table, "catalogName") or "UNKNOWN_DB"
            remarks     = attr(create_table, "remarks")  # maps to COMMENT

            fqn = f"{db_name.upper()}.{schema_name.upper()}.{table_name}"

            columns = []
            for col in find_all(create_table, "column"):
                col_name    = attr(col, "name").upper()
                col_type    = _normalise_type(attr(col, "type"))
                col_remarks = attr(col, "remarks")

                not_null  = False
                for constraint in find_all(col, "constraints"):
                    if attr(constraint, "nullable") == "false":
                        not_null = True
                    if attr(constraint, "primaryKey") == "true":
                        not_null = True

                if col_name:
                    not_null_str = " NOT NULL" if not_null else ""
                    col_comment  = f" COMMENT '{col_remarks}'" if col_remarks else ""
                    columns.append(
                        f"    {col_name}  {col_type}{not_null_str}{col_comment}"
                    )

            if not columns:
                continue

            table_comment = f"\n) COMMENT = '{remarks}'" if remarks else "\n)"
            sql = (
                f"CREATE TABLE {fqn} (\n"
                + ",\n".join(columns)
                + table_comment
            )

            results.append({
                "sql":          sql,
                "table_name":   table_name,
                "changeset_id": cs_id,
                "source_file":  path,
                "line":         cs_line,
            })

    logger.info(f"[LiquibaseParser] XML: found {len(results)} createTable changeset(s) in {path}")
    return results


# ── SQL changelog parser ──────────────────────────────────────────────────────

_CHANGESET_RE = re.compile(
    r"--\s*changeset\s+([^\s:]+:[^\s]+)",
    re.IGNORECASE,
)

_CREATE_TABLE_RE = re.compile(
    r"(CREATE\s+(?:OR\s+REPLACE\s+)?(?:TRANSIENT\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"[\"'\w]+(?:\.[\"'\w]+){0,2}\s*\(.*?\)(?:\s*COMMENT\s*[=]?\s*['\"].*?['\"])?)\s*;",
    re.IGNORECASE | re.DOTALL,
)


def _parse_sql(content: str, path: str) -> List[Dict]:
    results = []

    # Find all changesets and their positions
    changesets = []
    for m in _CHANGESET_RE.finditer(content):
        changesets.append((m.start(), m.group(1)))

    if not changesets:
        # No changeset markers — treat whole file as one block
        changesets = [(0, None)]

    # Add sentinel at end
    changesets.append((len(content), None))

    for idx, (start_pos, cs_id) in enumerate(changesets[:-1]):
        end_pos  = changesets[idx + 1][0]
        chunk    = content[start_pos:end_pos]
        line_num = content[:start_pos].count("\n") + 1

        for m in _CREATE_TABLE_RE.finditer(chunk):
            sql = m.group(1).strip()

            # Extract table name for display
            name_m = re.search(
                r"TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
                r"(?:[\"'\w]+\.){0,2}[\"']?(\w+)[\"']?",
                sql, re.IGNORECASE,
            )
            table_name = name_m.group(1).upper() if name_m else "UNKNOWN"

            results.append({
                "sql":          sql,
                "table_name":   table_name,
                "changeset_id": cs_id,
                "source_file":  path,
                "line":         line_num + chunk[:m.start()].count("\n"),
            })

    logger.info(f"[LiquibaseParser] SQL: found {len(results)} CREATE TABLE(s) in {path}")
    return results


def _normalise_type(t: str) -> str:
    """Map Liquibase/JDBC types to Snowflake equivalents."""
    t = t.upper().strip()
    mappings = {
        "BIGINT":           "NUMBER(38,0)",
        "INT":              "NUMBER(38,0)",
        "INTEGER":          "NUMBER(38,0)",
        "SMALLINT":         "NUMBER(5,0)",
        "TINYINT":          "NUMBER(3,0)",
        "BOOLEAN":          "BOOLEAN",
        "BOOL":             "BOOLEAN",
        "DATETIME":         "TIMESTAMP_NTZ",
        "TIMESTAMP":        "TIMESTAMP_NTZ",
        "TEXT":             "VARCHAR",
        "CLOB":             "VARCHAR",
        "NVARCHAR":         "VARCHAR",
        "NVARCHAR2":        "VARCHAR",
        "CHAR":             "CHAR",
        "FLOAT":            "FLOAT",
        "DOUBLE":           "FLOAT",
        "REAL":             "FLOAT",
        "NUMERIC":          "NUMBER",
        "DECIMAL":          "NUMBER",
    }
    base = re.sub(r"\(.*\)", "", t).strip()
    return mappings.get(base, t)
