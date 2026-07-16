"""
Schema-Wide Relationship Discovery — finds likely FK/PK relationships
between tables in the same database.schema by naming convention, then
verifies each candidate against live data with an orphan-rate check.

This exists because RuleIntelligenceAgent only ever sees one table's schema
at a time (see rule_intelligence_agent.py's docstring) — it has no candidate
ref_table/ref_column to fill in a referential_integrity check's
cross_table_ref, no matter how well it's prompted. This module supplies that
missing input: a persisted, per-schema relationship catalog Claude (and the
deterministic candidate generator in rule_intelligence_agent.py) can draw
real targets from.

Naming-convention based, not domain-specific: an FK-shaped column (ends in
_ID, not its own table's surrogate key) is matched against PK-shaped columns
(same regex dynamic_rules.py/profiler_agent.py already use) in every other
table in the schema. This generalizes to any schema using conventional
naming — it does not encode anything about what CUSTOMERS/ORDERS/etc. mean.

Cached per (database, schema) with a TTL so a schema-scope batch run only
pays discovery cost once, on the first table, not once per table.
"""
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.services import storage
from app.services.snowflake_session import session as sf_session

logger = logging.getLogger(__name__)

CACHE_TTL_HOURS = 24
MAX_LIVE_VERIFICATIONS = 50          # cap on live orphan-rate queries per discovery run
LARGE_TABLE_ROW_GUARD = 5_000_000    # above this, sample instead of a full anti-join
SAMPLE_ROWS = 10_000
ORPHAN_CONFIRM_THRESHOLD = 0.90      # orphan_rate >= this ⇒ not a real FK, mark rejected

# Same PK-shape pattern as profiler_agent.py / dynamic_rules.py.
_PK_SHAPE_RE = re.compile(r"(^ID$|_ID$|^PK_|_PK$|_KEY$|_SEQ$|_SURROGATE)", re.I)
_FK_SHAPE_RE = re.compile(r"_ID$", re.I)

# Snowflake INFORMATION_SCHEMA.DATA_TYPE values, grouped into join-compatible
# families. A name-matched FK/PK pair whose two columns fall in DIFFERENT
# families is NOT verified with a live orphan-rate join: Snowflake would
# implicitly coerce (e.g. VARCHAR '00123' == NUMBER 123) and return a
# plausible-but-meaningless orphan rate, which would then be laundered into a
# "confirmed / verified" relationship and fed to the deterministic
# referential-integrity proposal path. Only same-family pairs are trustworthy.
_TYPE_FAMILY = {
    "NUMBER": "numeric", "DECIMAL": "numeric", "NUMERIC": "numeric",
    "INT": "numeric", "INTEGER": "numeric", "BIGINT": "numeric",
    "SMALLINT": "numeric", "TINYINT": "numeric", "BYTEINT": "numeric",
    "FLOAT": "numeric", "FLOAT4": "numeric", "FLOAT8": "numeric",
    "DOUBLE": "numeric", "DOUBLE PRECISION": "numeric", "REAL": "numeric",
    "TEXT": "text", "VARCHAR": "text", "CHAR": "text", "CHARACTER": "text", "STRING": "text",
    "DATE": "temporal", "TIME": "temporal", "DATETIME": "temporal",
    "TIMESTAMP": "temporal", "TIMESTAMP_NTZ": "temporal",
    "TIMESTAMP_LTZ": "temporal", "TIMESTAMP_TZ": "temporal",
    "BOOLEAN": "boolean", "BINARY": "binary", "VARBINARY": "binary",
}


def _type_family(data_type: Optional[str]) -> Optional[str]:
    """Map a Snowflake DATA_TYPE to its join family, or None if unknown/missing.
    Strips any precision suffix (e.g. 'NUMBER(38,0)') defensively — DATA_TYPE
    from INFORMATION_SCHEMA is usually bare, but this costs nothing."""
    if not data_type:
        return None
    base = data_type.upper().split("(")[0].strip()
    return _TYPE_FAMILY.get(base)


def _types_compatible(from_type: Optional[str], to_type: Optional[str]) -> bool:
    """True unless both types are known AND fall in different families. Missing
    metadata is treated as compatible so we never over-skip a real FK just
    because a type couldn't be resolved — the fix targets the DEFINITE
    mismatch (VARCHAR vs NUMBER), not the unknown case."""
    f1, f2 = _type_family(from_type), _type_family(to_type)
    if f1 is None or f2 is None:
        return True
    return f1 == f2


