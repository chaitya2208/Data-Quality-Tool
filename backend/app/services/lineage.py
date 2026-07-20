"""
Data lineage service — fetches Snowflake object dependencies + data-flow
edges and normalizes them into LINEAGE_EDGES rows. Everything the /api/v1/
lineage router exposes ultimately flows through here.

Discovery strategy (per connection):
  1. Probe SNOWFLAKE.CORE.GET_LINEAGE availability (Enterprise + VIEW LINEAGE).
     Result cached in LINEAGE_CAPABILITY_CACHE for 24h.
  2. If available → per-object GET_LINEAGE crawl (covers views, CTAS, COPY INTO,
     dynamic tables, stages — critical for the medallion S3→raw edge).
  3. Otherwise → ACCOUNT_USAGE.OBJECT_DEPENDENCIES fallback (view→table only;
     misses COPY INTO / stage loads; up to 3h latency).

Graph endpoints (build_*) read entirely from the cached LINEAGE_EDGES table
so the page is snappy — a full refresh is on-demand via POST /refresh/{db}.
"""
from __future__ import annotations

import datetime
import logging
from collections import defaultdict, deque
from typing import Any, Dict, List, Optional, Tuple

import snowflake.connector.errors  # type: ignore

from app.services import storage
from app.services.connection_types import ConnectionType
from app.services.snowflake_session import session as sf_session

logger = logging.getLogger(__name__)


CAPABILITY_TTL = datetime.timedelta(hours=24)
CRAWL_HOPS = 1  # per-object GET_LINEAGE distance — we walk from every node


# ─────────────────────────────────────────────────────────────────────────
# Availability probe
# ─────────────────────────────────────────────────────────────────────────

_UNAVAILABLE_PATTERNS = (
    "unknown function",
    "does not exist or not authorized",
    "insufficient privileges",
    "enterprise edition",
    "not supported for your account",
)


def _looks_unavailable(err_text: str) -> bool:
    lower = (err_text or "").lower()
    return any(p in lower for p in _UNAVAILABLE_PATTERNS)


def probe_get_lineage_available(
    connection_id: str, database: str, force: bool = False,
) -> Tuple[bool, Optional[str]]:
    """Returns (available, error_text). Probes against the ACTUAL database
    being refreshed — not the SNOWFLAKE shared DB, which many non-admin
    roles can't read (that would cache a false "unavailable" and force the
    OBJECT_DEPENDENCIES fallback forever).

    Cached per-connection for 24h unless force=True; POST /refresh forces a
    re-probe so admin-granted privileges take effect on the next click."""
    cached = storage.get_lineage_capability(connection_id)
    if cached and not force and cached.probed_at:
        age = datetime.datetime.utcnow() - cached.probed_at.replace(tzinfo=None)
        if age < CAPABILITY_TTL:
            return bool(cached.get_lineage_available), cached.probe_error

    # Pick any real table in the database as the probe target.
    # INFORMATION_SCHEMA.TABLES inside the *target* DB always exists for any
    # role with USAGE on that DB.
    probe_sql = (
        f"SELECT COUNT(*) AS CT FROM TABLE(SNOWFLAKE.CORE.GET_LINEAGE("
        f"'{database}.INFORMATION_SCHEMA.TABLES', 'TABLE', 'UPSTREAM', 1))"
    )
    try:
        sf_session.query(probe_sql)
        storage.set_lineage_capability(connection_id, True, None)
        return True, None
    except snowflake.connector.errors.ProgrammingError as e:
        msg = str(e)
        if _looks_unavailable(msg):
            storage.set_lineage_capability(connection_id, False, msg)
            return False, msg
        raise


# ─────────────────────────────────────────────────────────────────────────
# Edge normalization — one canonical row shape for both discovery sources
# ─────────────────────────────────────────────────────────────────────────

_KIND_MAP = {
    "TABLE": "table",
    "VIEW": "view",
    "MATERIALIZED VIEW": "materialized_view",
    "DYNAMIC TABLE": "dynamic_table",
    "EXTERNAL TABLE": "external_table",
    "ICEBERG TABLE": "iceberg_table",
    "STAGE": "stage",
    "EXTERNAL LOCATION": "external_location",
    "SEMANTIC VIEW": "semantic_view",
}


def _normalize_domain(domain: Optional[str]) -> str:
    if not domain:
        return "table"
    return _KIND_MAP.get(domain.upper(), domain.lower())


def _fqn(db: str, sc: str, tb: str) -> str:
    return f"{db}.{sc}.{tb}"


def _edge_key(row: dict) -> Tuple[str, str, str]:
    return (row["source_fqn"], row["target_fqn"], row.get("edge_type") or "")


def _dedupe_edges(rows: List[dict]) -> List[dict]:
    seen: Dict[Tuple[str, str, str], dict] = {}
    for r in rows:
        seen[_edge_key(r)] = r  # last-write-wins; all sources produce the same shape
    return list(seen.values())


# ─────────────────────────────────────────────────────────────────────────
# Fetchers — GET_LINEAGE (preferred) and OBJECT_DEPENDENCIES (fallback)
# ─────────────────────────────────────────────────────────────────────────

def _list_objects_in_database(database: str) -> List[Tuple[str, str, str, str]]:
    """Returns [(schema, name, kind, fqn), ...] for tables + views in the DB.
    Uses INFORMATION_SCHEMA which every role with USAGE can read."""
    out: List[Tuple[str, str, str, str]] = []
    try:
        rows = sf_session.query(
            f"""
            SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE
            FROM {database}.INFORMATION_SCHEMA.TABLES
            WHERE TABLE_SCHEMA NOT IN ('INFORMATION_SCHEMA')
            """
        )
        for r in rows:
            sc = r["TABLE_SCHEMA"]
            nm = r["TABLE_NAME"]
            tt = (r.get("TABLE_TYPE") or "").upper()
            kind = "view" if "VIEW" in tt else "table"
            out.append((sc, nm, kind, _fqn(database, sc, nm)))
    except Exception as e:
        logger.warning(f"[lineage] enumerate tables failed for {database}: {e}")
    return out


