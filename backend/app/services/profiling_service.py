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

from app.services.datasources.base import DataSource

logger = logging.getLogger(__name__)

# Type-prefix families for category detection. Union of Snowflake + Postgres
# spellings so detect_category() is dialect-agnostic (Snowflake: NUMBER/VARCHAR/
# TIMESTAMP_NTZ; Postgres: integer/character varying/timestamp without time zone).
_NUMERIC_TYPE_PREFIXES = ("NUMBER", "DECIMAL", "INT", "FLOAT", "DOUBLE",
                          "SMALLINT", "INTEGER", "BIGINT", "NUMERIC", "REAL", "SERIAL", "MONEY")
_DATE_TYPE_PREFIXES    = ("DATE", "TIME", "TIMESTAMP")
_TEXT_TYPE_PREFIXES    = ("VARCHAR", "CHAR", "STRING", "TEXT", "CHARACTER")

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

    from app.services import settings_service
    cat_threshold = settings_service.get_categorical_max_distinct()

    # Numeric: amount (by name) vs generic measure
    if _is(_NUMERIC_TYPE_PREFIXES):
        if any(h in name for h in _AMOUNT_NAME_HINTS):
            return "amount"
        # A numeric column with very few distinct values is really categorical
        if distinct_count is not None and distinct_count <= cat_threshold:
            return "categorical"
        return "measure"

    # Text: low cardinality → categorical, else free text
    if _is(_TEXT_TYPE_PREFIXES):
        if distinct_count is not None and distinct_count <= max(cat_threshold * 2, 30):
            return "categorical"
        return "text"

    return "text"


# Regexes for pattern-match validation of email/phone columns.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_RE = re.compile(r"^\+?[0-9][0-9\-\s().]{6,}$")

def get_columns_with_pk(source: DataSource, database: str, schema: str, table: str) -> List[Dict[str, Any]]:
    """Column list + key flags, delegated to the resolved DataSource adapter."""
    return source.list_columns(database, schema, table)


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


def _pattern_match_pct(source: DataSource, database: str, schema: str, table: str,
                       column: str, kind: str) -> Optional[float]:
    """% of non-null values matching an email/phone shape, via a value sample."""
    rx = _EMAIL_RE if kind == "email" else _PHONE_RE
    try:
        vals = [str(v) for v in source.sample_values(database, schema, table, column, limit=200) if v is not None]
        if not vals:
            return None
        matched = sum(1 for v in vals if rx.match(v.strip()))
        return round(matched / len(vals) * 100, 1)
    except Exception:
        return None


def get_table_info(source: DataSource, database: str, schema: str, table: str) -> Dict[str, Any]:
    """Table-level metadata (row count, size, kind, owner, comment) via the adapter."""
    return source.table_info(database, schema, table)


def profile_table(source: DataSource, database: str, schema: str, table: str) -> Dict[str, Any]:
    """
    Profile every column of one table over the FULL dataset — every row, exact
    stats, no sampling. Dialect-specific SQL lives in the DataSource adapter; the
    orchestration (category detection, top-values gating, anomaly hints) is here.
    """
    logger.info(f"[Profiling] Full-dataset profiling {database}.{schema}.{table}")

    from app.services import settings_service
    top_values_cap = settings_service.get_top_values_max_distinct()
    outlier_mult = settings_service.get_outlier_stddev_mult()

    # Hold one source connection open for the whole pass — profiling issues a
    # dozen+ queries per table, and reconnecting per query is slow over RDS
    # (a TLS handshake each). No-op for sources that already reuse a session.
    with source.profiling_session():
        return _profile_table_body(source, database, schema, table, top_values_cap, outlier_mult)


def _profile_table_body(source, database, schema, table, top_values_cap, outlier_mult):
    columns = get_columns_with_pk(source, database, schema, table)
    column_profiles: List[Dict[str, Any]] = []

    # One batched full-table scan per ~40 columns covers every scalar stat.
    all_aggs = source.column_stats(database, schema, table, columns)
    row_count = next((a["total"] for a in all_aggs.values() if a.get("total")), 0)

    for col_meta in columns:
        column_name = col_meta["column_name"]
        data_type = col_meta["data_type"] or ""
        type_prefix = data_type.split("(")[0].strip().upper()

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
                    and distinct_count <= top_values_cap
                ):
                    top_values = source.top_values(database, schema, table, column_name)

            # Category-specific extras (kept cheap — only when relevant)
            duplicate_count = None
            if category == "id":
                duplicate_count = source.duplicate_count(database, schema, table, column_name)
            pattern_match_pct = None
            if category in ("email", "phone"):
                pattern_match_pct = _pattern_match_pct(source, database, schema, table, column_name, category)

            # Outlier hint for numerics: max is many stddevs above the mean.
            outlier_hint = None
            if agg["avg_value"] is not None and agg["stddev"]:
                try:
                    if abs(float(agg["max_value"]) - float(agg["avg_value"])) > outlier_mult * float(agg["stddev"]):
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

    info = get_table_info(source, database, schema, table)

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