def get_or_refresh_catalog(database: str, schema_name: str, force: bool = False) -> List[Any]:
    """Returns the relationship catalog for this schema, using the persisted
    cache if it's fresh enough (unless force=True)."""
    if not force:
        cached = storage.list_relationships(database, schema_name)
        if cached and _fresh_enough(cached, CACHE_TTL_HOURS):
            logger.info(f"[RelationshipDiscovery] Using cached catalog for {database}.{schema_name} ({len(cached)} rows)")
            return cached
    return _discover(database, schema_name)


def _fresh_enough(rows: List[Any], ttl_hours: int) -> bool:
    if not rows:
        return False
    oldest_check = min(r.last_verified_at for r in rows if r.last_verified_at)
    if oldest_check is None:
        return False
    if oldest_check.tzinfo is None:
        oldest_check = oldest_check.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - oldest_check < timedelta(hours=ttl_hours)


def _discover(database: str, schema_name: str) -> List[Any]:
    logger.info(f"[RelationshipDiscovery] Discovering relationships in {database}.{schema_name}")

    columns_by_table, column_types = _fetch_schema_columns(database, schema_name)
    if not columns_by_table:
        return []

    pk_shaped_by_name: Dict[str, List[str]] = {}   # table -> [_ID/_KEY/... suffixed columns]
    fk_shaped: Dict[str, List[str]] = {}           # table -> [fk-shaped column names]
    for table, cols in columns_by_table.items():
        for col in cols:
            if _PK_SHAPE_RE.search(col.upper()):
                pk_shaped_by_name.setdefault(table, []).append(col)
            if _FK_SHAPE_RE.search(col.upper()) and col.upper() not in _own_pk_names(table):
                fk_shaped.setdefault(table, []).append(col)

    # Name suffix alone can't tell "this table's own key" from "a foreign
    # key that happens to live here" (e.g. ORDERS.CUSTOMER_ID matches the
    # same _ID-suffix pattern as CUSTOMERS.CUSTOMER_ID). Resolved by relative
    # uniqueness, not an absolute cutoff: for a given column name (e.g.
    # CUSTOMER_ID), the table where it's MOST unique owns it as PK — even
    # if that table's own data has some duplicates (that's exactly the kind
    # of defect this app exists to catch; an absolute threshold would wrongly
    # disqualify a real PK for having the bug we want flagged). Guessing from
    # table-name singularization instead would also break on tables like
    # PRODUCT_CATALOG whose PK (PRODUCT_ID) doesn't share the table's name.
    pk_shaped = _resolve_pk_ownership_by_relative_uniqueness(database, schema_name, pk_shaped_by_name)

    candidates = _build_candidates(fk_shaped, pk_shaped)
    logger.info(f"[RelationshipDiscovery] {len(candidates)} name-match candidates found")

    row_counts = _fetch_row_counts(database, schema_name)

    # Accumulate result dicts, then persist the whole catalog for this schema in
    # ONE batch (delete-all-for-schema + one multi-row insert) instead of a
    # 3-round-trip upsert per candidate. See storage.replace_relationships.
    catalog_rows: List[dict] = []
    verified_count = 0
    for cand in candidates:
        if verified_count >= MAX_LIVE_VERIFICATIONS:
            catalog_rows.append({
                "from_table": cand["from_table"], "from_column": cand["from_column"],
                "to_table": cand["to_table"], "to_column": cand["to_column"],
                "status": "confirmed", "confidence": "name_match_unverified",
            })
            continue

        # Type-compatibility gate — BEFORE the live orphan-rate join. A name
        # match across incompatible types (e.g. VARCHAR CUSTOMER_ID vs NUMBER
        # CUSTOMER_ID) would be implicitly coerced by Snowflake and return a
        # plausible-but-meaningless orphan rate, which would then be laundered
        # into a "confirmed / verified" relationship and fed to the
        # deterministic referential-integrity proposal path. Record it as
        # rejected with a distinct confidence so it's visible, not silently
        # dropped — and don't spend a live verification slot on it.
        from_type = column_types.get((cand["from_table"].upper(), cand["from_column"].upper()))
        to_type = column_types.get((cand["to_table"].upper(), cand["to_column"].upper()))
        if not _types_compatible(from_type, to_type):
            logger.debug(
                f"[RelationshipDiscovery] Skipping type-incompatible candidate "
                f"{cand['from_table']}.{cand['from_column']} ({from_type}) -> "
                f"{cand['to_table']}.{cand['to_column']} ({to_type}) — not a real FK"
            )
            catalog_rows.append({
                "from_table": cand["from_table"], "from_column": cand["from_column"],
                "to_table": cand["to_table"], "to_column": cand["to_column"],
                "status": "rejected", "confidence": "type_mismatch",
            })
            continue

        orphan_result = _verify_orphan_rate(
            database, schema_name, cand["from_table"], cand["from_column"],
            cand["to_table"], cand["to_column"], row_counts,
        )
        verified_count += 1
        if orphan_result is None:
            continue  # query failed (e.g. type mismatch) — not a real candidate

        orphan_rate, total, orphans = orphan_result
        status = "rejected" if orphan_rate >= ORPHAN_CONFIRM_THRESHOLD else "confirmed"
        catalog_rows.append({
            "from_table": cand["from_table"], "from_column": cand["from_column"],
            "to_table": cand["to_table"], "to_column": cand["to_column"],
            "status": status, "confidence": "verified",
            "orphan_rate": orphan_rate, "sample_total": total, "sample_orphans": orphans,
        })

    if len(candidates) > MAX_LIVE_VERIFICATIONS:
        logger.info(
            f"[RelationshipDiscovery] Hit verification cap ({MAX_LIVE_VERIFICATIONS}) — "
            f"{len(candidates) - MAX_LIVE_VERIFICATIONS} candidates left as name_match_unverified"
        )

    # Persist the full recomputed catalog for this schema in one batch.
    return storage.replace_relationships(database, schema_name, catalog_rows)