def _snowflake_table_type_to_kind(t: str) -> str:
    """Map INFORMATION_SCHEMA.TABLES.TABLE_TYPE to our normalized kind."""
    t = (t or "").upper()
    if "MATERIALIZED" in t and "VIEW" in t:
        return "materialized_view"
    if "DYNAMIC" in t:
        return "dynamic_table"
    if "EXTERNAL" in t:
        return "external_table"
    if "ICEBERG" in t:
        return "iceberg_table"
    if "VIEW" in t:
        return "view"
    return "table"


def enumerate_full_catalog(database: str) -> List[dict]:
    """Enumerate every schema + every table/view in `database`. Returns
    LINEAGE_CATALOG row dicts (kind='schema' rows for each schema, plus
    kind='table'/'view'/etc for each object). Excludes INFORMATION_SCHEMA."""
    rows_out: List[dict] = []

    # Schemas — one row per schema so empty schemas still render.
    try:
        schema_rows = sf_session.query(
            f"""
            SELECT SCHEMA_NAME, COMMENT
            FROM {database}.INFORMATION_SCHEMA.SCHEMATA
            WHERE SCHEMA_NAME NOT IN ('INFORMATION_SCHEMA')
            """
        )
        for r in schema_rows:
            sc = r["SCHEMA_NAME"]
            rows_out.append({
                "object_kind": "schema",
                "database_name": database, "schema_name": sc, "table_name": None,
                "fqn": f"{database}.{sc}", "comment": r.get("COMMENT"),
            })
    except Exception as e:
        logger.warning(f"[lineage] enumerate schemas failed for {database}: {e}")

    # Tables / views / etc. — INFORMATION_SCHEMA.TABLES also lists
    # views + external + iceberg + dynamic + materialized. ROW_COUNT / BYTES
    # are populated for tables.
    try:
        table_rows = sf_session.query(
            f"""
            SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE, ROW_COUNT, BYTES, COMMENT
            FROM {database}.INFORMATION_SCHEMA.TABLES
            WHERE TABLE_SCHEMA NOT IN ('INFORMATION_SCHEMA')
            """
        )
        for r in table_rows:
            sc = r["TABLE_SCHEMA"]
            nm = r["TABLE_NAME"]
            kind = _snowflake_table_type_to_kind(r.get("TABLE_TYPE") or "")
            rows_out.append({
                "object_kind": kind,
                "database_name": database, "schema_name": sc, "table_name": nm,
                "fqn": _fqn(database, sc, nm),
                "row_count": r.get("ROW_COUNT"),
                "size_bytes": r.get("BYTES"),
                "comment": r.get("COMMENT"),
            })
    except Exception as e:
        logger.warning(f"[lineage] enumerate tables failed for {database}: {e}")

    return rows_out


def list_snowflake_databases() -> List[str]:
    """All databases the current role can see. Uses SHOW DATABASES which every
    role with any grant can run."""
    try:
        rows = sf_session.query("SHOW DATABASES")
        # SHOW returns dict rows with lowercase-ish keys via DictCursor —
        # snowflake-connector keeps the exact column names from SHOW output.
        out = []
        for r in rows:
            name = r.get("name") or r.get("NAME")
            if name and name.upper() != "SNOWFLAKE":
                out.append(name)
        return sorted(out)
    except Exception as e:
        logger.warning(f"[lineage] SHOW DATABASES failed: {e}")
        return []


def fetch_lineage_via_get_lineage(
    database: str,
) -> Tuple[List[dict], List[str]]:
    """Per-object GET_LINEAGE(UPSTREAM, 1). Walks every table + view in the
    DB — 1-hop upstream from each node yields every incoming edge exactly
    once, so we don't dedupe with the downstream side. Returns
    (edges, partial_failures[fqn])."""
    objects = _list_objects_in_database(database)
    edges: List[dict] = []
    partial_failures: List[str] = []
    total_rows_returned = 0

    logger.info(f"[lineage] GET_LINEAGE crawl starting: {len(objects)} objects in {database}")

    for schema, name, kind, fqn in objects:
        domain = "VIEW" if kind == "view" else "TABLE"
        try:
            rows = sf_session.query(
                f"""
                SELECT * FROM TABLE(SNOWFLAKE.CORE.GET_LINEAGE(
                    '{fqn}', '{domain}', 'UPSTREAM', {CRAWL_HOPS}))
                """
            )
            total_rows_returned += len(rows)
        except snowflake.connector.errors.ProgrammingError as e:
            partial_failures.append(f"{fqn}: {e}")
            continue

        for r in rows:
            # GET_LINEAGE returns SOURCE_* / TARGET_* columns. Column names
            # vary slightly across Snowflake releases — accept both DOMAIN
            # and OBJECT_DOMAIN, both DATABASE and OBJECT_DATABASE, etc.
            src_db = r.get("SOURCE_OBJECT_DATABASE") or r.get("SOURCE_DATABASE")
            src_sc = r.get("SOURCE_OBJECT_SCHEMA") or r.get("SOURCE_SCHEMA")
            src_nm = r.get("SOURCE_OBJECT_NAME") or r.get("SOURCE_NAME")
            src_dm = r.get("SOURCE_OBJECT_DOMAIN") or r.get("SOURCE_DOMAIN")
            tgt_db = r.get("TARGET_OBJECT_DATABASE") or r.get("TARGET_DATABASE") or database
            tgt_sc = r.get("TARGET_OBJECT_SCHEMA") or r.get("TARGET_SCHEMA") or schema
            tgt_nm = r.get("TARGET_OBJECT_NAME") or r.get("TARGET_NAME") or name
            tgt_dm = r.get("TARGET_OBJECT_DOMAIN") or r.get("TARGET_DOMAIN") or domain

            if not src_db or not src_nm:
                continue

            edges.append({
                "source_database": src_db, "source_schema": src_sc, "source_table": src_nm,
                "source_fqn": _fqn(src_db, src_sc or "", src_nm),
                "source_kind": _normalize_domain(src_dm),
                "target_database": tgt_db, "target_schema": tgt_sc, "target_table": tgt_nm,
                "target_fqn": _fqn(tgt_db, tgt_sc or "", tgt_nm),
                "target_kind": _normalize_domain(tgt_dm),
                "edge_type": _edge_type_from_domains(src_dm, tgt_dm),
                "discovery_source": "get_lineage",
                "evidence": {
                    "query_id": r.get("QUERY_ID"),
                    "distance": r.get("DISTANCE"),
                    "source_status": r.get("SOURCE_STATUS"),
                },
            })

    deduped = _dedupe_edges(edges)
    logger.info(
        f"[lineage] GET_LINEAGE crawl done for {database}: "
        f"{len(objects)} objects · {total_rows_returned} raw rows · "
        f"{len(edges)} kept · {len(deduped)} deduped · "
        f"{len(partial_failures)} object failures"
    )
    if partial_failures and len(partial_failures) <= 5:
        for f in partial_failures:
            logger.warning(f"[lineage] partial failure: {f}")
    elif partial_failures:
        logger.warning(f"[lineage] {len(partial_failures)} object failures (first 3): {partial_failures[:3]}")
    return deduped, partial_failures


