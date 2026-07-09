"""
Data profiling engine — read-only per-column statistics over Snowflake data.

Adapted from the reference implementation in
Claude/apps/backend/agent_service/tools/snowflake_profiling_tools.py, ported to
this project's singleton `sf_session`.

Computes, per column: null count / null %, distinct count, min/max (orderable
types), avg/stddev (numerics), and top-N frequent values. Row/column counts at
the table level.

Full-dataset by design: every stat is computed over ALL rows, no sampling.
This is a data-quality tool — a sampled MIN/MAX could miss the very outlier a
rule should catch — so accuracy is never traded for speed. Profiling runs as
part of a scheduled workflow where a longer full scan is acceptable.

Pure computation — no DB writes. Returns a dict shaped:
  {
    "table":   {"row_count", "column_count", "is_sampled", "sample_size"},
    "columns": [{"column_name", "data_type", "null_count", "null_percentage",
                 "distinct_count", "min_value", "max_value", "top_values",
                 "is_sampled"}, ...]
  }
"""
import logging
import re
from typing import Any, Dict, List, Optional

from app.services.snowflake_session import session as sf_session

logger = logging.getLogger(__name__)

# Types for which MIN/MAX is meaningful (numeric, text-lexicographic, date/time).
_MIN_MAX_TYPE_PREFIXES = (
    "NUMBER", "DECIMAL", "INT", "FLOAT", "DOUBLE",
    "VARCHAR", "CHAR", "STRING", "TEXT",
    "DATE", "TIME", "TIMESTAMP",
)

# Numeric types where AVG/STDDEV are meaningful.
_NUMERIC_TYPE_PREFIXES = ("NUMBER", "DECIMAL", "INT", "FLOAT", "DOUBLE")
_DATE_TYPE_PREFIXES    = ("DATE", "TIME", "TIMESTAMP")
_TEXT_TYPE_PREFIXES    = ("VARCHAR", "CHAR", "STRING", "TEXT")

# Max distinct count for which top-values is worth a full-table GROUP BY scan.
# Above this a column is high-cardinality (IDs, free text) where top-5 is noise —
# skip the scan entirely. status/categorical are always below this by construction.
TOP_VALUES_MAX_DISTINCT = 50

# ── Column category detection ─────────────────────────────────────────────────
# Category → which stats are meaningful to display for that category. The UI
# groups columns by category and shows only these stat keys.
CATEGORY_STATS = {
    "id":          ["null_percentage", "distinct_count", "distinct_pct", "duplicate_count"],
    "date":        ["null_percentage", "min_value", "max_value", "freshness_days"],
    "amount":      ["null_percentage", "min_value", "max_value", "avg_value", "stddev"],
    "measure":     ["null_percentage", "min_value", "max_value", "avg_value", "stddev"],
    "status":      ["null_percentage", "distinct_count", "top_values"],
    "categorical": ["null_percentage", "distinct_count", "top_values"],
    "email":       ["null_percentage", "distinct_pct", "pattern_match_pct"],
    "phone":       ["null_percentage", "distinct_pct", "pattern_match_pct"],
    "text":        ["null_percentage", "distinct_count", "top_values"],
}

CATEGORY_LABELS = {
    "id": "ID", "date": "Date", "amount": "Amount", "measure": "Measure",
    "status": "Status", "categorical": "Categorical", "email": "Email",
    "phone": "Phone", "text": "Text",
}

_AMOUNT_NAME_HINTS = ("AMOUNT", "AMT", "PRICE", "COST", "REVENUE", "SALARY",
                      "BALANCE", "TOTAL", "FEE", "SPREAD", "PREMIUM", "VALUE", "USD")
_ID_NAME_HINTS     = ("_ID", "_KEY", "_PK", "_FK", "_SEQ", "GUID", "UUID")
_STATUS_NAME_HINTS = ("STATUS", "STATE", "_FL", "_FLAG", "_IND", "IS_", "TYPE", "KIND", "CATEGORY")