_MIN_OWNER_RATIO = 0.5  # even the best-ranked table must be at least this unique to count as PK owner at all


def _resolve_pk_ownership_by_relative_uniqueness(
    database: str, schema_name: str, pk_shaped_by_name: Dict[str, List[str]],
) -> Dict[str, List[str]]:
    """A column name matching the PK-shape regex may appear PK-shaped in
    several tables at once (its real owner, plus every table that merely
    holds it as a foreign key — e.g. CUSTOMER_ID in CUSTOMERS/ORDERS/
    TRANSACTIONS). For each distinct column NAME, rank every table that has
    it by live uniqueness ratio (distinct/non-null) and keep only the single
    highest-ranked table as that name's PK owner — relative comparison, not
    an absolute cutoff, so a real PK with some duplicate-key defects (the
    exact thing this app exists to flag) still outranks tables that are only
    ever referencing it, so long as it's still more unique than they are."""
    ratios: Dict[str, List[tuple]] = {}  # column_name -> [(table, ratio)]
    for table, cols in pk_shaped_by_name.items():
        fqn = f"{database}.{schema_name}.{table}"
        # One scan per TABLE instead of one per column: COUNT(*) plus a
        # COUNT(DISTINCT col) for every PK-shaped column in this table, in a
        # single query. Ratio = distinct / row_count (COUNT(DISTINCT) already
        # ignores NULLs; row_count vs per-column non-null count preserves the
        # cross-table ranking used to pick the PK owner).
        select_terms = ["COUNT(*) AS TOTAL"]
        col_aliases = []  # (original_col, alias)
        for idx, col in enumerate(cols):
            alias = f"D_{idx}"
            select_terms.append(f"COUNT(DISTINCT {col}) AS {alias}")
            col_aliases.append((col, alias))
        try:
            rows = sf_session.query(f"SELECT {', '.join(select_terms)} FROM {fqn}")
        except Exception as e:
            logger.debug(f"[RelationshipDiscovery] Uniqueness check failed for {fqn}: {e}")
            continue
        if not rows:
            continue
        total = rows[0].get("TOTAL") or 0
        for col, alias in col_aliases:
            distinct = rows[0].get(alias) or 0
            ratio = distinct / total if total else 0.0
            ratios.setdefault(col.upper(), []).append((table, ratio))

    result: Dict[str, List[str]] = {}
    for col_name, table_ratios in ratios.items():
        owner_table, owner_ratio = max(table_ratios, key=lambda tr: tr[1])
        if owner_ratio >= _MIN_OWNER_RATIO:
            result.setdefault(owner_table, []).append(col_name)
    return result