def _edge_type_from_domains(src_domain: Optional[str], tgt_domain: Optional[str]) -> str:
    """Best-effort classification when GET_LINEAGE doesn't spell out the
    operation. A view/dynamic-table target implies a definitional link;
    a stage source implies a load."""
    s = (src_domain or "").upper()
    t = (tgt_domain or "").upper()
    if "STAGE" in s or "EXTERNAL LOCATION" in s:
        return "copy_into"
    if "DYNAMIC" in t:
        return "dynamic_table"
    if "MATERIALIZED VIEW" in t:
        return "materialized_view"
    if "VIEW" in t:
        return "view_dep"
    return "data_flow"


def fetch_lineage_via_object_dependencies(database: str) -> List[dict]:
    """Fallback: single ACCOUNT_USAGE query. Only captures reference-style
    dependencies (view→table, UDF→table). Silent on COPY INTO / stages —
    documented at the discovery-method badge in the UI."""
    rows = sf_session.query(
        """
        SELECT REFERENCED_DATABASE, REFERENCED_SCHEMA, REFERENCED_OBJECT_NAME, REFERENCED_OBJECT_DOMAIN,
               REFERENCING_DATABASE, REFERENCING_SCHEMA, REFERENCING_OBJECT_NAME, REFERENCING_OBJECT_DOMAIN,
               DEPENDENCY_TYPE
        FROM SNOWFLAKE.ACCOUNT_USAGE.OBJECT_DEPENDENCIES
        WHERE REFERENCED_DATABASE = %(db)s OR REFERENCING_DATABASE = %(db)s
        """,
        {"db": database},
    )
    edges: List[dict] = []
    for r in rows:
        src_db = r["REFERENCED_DATABASE"]
        src_sc = r["REFERENCED_SCHEMA"]
        src_nm = r["REFERENCED_OBJECT_NAME"]
        src_dm = r["REFERENCED_OBJECT_DOMAIN"]
        tgt_db = r["REFERENCING_DATABASE"]
        tgt_sc = r["REFERENCING_SCHEMA"]
        tgt_nm = r["REFERENCING_OBJECT_NAME"]
        tgt_dm = r["REFERENCING_OBJECT_DOMAIN"]
        if not (src_db and src_nm and tgt_db and tgt_nm):
            continue
        edges.append({
            "source_database": src_db, "source_schema": src_sc, "source_table": src_nm,
            "source_fqn": _fqn(src_db, src_sc or "", src_nm),
            "source_kind": _normalize_domain(src_dm),
            "target_database": tgt_db, "target_schema": tgt_sc, "target_table": tgt_nm,
            "target_fqn": _fqn(tgt_db, tgt_sc or "", tgt_nm),
            "target_kind": _normalize_domain(tgt_dm),
            "edge_type": _edge_type_from_domains(src_dm, tgt_dm),
            "discovery_source": "object_dependencies",
            "evidence": {"dependency_type": r.get("DEPENDENCY_TYPE")},
        })
    return _dedupe_edges(edges)


# ─────────────────────────────────────────────────────────────────────────
# Refresh orchestrator
# ─────────────────────────────────────────────────────────────────────────

class RefreshResult:
    __slots__ = ("status", "edge_count", "method_used", "partial_failures", "error")

    def __init__(self, status, edge_count, method_used, partial_failures=None, error=None):
        self.status = status
        self.edge_count = edge_count
        self.method_used = method_used
        self.partial_failures = partial_failures or []
        self.error = error

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "edge_count": self.edge_count,
            "method_used": self.method_used,
            "partial_failures": self.partial_failures,
            "error": self.error,
        }


def _require_snowflake(connection_id: str):
    conn = storage.get_connection_record(connection_id)
    if conn is None:
        raise ValueError(f"Connection {connection_id} not found")
    if conn.type != ConnectionType.SNOWFLAKE.value:
        raise ValueError("Lineage is only supported for Snowflake connections")
    return conn


def refresh_database(connection_id: str, database: str) -> RefreshResult:
    """Full-database refresh:
      1. Enumerate every schema + table + view in the DB into LINEAGE_CATALOG
         (unconditional — this is what powers 'show me everything', not just
         scanned tables).
      2. Probe GET_LINEAGE, pick the fetcher, replace LINEAGE_EDGES.
      3. Upsert refresh state.
    Catalog indexing is best-effort: it runs whether or not GET_LINEAGE is
    available, so users always get a full nested tree even before granting
    VIEW LINEAGE."""
    _require_snowflake(connection_id)

    # Step 1 — catalog. Never blocks edge discovery on catalog failure.
    try:
        catalog_rows = enumerate_full_catalog(database)
        storage.replace_catalog_for_database(connection_id, database, catalog_rows)
    except Exception as e:
        logger.warning(f"[lineage] catalog index failed for {database}: {e}")

    # Step 2 — edges. Probe against the DB being refreshed, not SNOWFLAKE
    # shared DB (which most non-admin roles can't read).
    available, probe_err = probe_get_lineage_available(connection_id, database, force=True)
    logger.info(f"[lineage] probe result for {database}: available={available} err={probe_err}")

    try:
        if available:
            edges, partial = fetch_lineage_via_get_lineage(database)
            method = "get_lineage"
        else:
            edges = fetch_lineage_via_object_dependencies(database)
            partial = []
            method = "object_dependencies"
    except Exception as e:
        storage.upsert_refresh_state(
            connection_id, database, "error", 0, None, error=str(e),
        )
        return RefreshResult("error", 0, None, error=str(e))

    n = storage.replace_lineage_edges_for_database(connection_id, database, edges)
    status = "partial" if partial else "ok"
    storage.upsert_refresh_state(
        connection_id, database, status, n, method, partial_failures=partial,
    )
    return RefreshResult(status, n, method, partial_failures=partial)