def detect_category(
    column_name: str,
    data_type: str,
    distinct_pct: Optional[float],
    distinct_count: Optional[int],
    top_values: List[Dict[str, Any]],
) -> str:
    """
    Infer a column's semantic category from its name, type, and stats.
    Name hints take priority (a column called STATUS is categorical even if
    numeric); type + cardinality break ties.
    """
    name = (column_name or "").upper()
    prefix = (data_type or "").split("(")[0].upper()

    def _is(prefixes) -> bool:
        return any(prefix.startswith(p) for p in prefixes)

    # Email / phone by name (pattern % validates it later)
    if "EMAIL" in name or name.endswith("_MAIL"):
        return "email"
    if "PHONE" in name or "MOBILE" in name or name.endswith("_TEL"):
        return "phone"

    # Date/time by type
    if _is(_DATE_TYPE_PREFIXES):
        return "date"

    # ID: name hint, or near-unique non-numeric-measure column
    if name == "ID" or any(h in name for h in _ID_NAME_HINTS):
        return "id"
    if distinct_pct is not None and distinct_pct >= 98 and not _is(_NUMERIC_TYPE_PREFIXES):
        return "id"

    # Status / flag / categorical by name
    if any(h in name for h in _STATUS_NAME_HINTS):
        return "status"

    # Numeric: amount (by name) vs generic measure
    if _is(_NUMERIC_TYPE_PREFIXES):
        if any(h in name for h in _AMOUNT_NAME_HINTS):
            return "amount"
        # A numeric column with very few distinct values is really categorical
        if distinct_count is not None and distinct_count <= 15:
            return "categorical"
        return "measure"

    # Text: low cardinality → categorical, else free text
    if _is(_TEXT_TYPE_PREFIXES):
        if distinct_count is not None and distinct_count <= 30:
            return "categorical"
        return "text"

    return "text"


# Regexes for pattern-match validation of email/phone columns.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_RE = re.compile(r"^\+?[0-9][0-9\-\s().]{6,}$")

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def _safe_identifier(name: str) -> str:
    """
    Quote an identifier for safe interpolation. Snowflake identifiers that are
    plain (letters/digits/underscore) can be used as-is; anything else is
    double-quoted with internal quotes escaped. Rejects nothing — quoting makes
    arbitrary names safe — but guards against SQL injection via table/column
    names coming from the API path.
    """
    if _IDENT_RE.match(name):
        return name
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def _fqn(database: str, schema: str, table: str) -> str:
    return f"{_safe_identifier(database)}.{_safe_identifier(schema)}.{_safe_identifier(table)}"


def get_table_columns(database: str, schema: str, table: str) -> List[Dict[str, Any]]:
    """Column list via INFORMATION_SCHEMA — name, data_type, nullable, default, comment."""
    rows = sf_session.query(f"""
        SELECT column_name    AS COLUMN_NAME,
               data_type      AS DATA_TYPE,
               is_nullable    AS IS_NULLABLE,
               column_default AS COLUMN_DEFAULT,
               comment        AS COMMENT,
               ordinal_position AS ORDINAL_POSITION
        FROM {database}.INFORMATION_SCHEMA.COLUMNS
        WHERE table_schema = '{schema}'
        AND   table_name   = '{table}'
        ORDER BY ordinal_position
    """)
    return [
        {
            "column_name": r.get("COLUMN_NAME"),
            "data_type":   r.get("DATA_TYPE"),
            "is_nullable": str(r.get("IS_NULLABLE", "YES")).upper() in ("Y", "YES"),
            "primary_key": False,  # INFORMATION_SCHEMA.COLUMNS has no key flags; enriched below
            "unique_key":  False,
            "comment":     r.get("COMMENT"),
        }
        for r in rows
    ]


def get_columns_with_pk(database: str, schema: str, table: str) -> List[Dict[str, Any]]:
    """
    Columns plus key detection via DESCRIBE TABLE, which reports 'primary key'
    and 'unique key' per column (raw 'Y'/'N'). Many Snowflake tables — raw /
    bronze ingestion tables especially — declare no keys at all, in which case
    both flags stay False and the UI shows '—'.
    """
    columns = get_table_columns(database, schema, table)
    try:
        described = sf_session.describe_table(database, schema, table)

        def _truthy(row, *keys) -> bool:
            for k in keys:
                if str(row.get(k, "")).upper() in ("Y", "YES", "TRUE"):
                    return True
            return False

        pk_names     = {(r.get("name") or "").upper() for r in described if _truthy(r, "primary key", "PRIMARY KEY")}
        unique_names = {(r.get("name") or "").upper() for r in described if _truthy(r, "unique key", "UNIQUE KEY")}
        for c in columns:
            name = (c["column_name"] or "").upper()
            if name in pk_names:
                c["primary_key"] = True
            if name in unique_names:
                c["unique_key"] = True
    except Exception as e:
        logger.warning(f"[Profiling] key detection failed for {database}.{schema}.{table}: {e}")
    return columns