def _own_pk_names(table: str) -> set:
    """Rough singularization guess for 'this table's own surrogate key',
    same heuristic dynamic_rules.py::check_fk_without_constraint uses — a
    FK-shaped column matching this is excluded from FK candidates (it's the
    table's own PK, not a reference to something else)."""
    table_upper = table.upper().rstrip("S")
    return {f"{table_upper}_ID", "SURROGATE_KEY", "ROW_ID", "RECORD_ID"}


def _build_candidates(fk_shaped: Dict[str, List[str]], pk_shaped: Dict[str, List[str]]) -> List[dict]:
    """FK-shaped column name matches a PK-shaped column name in a DIFFERENT
    table -> candidate relationship. Exact name match only (e.g.
    CUSTOMER_ID -> CUSTOMER_ID) — deliberately conservative to keep
    candidate count bounded without needing fuzzy matching."""
    candidates = []
    for from_table, fk_cols in fk_shaped.items():
        for fk_col in fk_cols:
            for to_table, pk_cols in pk_shaped.items():
                if to_table == from_table:
                    continue
                for pk_col in pk_cols:
                    if fk_col.upper() == pk_col.upper():
                        candidates.append({
                            "from_table": from_table, "from_column": fk_col,
                            "to_table": to_table, "to_column": pk_col,
                        })
    return candidates


def _fetch_schema_columns(
    database: str, schema_name: str,
) -> tuple[Dict[str, List[str]], Dict[tuple, str]]:
    """Returns (columns_by_table, types_by_table_column) from a single
    INFORMATION_SCHEMA query. The types map is keyed by (TABLE_NAME.upper(),
    COLUMN_NAME.upper()) so the caller can check FK/PK join-type compatibility
    without a second round trip."""
    try:
        rows = sf_session.query(
            f"""
            SELECT table_name AS TABLE_NAME, column_name AS COLUMN_NAME, data_type AS DATA_TYPE
            FROM {database}.INFORMATION_SCHEMA.COLUMNS
            WHERE table_schema = '{schema_name}'
            ORDER BY table_name, ordinal_position
            """
        )
    except Exception as e:
        logger.warning(f"[RelationshipDiscovery] Could not list columns for {database}.{schema_name}: {e}")
        return {}, {}

    by_table: Dict[str, List[str]] = {}
    types: Dict[tuple, str] = {}
    for r in rows:
        table, column = r["TABLE_NAME"], r["COLUMN_NAME"]
        by_table.setdefault(table, []).append(column)
        types[(table.upper(), column.upper())] = r.get("DATA_TYPE")
    return by_table, types


def _fetch_row_counts(database: str, schema_name: str) -> Dict[str, int]:
    try:
        rows = sf_session.query(f"SHOW TABLES IN {database}.{schema_name}")
    except Exception as e:
        logger.warning(f"[RelationshipDiscovery] Could not fetch row counts for {database}.{schema_name}: {e}")
        return {}
    return {
        (r.get("name") or r.get("NAME") or ""): int(r.get("rows") or r.get("ROWS") or 0)
        for r in rows
    }


def _verify_orphan_rate(
    database: str, schema_name: str,
    from_table: str, from_column: str, to_table: str, to_column: str,
    row_counts: Dict[str, int],
) -> Optional[tuple]:
    """Returns (orphan_rate, sample_total, sample_orphans), or None if the
    verification query itself fails (e.g. incompatible types — not a real
    FK candidate, just a name collision)."""
    from_fqn = f"{database}.{schema_name}.{from_table}"
    to_fqn = f"{database}.{schema_name}.{to_table}"
    from_rows = row_counts.get(from_table, 0)

    source_expr = f"{from_fqn}"
    if from_rows > LARGE_TABLE_ROW_GUARD:
        source_expr = f"(SELECT * FROM {from_fqn} SAMPLE ({SAMPLE_ROWS} ROWS))"

    try:
        rows = sf_session.query(
            f"""
            SELECT
                COUNT(*) AS TOTAL,
                COUNT_IF(t.{from_column} IS NOT NULL AND r.{to_column} IS NULL) AS ORPHANS
            FROM {source_expr} t
            LEFT JOIN {to_fqn} r ON t.{from_column} = r.{to_column}
            """
        )
    except Exception as e:
        logger.debug(f"[RelationshipDiscovery] Orphan-rate check failed for {from_fqn}.{from_column} -> {to_fqn}.{to_column}: {e}")
        return None

    if not rows:
        return None
    total = rows[0].get("TOTAL") or 0
    orphans = rows[0].get("ORPHANS") or 0
    if total == 0:
        return None
    return orphans / total, total, orphans