def index_all_databases(connection_id: str) -> dict:
    """One-shot: enumerate every DB the role can see and populate
    LINEAGE_CATALOG for each. Cheap (INFORMATION_SCHEMA reads only) — does NOT
    run GET_LINEAGE or OBJECT_DEPENDENCIES. Lets the user browse the full
    nested tree immediately; per-DB Refresh then adds lineage edges."""
    _require_snowflake(connection_id)
    dbs = list_snowflake_databases()
    results = []
    for db in dbs:
        try:
            rows = enumerate_full_catalog(db)
            n = storage.replace_catalog_for_database(connection_id, db, rows)
            results.append({"database": db, "objects": n, "status": "ok"})
        except Exception as e:
            results.append({"database": db, "objects": 0, "status": "error", "error": str(e)})
    return {"databases": results, "total_databases": len(dbs)}


# ─────────────────────────────────────────────────────────────────────────
# Graph builders
# ─────────────────────────────────────────────────────────────────────────

def _resolve_connection_id(connection_id: Optional[str]) -> Optional[str]:
    """None → first Snowflake connection (mirror registry.get_source's fallback)."""
    if connection_id:
        return connection_id
    conn = storage.get_first_connection(prefer_type=ConnectionType.SNOWFLAKE.value)
    return conn.id if conn else None


def _empty_graph(reason: Optional[str] = None) -> dict:
    return {
        "available": reason is None,
        "reason": reason,
        "nodes": [], "edges": [],
        "last_refreshed_at": None,
        "discovery_method": None,
    }


def _check_snowflake_or_empty(connection_id: Optional[str]) -> Tuple[Optional[str], Optional[dict]]:
    """Returns (resolved_connection_id, empty_response_if_unavailable)."""
    cid = _resolve_connection_id(connection_id)
    if not cid:
        return None, _empty_graph("no_connection")
    conn = storage.get_connection_record(cid)
    if conn is None:
        return None, _empty_graph("no_connection")
    if conn.type != ConnectionType.SNOWFLAKE.value:
        return None, _empty_graph("postgres_unsupported")
    return cid, None