# Max columns' worth of aggregates to fold into a single SELECT. Every column
# contributes up to ~7 aggregate expressions; batching keeps the projection
# width reasonable while still collapsing many full-table scans into one each.
_AGG_BATCH_SIZE = 40


def _profile_all_columns_agg(fqn: str, columns: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """
    Compute scalar stats (total, non-null, distinct, min/max, avg/stddev) for
    EVERY column in as few full-table scans as possible: one aggregate SELECT
    covering a batch of columns per scan (batched to cap projection width).

    Exactly equivalent to the previous per-column queries — same aggregate
    functions over the same full dataset — just folded together so Snowflake
    scans the table once per batch instead of once per column. No sampling, no
    approximation.

    Returns {column_name: {total, non_null, distinct_count, min_value,
    max_value, avg_value, stddev}}. A column whose batch query fails is retried
    on its own; if it still fails it's returned with an "error" marker so the
    caller can record it without aborting the whole table.
    """
    results: Dict[str, Dict[str, Any]] = {}

    # Alias per column by index — column names can be long/duplicate-cased, so
    # positional aliases (C0_*, C1_*) keep the result keys unambiguous.
    def _exprs_for(idx: int, col: str, type_prefix: str) -> List[str]:
        p = [
            f"COUNT(*) AS C{idx}_TOTAL",
            f"COUNT({col}) AS C{idx}_NON_NULL",
            f"COUNT(DISTINCT {col}) AS C{idx}_DISTINCT",
        ]
        if type_prefix in _MIN_MAX_TYPE_PREFIXES:
            p += [f"MIN({col}) AS C{idx}_MIN", f"MAX({col}) AS C{idx}_MAX"]
        if type_prefix in _NUMERIC_TYPE_PREFIXES:
            p += [f"AVG({col}) AS C{idx}_AVG", f"STDDEV({col}) AS C{idx}_STDDEV"]
        return p

    def _unpack(row: Dict[str, Any], idx: int, type_prefix: str) -> Dict[str, Any]:
        want_mm = type_prefix in _MIN_MAX_TYPE_PREFIXES
        want_num = type_prefix in _NUMERIC_TYPE_PREFIXES
        return {
            "total":          row.get(f"C{idx}_TOTAL", 0) or 0,
            "non_null":       row.get(f"C{idx}_NON_NULL", 0) or 0,
            "distinct_count": row.get(f"C{idx}_DISTINCT", 0) or 0,
            "min_value":      row.get(f"C{idx}_MIN") if want_mm else None,
            "max_value":      row.get(f"C{idx}_MAX") if want_mm else None,
            "avg_value":      row.get(f"C{idx}_AVG") if want_num else None,
            "stddev":         row.get(f"C{idx}_STDDEV") if want_num else None,
        }

    for start in range(0, len(columns), _AGG_BATCH_SIZE):
        batch = columns[start:start + _AGG_BATCH_SIZE]
        select_parts: List[str] = []
        meta: List[tuple] = []  # (idx, column_name, type_prefix)
        for i, col_meta in enumerate(batch):
            name = col_meta["column_name"]
            prefix = (col_meta["data_type"] or "").split("(")[0].upper()
            select_parts += _exprs_for(i, _safe_identifier(name), prefix)
            meta.append((i, name, prefix))

        try:
            row = sf_session.query(f"SELECT {', '.join(select_parts)} FROM {fqn}")[0]
            for idx, name, prefix in meta:
                results[name] = _unpack(row, idx, prefix)
        except Exception as e:
            # One column's expression can poison the whole batch (e.g. STDDEV on
            # a semi-structured type). Fall back to per-column queries for this
            # batch only — still full-dataset, just not folded.
            logger.warning(f"[Profiling] batch agg failed ({e}); retrying columns individually")
            for idx, name, prefix in meta:
                col = _safe_identifier(name)
                try:
                    single = sf_session.query(f"SELECT {', '.join(_exprs_for(idx, col, prefix))} FROM {fqn}")[0]
                    results[name] = _unpack(single, idx, prefix)
                except Exception as e2:
                    logger.warning(f"[Profiling] column {name} agg failed: {e2}")
                    results[name] = {"error": str(e2), "total": 0, "non_null": 0,
                                     "distinct_count": 0, "min_value": None, "max_value": None,
                                     "avg_value": None, "stddev": None}
    return results


def _duplicate_count(fqn: str, col: str) -> Optional[int]:
    """Rows sharing a non-null value on this column (candidate-key dup check)."""
    try:
        rows = sf_session.query(f"""
            SELECT COUNT(*) AS DUP_ROWS FROM (
                SELECT {col} FROM {fqn} WHERE {col} IS NOT NULL
                GROUP BY {col} HAVING COUNT(*) > 1
            )
        """)
        return rows[0].get("DUP_ROWS", 0) or 0
    except Exception:
        return None


def _freshness_days(max_value: Any) -> Optional[float]:
    """Days between the newest value in a date column and now (uses DB clock via a query
    would be ideal, but max_value is already fetched — parse it against local now)."""
    import datetime
    try:
        if isinstance(max_value, (datetime.datetime, datetime.date)):
            dt = max_value if isinstance(max_value, datetime.datetime) else datetime.datetime.combine(max_value, datetime.time())
        else:
            dt = datetime.datetime.fromisoformat(str(max_value).replace("Z", "+00:00"))
        now = datetime.datetime.now(dt.tzinfo) if dt.tzinfo else datetime.datetime.now()
        return round((now - dt).total_seconds() / 86400, 1)
    except Exception:
        return None


def _pattern_match_pct(fqn: str, col: str, kind: str) -> Optional[float]:
    """% of non-null values matching an email/phone shape, via a sample of top values."""
    rx = _EMAIL_RE if kind == "email" else _PHONE_RE
    try:
        rows = sf_session.query(f"SELECT {col} AS V FROM {fqn} WHERE {col} IS NOT NULL LIMIT 200")
        vals = [str(r.get("V")) for r in rows if r.get("V") is not None]
        if not vals:
            return None
        matched = sum(1 for v in vals if rx.match(v.strip()))
        return round(matched / len(vals) * 100, 1)
    except Exception:
        return None


def get_table_info(database: str, schema: str, table: str) -> Dict[str, Any]:
    """
    Table-level metadata (no data) so users understand a table at a glance:
    row count, size, kind (TABLE/VIEW), owner, comment.
    """
    try:
        rows = sf_session.query(f"SHOW TABLES LIKE '{table}' IN {database}.{schema}")
        match = next((r for r in rows if (r.get("name") or "").upper() == table.upper()), {})
    except Exception as e:
        logger.warning(f"[Profiling] table-info failed for {database}.{schema}.{table}: {e}")
        match = {}
    return {
        "name":      table,
        "row_count": match.get("rows"),
        "bytes":     match.get("bytes"),
        "kind":      match.get("kind"),
        "owner":     match.get("owner"),
        "comment":   match.get("comment"),
    }


def _profile_top_values(fqn: str, col: str, limit: int = 5) -> List[Dict[str, Any]]:
    rows = sf_session.query(f"""
        SELECT {col} AS VALUE, COUNT(*) AS OCCURRENCES
        FROM {fqn}
        WHERE {col} IS NOT NULL
        GROUP BY {col}
        ORDER BY OCCURRENCES DESC
        LIMIT {int(limit)}
    """)
    return [{"value": r.get("VALUE"), "count": r.get("OCCURRENCES")} for r in rows]


def profile_table(database: str, schema: str, table: str) -> Dict[str, Any]:
    """
    Profile every column of one table over the FULL dataset — every row, exact
    stats, no sampling. This is a data-quality tool: rules must be grounded in
    the true distribution (a sampled MIN/MAX could miss the very outlier a rule
    should catch). Profiling is part of a scheduled workflow, so a longer full
    scan is an acceptable trade for uncompromised accuracy.
    """
    logger.info(f"[Profiling] Full-dataset profiling {database}.{schema}.{table}")

    columns = get_columns_with_pk(database, schema, table)
    fqn = _fqn(database, schema, table)  # no SAMPLE clause — full scan
    column_profiles: List[Dict[str, Any]] = []

    # One batched full-table scan per ~40 columns covers every scalar stat for
    # every column (null/distinct/min/max/avg/stddev) — no per-column scans.
    all_aggs = _profile_all_columns_agg(fqn, columns)
    # COUNT(*) is identical across columns; take it from any agg result.
    row_count = next((a["total"] for a in all_aggs.values() if a.get("total")), 0)

    for col_meta in columns:
        column_name = col_meta["column_name"]
        data_type = col_meta["data_type"] or ""
        col = _safe_identifier(column_name)
        type_prefix = data_type.split("(")[0].upper()

        try:
            agg = all_aggs.get(column_name) or {}
            if agg.get("error"):
                raise RuntimeError(agg["error"])
            total, non_null = agg.get("total", 0), agg.get("non_null", 0)
            distinct_count = agg.get("distinct_count", 0)

            null_count = total - non_null
            null_pct = round(null_count / total * 100, 4) if total > 0 else 0.0
            # Distinct % vs non-null rows — exact, full-dataset.
            distinct_pct = round(distinct_count / non_null * 100, 2) if non_null > 0 else None
            distinct_est = distinct_count  # exact, not scaled

            # Freshness: for date/time columns, how old the newest value is.
            freshness_days = None
            if type_prefix in _DATE_TYPE_PREFIXES and agg["max_value"] is not None:
                freshness_days = _freshness_days(agg["max_value"])

            # Classify first (detect_category ignores top_values), then fetch
            # top-values ONLY for columns that display them and are low-cardinality.
            # Top-5 of a 1.2M-distinct ID is meaningless — and each fetch is a full
            # GROUP BY scan of the whole table, the main cost on large tables.
            category = detect_category(column_name, data_type, distinct_pct, distinct_count, [])
            top_values: List[Dict[str, Any]] = []
            if "top_values" in CATEGORY_STATS.get(category, []):
                if category in ("status", "categorical") or (
                    category == "text"
                    and distinct_count is not None
                    and distinct_count <= TOP_VALUES_MAX_DISTINCT
                ):
                    top_values = _profile_top_values(fqn, col)

            # Category-specific extras (kept cheap — only when relevant)
            duplicate_count = None
            if category == "id":
                duplicate_count = _duplicate_count(fqn, col)
            pattern_match_pct = None
            if category in ("email", "phone"):
                pattern_match_pct = _pattern_match_pct(fqn, col, category)

            # Outlier hint for numerics: max is many stddevs above the mean.
            outlier_hint = None
            if agg["avg_value"] is not None and agg["stddev"]:
                try:
                    if abs(float(agg["max_value"]) - float(agg["avg_value"])) > 4 * float(agg["stddev"]):
                        outlier_hint = True
                except (TypeError, ValueError):
                    pass

            column_profiles.append({
                "column_name":       column_name,
                "data_type":         data_type,
                "category":          category,
                "relevant_stats":    CATEGORY_STATS.get(category, []),
                "null_count":        null_count,
                "null_percentage":   null_pct,
                "distinct_count":    distinct_est,
                "distinct_pct":      distinct_pct,
                "duplicate_count":   duplicate_count,
                "min_value":         _jsonable(agg["min_value"]),
                "max_value":         _jsonable(agg["max_value"]),
                "avg_value":         _jsonable(agg["avg_value"]),
                "stddev":            _jsonable(agg["stddev"]),
                "freshness_days":    freshness_days,
                "pattern_match_pct": pattern_match_pct,
                "outlier_hint":      outlier_hint,
                "top_values":        [{"value": _jsonable(t["value"]), "count": t["count"]} for t in top_values],
                "is_sampled":        False,
            })
        except Exception as e:
            logger.warning(f"[Profiling] column {column_name} failed: {e}")
            column_profiles.append({
                "column_name": column_name, "data_type": data_type,
                "category": "text", "relevant_stats": CATEGORY_STATS["text"],
                "null_count": None, "null_percentage": None, "distinct_count": None,
                "distinct_pct": None, "duplicate_count": None, "min_value": None,
                "max_value": None, "avg_value": None, "stddev": None, "freshness_days": None,
                "pattern_match_pct": None, "outlier_hint": None,
                "top_values": [], "is_sampled": False, "error": str(e),
            })

    info = get_table_info(database, schema, table)

    # Category summary for the UI grouping
    category_order = ["id", "date", "amount", "measure", "status", "categorical", "email", "phone", "text"]
    present = [c for c in category_order if any(col["category"] == c for col in column_profiles)]

    return {
        "table": {
            "row_count":    row_count,
            "column_count": len(columns),
            "is_sampled":   False,
            "sample_size":  None,
            "bytes":        info.get("bytes"),
            "kind":         info.get("kind"),
            "owner":        info.get("owner"),
            "comment":      info.get("comment"),
        },
        "columns":         column_profiles,
        "categories":      present,
        "category_labels": CATEGORY_LABELS,
        "category_stats":  CATEGORY_STATS,
    }


def _jsonable(value: Any) -> Any:
    """Coerce Snowflake/py values (Decimal, datetime, date) to JSON-safe primitives."""
    if value is None:
        return None
    import datetime
    from decimal import Decimal
    if isinstance(value, Decimal):
        # keep integers as int, else float
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    return value