def _latest_refresh_meta(connection_id: str, database: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    """Returns (last_refreshed_at_iso, discovery_method)."""
    states = storage.list_refresh_states(connection_id)
    if database:
        states = [s for s in states if s.database_name == database]
    if not states:
        return None, None
    latest = max(states, key=lambda s: s.last_refreshed_at or datetime.datetime.min)
    ts = latest.last_refreshed_at
    return (
        ts.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z' if ts else None,
        latest.discovery_method_used,
    )


def _collect_overlays(
    connection_id: str, databases: List[str],
) -> Tuple[Dict[str, str], Dict[str, int], Dict[str, int], Dict[str, Optional[float]]]:
    """Batched overlay lookup for a set of databases.
    Returns:
      asset_by_fqn:      {table_fqn: asset_id}   — from ASSETS
      findings_by_fqn:   {table_fqn: open_count}
      rules_by_fqn:      {table_fqn: rules_run_count}
      health_by_fqn:     {table_fqn: score in [0,1] or None}
    """
    asset_by_fqn: Dict[str, str] = {}
    findings_by_fqn: Dict[str, int] = {}
    rules_by_fqn: Dict[str, int] = {}
    health_by_fqn: Dict[str, Optional[float]] = {}

    if not databases:
        return asset_by_fqn, findings_by_fqn, rules_by_fqn, health_by_fqn

    for db in databases:
        _, asset_list = storage.list_assets(
            asset_type="table", database_name=db, limit=100000,
        )
        asset_ids = []
        for a in asset_list:
            fqn = a.fqn or _fqn(a.database_name, a.schema_name, a.table_name)
            asset_by_fqn[fqn] = a.id
            asset_ids.append(a.id)
        if asset_ids:
            open_map = storage.count_open_findings_by_asset(asset_ids)
            id_to_fqn = {a.id: (a.fqn or _fqn(a.database_name, a.schema_name, a.table_name)) for a in asset_list}
            for aid, ct in open_map.items():
                fqn = id_to_fqn.get(aid)
                if fqn:
                    findings_by_fqn[fqn] = ct

        rules = storage.count_rules_run_by_table(database_name=db)
        for (d, s, t), ct in rules.items():
            rules_by_fqn[_fqn(d, s, t)] = ct

        scores = storage.batch_health_scores(database_name=db)
        for (d, s, t), score in scores.items():
            health_by_fqn[_fqn(d, s, t)] = score

    return asset_by_fqn, findings_by_fqn, rules_by_fqn, health_by_fqn


def _nested_nodes_and_edges(
    connection_id: str,
    databases: List[str],
    include_indexed_only: bool = True,
) -> dict:
    """Build the FULL nested graph — database containers, schema containers,
    table leaves, plus lineage edges — for `databases`.

    Each node carries `parent_id` (or null for top-level DB containers) so the
    frontend can render them nested via reactflow's parentId + extent='parent'.

    Overlay data (health, findings, rules-run) is attached to every table node.
    Edges include lineage between tables regardless of which DB they span
    (so cross-DB flows show up as arcs).
    """
    # Fetch all catalog rows for the requested DBs in one shot.
    where = ["CONNECTION_ID = %(cid)s"]
    params: Dict[str, Any] = {"cid": connection_id}
    if databases:
        ph = ", ".join(f"%(d{i})s" for i in range(len(databases)))
        for i, d in enumerate(databases):
            params[f"d{i}"] = d
        where.append(f"DATABASE_NAME IN ({ph})")
    catalog_rows = sf_session.query(
        f"SELECT * FROM LINEAGE_CATALOG WHERE {' AND '.join(where)} "
        f"ORDER BY DATABASE_NAME, SCHEMA_NAME NULLS FIRST, TABLE_NAME NULLS FIRST",
        params,
    )
    catalog = [storage._catalog_from_row(r) for r in catalog_rows]

    # Overlays (batched per DB).
    _, findings_by_fqn, rules_by_fqn, health_by_fqn = _collect_overlays(connection_id, databases)

    # Group by DB → schema → tables.
    schemas_by_db: Dict[str, set] = defaultdict(set)
    tables_by_schema: Dict[Tuple[str, str], List[Any]] = defaultdict(list)
    for c in catalog:
        if c.object_kind == "schema":
            schemas_by_db[c.database_name].add(c.schema_name)
        elif c.schema_name and c.table_name:
            schemas_by_db[c.database_name].add(c.schema_name)
            tables_by_schema[(c.database_name, c.schema_name)].append(c)

    # Build nodes.
    nodes: List[dict] = []
    for db in sorted(schemas_by_db.keys()):
        db_id = f"db:{db}"
        schema_names = sorted(s for s in schemas_by_db[db] if s)
        # Aggregate DB overlays: sum findings across tables, avg health.
        db_tables = []
        for sc in schema_names:
            db_tables.extend(tables_by_schema[(db, sc)])
        open_ct = sum(findings_by_fqn.get(t.fqn, 0) for t in db_tables)
        scored = [health_by_fqn[t.fqn] for t in db_tables if health_by_fqn.get(t.fqn) is not None]
        avg_health = sum(scored) / len(scored) if scored else None
        nodes.append({
            "id": db_id, "label": db, "kind": "database",
            "parent_id": None,
            "database": db,
            "table_count": len(db_tables),
            "schema_count": len(schema_names),
            "open_findings_total": open_ct,
            "avg_health_score": avg_health,
        })

        for sc in schema_names:
            sc_id = f"sc:{db}.{sc}"
            sc_tables = tables_by_schema[(db, sc)]
            sc_open = sum(findings_by_fqn.get(t.fqn, 0) for t in sc_tables)
            sc_scored = [health_by_fqn[t.fqn] for t in sc_tables if health_by_fqn.get(t.fqn) is not None]
            sc_avg = sum(sc_scored) / len(sc_scored) if sc_scored else None
            nodes.append({
                "id": sc_id, "label": sc, "kind": "schema",
                "parent_id": db_id,
                "database": db, "schema": sc,
                "table_count": len(sc_tables),
                "open_findings_total": sc_open,
                "avg_health_score": sc_avg,
            })

            for t in sc_tables:
                nodes.append({
                    "id": t.fqn, "label": t.table_name, "kind": t.object_kind,
                    "parent_id": sc_id,
                    "database": db, "schema": sc, "table": t.table_name,
                    "row_count": t.row_count, "size_bytes": t.size_bytes,
                    "health_score": health_by_fqn.get(t.fqn),
                    "open_findings": findings_by_fqn.get(t.fqn, 0),
                    "rules_run": rules_by_fqn.get(t.fqn, 0),
                })

    # Edges — every lineage edge whose either endpoint is in `databases`.
    edge_rows: List = []
    if databases:
        ph = ", ".join(f"%(d{i})s" for i in range(len(databases)))
        p2: Dict[str, Any] = {"cid": connection_id}
        for i, d in enumerate(databases):
            p2[f"d{i}"] = d
        rows = sf_session.query(
            f"""
            SELECT * FROM LINEAGE_EDGES
            WHERE CONNECTION_ID = %(cid)s
              AND (SOURCE_DATABASE IN ({ph}) OR TARGET_DATABASE IN ({ph}))
            """,
            p2,
        )
        edge_rows = [storage._edge_from_row(r) for r in rows]

    # Include cross-DB tables that appear in edges but weren't in the catalog
    # slice — render them as ghost nodes under a synthetic "external" container
    # so the arrow has somewhere to land.
    known_fqns = {n["id"] for n in nodes if n["kind"] not in ("database", "schema")}
    ghost_dbs: set = set()
    ghost_schemas: set = set()
    ghost_tables: Dict[str, Any] = {}
    for e in edge_rows:
        for db, sc, tb, fqn, kind in (
            (e.source_database, e.source_schema, e.source_table, e.source_fqn, e.source_kind or "table"),
            (e.target_database, e.target_schema, e.target_table, e.target_fqn, e.target_kind or "table"),
        ):
            if fqn in known_fqns or not (db and sc and tb):
                continue
            ghost_dbs.add(db)
            ghost_schemas.add((db, sc))
            if fqn not in ghost_tables:
                ghost_tables[fqn] = (db, sc, tb, kind)

    for db in sorted(ghost_dbs):
        # Skip if we already added this DB container above.
        if any(n["id"] == f"db:{db}" for n in nodes):
            continue
        nodes.append({
            "id": f"db:{db}", "label": db + " (external)", "kind": "database",
            "parent_id": None, "database": db, "table_count": 0, "schema_count": 0,
            "open_findings_total": 0, "avg_health_score": None,
        })
    for db, sc in sorted(ghost_schemas):
        sc_id = f"sc:{db}.{sc}"
        if any(n["id"] == sc_id for n in nodes):
            continue
        nodes.append({
            "id": sc_id, "label": sc, "kind": "schema",
            "parent_id": f"db:{db}", "database": db, "schema": sc,
            "table_count": 0, "open_findings_total": 0, "avg_health_score": None,
        })
    for fqn, (db, sc, tb, kind) in ghost_tables.items():
        nodes.append({
            "id": fqn, "label": tb, "kind": kind,
            "parent_id": f"sc:{db}.{sc}",
            "database": db, "schema": sc, "table": tb,
            "row_count": None, "size_bytes": None,
            "health_score": None, "open_findings": 0, "rules_run": 0,
            "is_external": True,
        })

    edges: List[dict] = []
    for e in edge_rows:
        edges.append({
            "id": f"{e.source_fqn}->{e.target_fqn}:{e.edge_type or ''}",
            "source": e.source_fqn, "target": e.target_fqn,
            "edge_type": e.edge_type,
            "discovery_source": e.discovery_source,
        })

    return {"nodes": nodes, "edges": edges}


def build_all_databases_graph(connection_id: Optional[str]) -> dict:
    """Landing view — DB picker cards, not a full graph. Returns one card per
    indexed database with counts and health so the user can pick which one to
    open. Empty edges/nodes at this level; frontend renders cards, not a
    reactflow canvas."""
    cid, empty = _check_snowflake_or_empty(connection_id)
    if empty is not None:
        return empty
    databases = storage.indexed_databases(cid)
    if not databases:
        return {
            "available": True, "reason": None,
            "nodes": [], "edges": [], "databases": [],
            "last_refreshed_at": None,
            "discovery_method": None,
        }

    # One row per (DB, schema, table) from catalog + refresh state per DB.
    cards: List[dict] = []
    refresh_states = {s.database_name: s for s in storage.list_refresh_states(cid)}
    for db in databases:
        rows = storage.list_catalog(cid, database_name=db)
        schemas = {r.schema_name for r in rows if r.schema_name}
        tables = [r for r in rows if r.table_name]
        # Overlays
        _, _, _, health_map = _collect_overlays(cid, [db])
        scored = [health_map[t.fqn] for t in tables if health_map.get(t.fqn) is not None]
        avg_health = sum(scored) / len(scored) if scored else None
        # Edge count from LINEAGE_EDGES scoped to this DB
        edge_rows = sf_session.query(
            """
            SELECT COUNT(*) AS CT FROM LINEAGE_EDGES
            WHERE CONNECTION_ID = %(cid)s
              AND (SOURCE_DATABASE = %(db)s OR TARGET_DATABASE = %(db)s)
            """,
            {"cid": cid, "db": db},
        )
        edge_ct = int((edge_rows[0]["CT"] if edge_rows else 0) or 0)
        st = refresh_states.get(db)
        cards.append({
            "database": db,
            "schema_count": len(schemas),
            "table_count": len(tables),
            "edge_count": edge_ct,
            "avg_health_score": avg_health,
            "last_refreshed_at": st.last_refreshed_at.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z' if st and st.last_refreshed_at else None,
            "discovery_method_used": st.discovery_method_used if st else None,
            "last_status": st.last_status if st else None,
        })

    ts, method = _latest_refresh_meta(cid)
    return {
        "available": True, "reason": None,
        "nodes": [], "edges": [], "databases": cards,
        "last_refreshed_at": ts,
        "discovery_method": method,
    }


def build_database_graph(connection_id: Optional[str], database: str) -> dict:
    """Complete nested graph for a single database — every schema and every
    table with edges. This is the drill-down for one database.

    Auto-crawls on first open: if this DB has never been successfully refreshed
    (no LINEAGE_REFRESH_STATE row with LAST_STATUS='ok'), we run the crawl
    inline so the user gets edges without having to hunt for the Refresh
    button. Subsequent opens skip the auto-crawl even if the last run had a
    non-ok status (avoids re-hitting a permission-denied path on every nav)."""
    cid, empty = _check_snowflake_or_empty(connection_id)
    if empty is not None:
        return empty

    prior_state = storage.get_refresh_state(cid, database)
    if prior_state is None:
        # Never crawled. Try to auto-crawl before returning the graph.
        logger.info(f"[lineage] auto-crawl triggered for {database} (no prior refresh state)")
        try:
            result = refresh_database(cid, database)
            logger.info(
                f"[lineage] auto-crawl finished for {database}: "
                f"status={result.status} edges={result.edge_count} method={result.method_used}"
            )
        except Exception as e:
            logger.warning(f"[lineage] auto-crawl failed for {database}: {e}")

    payload = _nested_nodes_and_edges(cid, [database])
    ts, method = _latest_refresh_meta(cid, database)
    return {
        "available": True, "reason": None,
        **payload,
        "last_refreshed_at": ts,
        "discovery_method": method,
    }


def _instances_by_table(database: str, schema: Optional[str]) -> Dict[Tuple[str, str, str], List[str]]:
    where = ["I.DATABASE_NAME = %(db)s", "I.IS_ACTIVE = TRUE"]
    params: Dict[str, Any] = {"db": database}
    if schema:
        where.append("I.SCHEMA_NAME = %(sc)s")
        params["sc"] = schema
    rows = sf_session.query(
        f"""
        SELECT DATABASE_NAME AS DB, SCHEMA_NAME AS SC, TABLE_NAME AS TB, ID
        FROM RULE_INSTANCES I
        WHERE {' AND '.join(where)}
        """,
        params,
    )
    out: Dict[Tuple[str, str, str], List[str]] = defaultdict(list)
    for r in rows:
        out[(r["DB"], r["SC"], r["TB"])].append(r["ID"])
    return out


def build_schema_graph(
    connection_id: Optional[str], database: str, schema: str,
) -> dict:
    """Flat table-level graph for one schema. Includes every table in the
    schema (from LINEAGE_CATALOG) plus the immediate neighbour tables on the
    other side of any lineage edges — so cross-schema flows still have
    somewhere to anchor.

    Fast: only queries edges touching this schema, then does 1-hop expansion
    for neighbour metadata. No full-DB catalog scan."""
    cid, empty = _check_snowflake_or_empty(connection_id)
    if empty is not None:
        return empty

    # 1. Edges touching this schema (source OR target side).
    edge_rows = sf_session.query(
        """
        SELECT * FROM LINEAGE_EDGES
        WHERE CONNECTION_ID = %(cid)s
          AND ((SOURCE_DATABASE = %(db)s AND SOURCE_SCHEMA = %(sc)s)
            OR (TARGET_DATABASE = %(db)s AND TARGET_SCHEMA = %(sc)s))
        """,
        {"cid": cid, "db": database, "sc": schema},
    )
    edges_raw = [storage._edge_from_row(r) for r in edge_rows]

    # 2. Catalog rows for tables in this schema (all of them, even isolated).
    catalog_here = storage.list_catalog(cid, database_name=database, schema_name=schema)
    tables_here = [c for c in catalog_here if c.table_name]

    # 3. FQNs of neighbours (other side of every edge) — pulled by fqn from
    # catalog wherever available, otherwise ghosted.
    neighbour_fqns: set[str] = set()
    for e in edges_raw:
        neighbour_fqns.add(e.source_fqn)
        neighbour_fqns.add(e.target_fqn)
    own_fqns = {c.fqn for c in tables_here}
    external_fqns = neighbour_fqns - own_fqns
    external_by_fqn: Dict[str, Any] = {}
    if external_fqns:
        # Look those up in one shot from the catalog (may miss cross-DB ones).
        # Small N; the where-in inline is fine.
        ph = ", ".join(f"%(f{i})s" for i in range(len(external_fqns)))
        params = {f"f{i}": f for i, f in enumerate(external_fqns)}
        params["cid"] = cid
        rows = sf_session.query(
            f"SELECT * FROM LINEAGE_CATALOG WHERE CONNECTION_ID = %(cid)s AND FQN IN ({ph})",
            params,
        )
        for r in rows:
            external_by_fqn[r["FQN"]] = storage._catalog_from_row(r)

    # 4. Overlays for own tables + external tables that live in databases we
    # can hit cheaply. Group by DB → schema so batch calls stay tight.
    all_dbs = {database, *(c.database_name for c in external_by_fqn.values())}
    _, findings_by_fqn, rules_by_fqn, health_by_fqn = _collect_overlays(cid, list(all_dbs))

    # 5. Build the flat node list.
    nodes: List[dict] = []
    def _table_node(c, is_external: bool):
        return {
            "id": c.fqn, "label": c.table_name, "kind": c.object_kind or "table",
            "parent_id": None,
            "database": c.database_name, "schema": c.schema_name, "table": c.table_name,
            "row_count": c.row_count, "size_bytes": c.size_bytes,
            "health_score": health_by_fqn.get(c.fqn),
            "open_findings": findings_by_fqn.get(c.fqn, 0),
            "rules_run": rules_by_fqn.get(c.fqn, 0),
            "is_external": is_external,
        }
    for c in tables_here:
        nodes.append(_table_node(c, is_external=False))
    for fqn in external_fqns:
        c = external_by_fqn.get(fqn)
        if c is not None:
            nodes.append(_table_node(c, is_external=True))
        else:
            # Ghost node (catalog miss). Parse fqn back into parts.
            parts = fqn.split(".")
            if len(parts) < 3:
                continue
            db2, sc2, tb2 = parts
            nodes.append({
                "id": fqn, "label": tb2, "kind": "table",
                "parent_id": None,
                "database": db2, "schema": sc2, "table": tb2,
                "row_count": None, "size_bytes": None,
                "health_score": None, "open_findings": 0, "rules_run": 0,
                "is_external": True,
            })

    # 6. Edges — one entry per LINEAGE_EDGES row.
    edges: List[dict] = []
    for e in edges_raw:
        edges.append({
            "id": f"{e.source_fqn}->{e.target_fqn}:{e.edge_type or ''}",
            "source": e.source_fqn, "target": e.target_fqn,
            "edge_type": e.edge_type,
            "discovery_source": e.discovery_source,
        })

    ts, method = _latest_refresh_meta(cid, database)
    return {
        "available": True, "reason": None,
        "nodes": nodes, "edges": edges,
        "last_refreshed_at": ts,
        "discovery_method": method,
    }


def build_table_lineage(
    connection_id: Optional[str], database: str, schema: str, table: str, hops: int = 3,
) -> dict:
    """BFS upstream + downstream from a single table, up to `hops` steps.
    Nodes carry the same overlay contract as schema-level view."""
    cid, empty = _check_snowflake_or_empty(connection_id)
    if empty is not None:
        return empty
    hops = max(1, min(int(hops or 3), 10))

    all_edges = storage.list_lineage_edges(cid)
    upstream = defaultdict(list)   # target_fqn -> list of edges arriving
    downstream = defaultdict(list) # source_fqn -> list of edges leaving
    for e in all_edges:
        upstream[e.target_fqn].append(e)
        downstream[e.source_fqn].append(e)

    start = _fqn(database, schema, table)
    visited: Dict[str, int] = {start: 0}
    used_edges: List = []
    queue = deque([(start, 0)])
    while queue:
        node, depth = queue.popleft()
        if depth >= hops:
            continue
        for e in upstream.get(node, []):
            used_edges.append(e)
            if e.source_fqn not in visited:
                visited[e.source_fqn] = depth + 1
                queue.append((e.source_fqn, depth + 1))
        for e in downstream.get(node, []):
            used_edges.append(e)
            if e.target_fqn not in visited:
                visited[e.target_fqn] = depth + 1
                queue.append((e.target_fqn, depth + 1))

    # Build node metadata. Group affected DBs and pull overlays lazily.
    dbs_scoped: Dict[str, set] = defaultdict(set)
    for fqn in visited:
        parts = fqn.split(".")
        if len(parts) == 3:
            dbs_scoped[parts[0]].add(parts[1])

    all_scores: Dict[Tuple[str, str, str], Optional[float]] = {}
    all_rules: Dict[Tuple[str, str, str], int] = {}
    for db, schs in dbs_scoped.items():
        for sc in schs:
            all_scores.update(storage.batch_health_scores(database_name=db, schema_name=sc))
            all_rules.update(storage.count_rules_run_by_table(database_name=db, schema_name=sc))

    # Look up object kinds from the edge records so views/dynamic tables get
    # the right border colour, and from LINEAGE_CATALOG for objects that don't
    # appear on any edge (only the focus can be in that bucket).
    kind_by_fqn: Dict[str, str] = {}
    for e in used_edges:
        kind_by_fqn.setdefault(e.source_fqn, e.source_kind or "table")
        kind_by_fqn.setdefault(e.target_fqn, e.target_kind or "table")
    # Fallback for the focus node: check the catalog.
    if start not in kind_by_fqn:
        try:
            parts = start.split(".")
            if len(parts) == 3:
                cat = storage.list_catalog(cid, database_name=parts[0], schema_name=parts[1])
                for c in cat:
                    if c.fqn == start and c.object_kind:
                        kind_by_fqn[start] = c.object_kind
                        break
        except Exception:
            pass

    nodes = []
    asset_ids_needed: Dict[str, str] = {}
    for fqn in visited:
        parts = fqn.split(".")
        if len(parts) != 3:
            continue
        db, sc, tb = parts
        asset = storage.get_table_asset(db, sc, tb)
        asset_ids_needed[fqn] = asset.id if asset else None
        nodes.append({
            "id": fqn, "label": tb,
            "kind": kind_by_fqn.get(fqn, "table"),
            "database": db, "schema": sc, "table": tb,
            "asset_id": asset.id if asset else None,
            "row_count": asset.row_count if asset else None,
            "size_bytes": asset.size_bytes if asset else None,
            "last_scanned_at": asset.last_scanned_at.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z' if asset and asset.last_scanned_at else None,
            "health_score": all_scores.get((db, sc, tb)),
            "rules_run": all_rules.get((db, sc, tb), 0),
            "depth": visited[fqn],
            "is_focus": fqn == start,
        })

    findings = storage.count_open_findings_by_asset([aid for aid in asset_ids_needed.values() if aid])
    for n in nodes:
        n["open_findings"] = findings.get(n["asset_id"], 0) if n["asset_id"] else 0

    # Dedupe edges by identity
    seen_edge_ids = set()
    edges_out = []
    for e in used_edges:
        key = (e.source_fqn, e.target_fqn, e.edge_type or "")
        if key in seen_edge_ids:
            continue
        seen_edge_ids.add(key)
        edges_out.append({
            "id": f"{e.source_fqn}->{e.target_fqn}:{e.edge_type or ''}",
            "source": e.source_fqn, "target": e.target_fqn,
            "edge_type": e.edge_type,
            "discovery_source": e.discovery_source,
        })

    ts, method = _latest_refresh_meta(cid, database)
    return {
        "available": True, "reason": None,
        "nodes": nodes, "edges": edges_out,
        "focus_fqn": start, "hops": hops,
        "last_refreshed_at": ts,
        "discovery_method": method,
    }


# ─────────────────────────────────────────────────────────────────────────
# Workflow highlight
# ─────────────────────────────────────────────────────────────────────────

def _triples_from_target_config(
    pattern: dict, default_scope: Optional[dict] = None,
) -> List[Tuple[str, str, str]]:
    """Extract (db, schema, table) triples from a rule pattern. Handles the
    historic shape variations: pattern-level db/schema/table, target_config
    with database/schema/table, target_config.tables list, target_config.columns
    with per-column {table, column}."""
    default_scope = default_scope or {}
    tc = pattern.get("target_config") or {}
    triples: List[Tuple[str, str, str]] = []

    db = pattern.get("database_name") or tc.get("database") or tc.get("database_name") or default_scope.get("database")
    sc = pattern.get("schema_name") or tc.get("schema") or tc.get("schema_name") or default_scope.get("schema")
    tb = pattern.get("table_name") or tc.get("table") or tc.get("table_name") or default_scope.get("table")
    if db and sc and tb:
        triples.append((db, sc, tb))

    for t in (tc.get("tables") or []):
        if isinstance(t, str) and db and sc:
            triples.append((db, sc, t))
        elif isinstance(t, dict):
            tdb = t.get("database") or db
            tsc = t.get("schema") or sc
            ttb = t.get("table") or t.get("name")
            if tdb and tsc and ttb:
                triples.append((tdb, tsc, ttb))

    for c in (tc.get("columns") or []):
        if isinstance(c, dict):
            tdb = c.get("database") or db
            tsc = c.get("schema") or sc
            ttb = c.get("table")
            if tdb and tsc and ttb:
                triples.append((tdb, tsc, ttb))

    return triples


def compute_workflow_highlight_set(
    connection_id: Optional[str], workflow_id: str,
) -> dict:
    """Resolve a saved workflow into a highlight set. Returns:
      {
        origin: {database, schema, table} | null,
        nodes:  [{database, schema, table, fqn}, ...],  # fully-qualified
        unmatched_targets: [...triples not present in LINEAGE_EDGES...],
      }"""
    workflow = storage.get_workflow(workflow_id)
    if workflow is None:
        return {"origin": None, "nodes": [], "unmatched_targets": []}

    origin: Optional[dict] = None
    if workflow.origin_scope == "table" and workflow.origin_table:
        origin = {
            "database": workflow.origin_database,
            "schema": workflow.origin_schema,
            "table": workflow.origin_table,
        }

    default_scope = {
        "database": workflow.origin_database,
        "schema": workflow.origin_schema,
        "table": workflow.origin_table if workflow.origin_scope == "table" else None,
    }

    triples: set[Tuple[str, str, str]] = set()
    if origin:
        triples.add((origin["database"], origin["schema"], origin["table"]))
    for p in (workflow.rule_patterns or []):
        for t in _triples_from_target_config(p, default_scope):
            triples.add(t)

    # Match against LINEAGE_EDGES presence — an "unmatched" target is one
    # that isn't referenced by any cached edge (either as source or target).
    cid = _resolve_connection_id(connection_id)
    present: set[Tuple[str, str, str]] = set()
    if cid:
        rows = sf_session.query(
            """
            SELECT DISTINCT SOURCE_DATABASE AS DB, SOURCE_SCHEMA AS SC, SOURCE_TABLE AS TB
            FROM LINEAGE_EDGES WHERE CONNECTION_ID = %(cid)s
            UNION
            SELECT DISTINCT TARGET_DATABASE AS DB, TARGET_SCHEMA AS SC, TARGET_TABLE AS TB
            FROM LINEAGE_EDGES WHERE CONNECTION_ID = %(cid)s
            """,
            {"cid": cid},
        )
        present = {(r["DB"], r["SC"], r["TB"]) for r in rows}

    unmatched: List[dict] = []
    matched_nodes: List[dict] = []
    for db, sc, tb in triples:
        entry = {"database": db, "schema": sc, "table": tb, "fqn": _fqn(db, sc, tb)}
        if not present or (db, sc, tb) in present:
            matched_nodes.append(entry)
        else:
            unmatched.append(entry)

    return {
        "origin": origin,
        "nodes": matched_nodes,
        "unmatched_targets": unmatched,
    }


# ─────────────────────────────────────────────────────────────────────────
# Status endpoint helper
# ─────────────────────────────────────────────────────────────────────────

def get_status(connection_id: Optional[str]) -> dict:
    cid, empty = _check_snowflake_or_empty(connection_id)
    if empty is not None:
        return {"available": empty["available"], "reason": empty["reason"], "databases": [], "capability": None}

    states = storage.list_refresh_states(cid)
    cap = storage.get_lineage_capability(cid)
    return {
        "available": True, "reason": None,
        "databases": [
            {
                "database": s.database_name,
                "last_refreshed_at": s.last_refreshed_at.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z' if s.last_refreshed_at else None,
                "last_status": s.last_status,
                "edge_count": s.edge_count,
                "discovery_method_used": s.discovery_method_used,
                "last_error": s.last_error,
            }
            for s in states
        ],
        "capability": {
            "get_lineage_available": bool(cap.get_lineage_available) if cap else None,
            "probed_at": cap.probed_at.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z' if cap and cap.probed_at else None,
            "probe_error": cap.probe_error if cap else None,
        } if cap else None,
    }
