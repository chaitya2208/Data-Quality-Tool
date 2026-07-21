"""
App storage layer — raw SQL against Snowflake (DQ_APP schema), replacing
the old SQLAlchemy models (app/models/*.py) and ORM Session pattern.

Every function goes through app.services.snowflake_session.session (the
same singleton SSO connection everything else in this app already uses) —
no separate connection/pool here.

Rows come back from snowflake-connector as dicts with UPPERCASE keys and
VARIANT columns as JSON strings. Each entity has a `_from_row()` that
reshapes this into a SimpleNamespace with lowercase snake_case attributes
(matching the old ORM model's attribute names) and already-parsed
JSON/VARIANT fields — so existing call sites that do `asset.fqn`,
`rule.severity`, etc. keep working with minimal changes.

IDs are generated in Python (uuid4) before insert, then returned directly —
no round-trip SELECT needed to get the new row's ID.

VARIANT columns can't bind a raw Python dict/list — Snowflake needs
PARSE_JSON(<json string>). Every INSERT/UPDATE touching a VARIANT column
uses PARSE_JSON(%(...)s) with json.dumps() on the Python side, and
`INSERT ... SELECT ...` (not `INSERT ... VALUES (...)`) since Snowflake
rejects PARSE_JSON(NULL) inside a VALUES clause but allows it in a SELECT
list.
"""
from __future__ import annotations

import datetime
import decimal
import json
import logging
import uuid
from collections import defaultdict
from types import SimpleNamespace
from typing import Any, Dict, Optional

from app.services.snowflake_session import session as sf_session

logger = logging.getLogger(__name__)


def _new_id() -> str:
    return str(uuid.uuid4())


def _sha256(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime.datetime):
        return value.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
    if isinstance(value, (datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _json_or_null(value: Any) -> Optional[str]:
    return json.dumps(value, default=_json_default) if value is not None else None


def _parse_json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value  # already parsed by the driver
    try:
        return json.loads(value)
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════════
# ASSETS
# ═══════════════════════════════════════════════════════════════════════════

def _asset_from_row(row: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id=row["ID"],
        asset_type=row["ASSET_TYPE"],
        database_name=row["DATABASE_NAME"],
        schema_name=row["SCHEMA_NAME"],
        table_name=row["TABLE_NAME"],
        column_name=row["COLUMN_NAME"],
        fqn=row["FQN"],
        owner=row["OWNER"],
        comment=row["COMMENT"],
        row_count=row["ROW_COUNT"],
        size_bytes=row["SIZE_BYTES"],
        raw_metadata=_parse_json(row.get("RAW_METADATA")),
        created_at=row["CREATED_AT"],
        updated_at=row["UPDATED_AT"],
        last_scanned_at=row["LAST_SCANNED_AT"],
    )


def get_asset(asset_id: str) -> Optional[SimpleNamespace]:
    rows = sf_session.query("SELECT * FROM ASSETS WHERE ID = %(id)s", {"id": asset_id})
    return _asset_from_row(rows[0]) if rows else None


def get_asset_by_fqn(fqn: str) -> Optional[SimpleNamespace]:
    rows = sf_session.query("SELECT * FROM ASSETS WHERE FQN = %(fqn)s", {"fqn": fqn})
    return _asset_from_row(rows[0]) if rows else None


def list_assets(
    asset_type: Optional[str] = None,
    database_name: Optional[str] = None,
    schema_name: Optional[str] = None,
    table_name: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
) -> tuple[int, list[SimpleNamespace]]:
    where, params = [], {}
    if asset_type:
        where.append("ASSET_TYPE = %(asset_type)s")
        params["asset_type"] = asset_type
    if database_name:
        where.append("DATABASE_NAME = %(database_name)s")
        params["database_name"] = database_name
    if schema_name:
        where.append("SCHEMA_NAME = %(schema_name)s")
        params["schema_name"] = schema_name
    if table_name:
        where.append("TABLE_NAME = %(table_name)s")
        params["table_name"] = table_name
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    total_rows = sf_session.query(f"SELECT COUNT(*) AS CNT FROM ASSETS {where_sql}", params)
    total = total_rows[0]["CNT"] if total_rows else 0

    rows = sf_session.query(
        f"""
        SELECT * FROM ASSETS {where_sql}
        ORDER BY CREATED_AT DESC
        LIMIT %(limit)s OFFSET %(skip)s
        """,
        {**params, "limit": limit, "skip": skip},
    )
    return total, [_asset_from_row(r) for r in rows]


def create_or_update_asset(
    fqn: str,
    asset_type: str,
    database_name: str,
    schema_name: Optional[str] = None,
    table_name: Optional[str] = None,
    column_name: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> SimpleNamespace:
    """Create the asset if FQN is new, else update it in place. Returns the asset."""
    metadata = metadata or {}
    existing = get_asset_by_fqn(fqn)

    if existing:
        sf_session.execute(
            """
            UPDATE ASSETS
            SET OWNER = %(owner)s, COMMENT = %(comment)s, ROW_COUNT = %(row_count)s,
                SIZE_BYTES = %(size_bytes)s, RAW_METADATA = PARSE_JSON(%(raw_metadata)s),
                UPDATED_AT = CURRENT_TIMESTAMP()
            WHERE ID = %(id)s
            """,
            {
                "id": existing.id,
                "owner": metadata.get("owner"),
                "comment": metadata.get("comment"),
                "row_count": metadata.get("row_count"),
                "size_bytes": metadata.get("size_bytes"),
                "raw_metadata": _json_or_null(metadata),
            },
        )
        return get_asset(existing.id)

    asset_id = _new_id()
    sf_session.execute(
        """
        INSERT INTO ASSETS
            (ID, ASSET_TYPE, DATABASE_NAME, SCHEMA_NAME, TABLE_NAME, COLUMN_NAME,
             FQN, OWNER, COMMENT, ROW_COUNT, SIZE_BYTES, RAW_METADATA)
        SELECT
            %(id)s, %(asset_type)s, %(database_name)s, %(schema_name)s, %(table_name)s,
            %(column_name)s, %(fqn)s, %(owner)s, %(comment)s, %(row_count)s,
            %(size_bytes)s, PARSE_JSON(%(raw_metadata)s)
        """,
        {
            "id": asset_id,
            "asset_type": asset_type,
            "database_name": database_name,
            "schema_name": schema_name,
            "table_name": table_name,
            "column_name": column_name,
            "fqn": fqn,
            "owner": metadata.get("owner"),
            "comment": metadata.get("comment"),
            "row_count": metadata.get("row_count"),
            "size_bytes": metadata.get("size_bytes"),
            "raw_metadata": _json_or_null(metadata),
        },
    )
    return get_asset(asset_id)


def update_asset_last_scanned(asset_id: str) -> None:
    sf_session.execute(
        "UPDATE ASSETS SET LAST_SCANNED_AT = CURRENT_TIMESTAMP() WHERE ID = %(id)s",
        {"id": asset_id},
    )


def list_column_assets(database_name: str, schema_name: str, table_name: str) -> list[SimpleNamespace]:
    rows = sf_session.query(
        """
        SELECT * FROM ASSETS
        WHERE DATABASE_NAME = %(database_name)s AND SCHEMA_NAME = %(schema_name)s
          AND TABLE_NAME = %(table_name)s AND ASSET_TYPE = 'column'
        """,
        {"database_name": database_name, "schema_name": schema_name, "table_name": table_name},
    )
    return [_asset_from_row(r) for r in rows]


def get_table_asset(database_name: str, schema_name: str, table_name: str) -> Optional[SimpleNamespace]:
    rows = sf_session.query(
        """
        SELECT * FROM ASSETS
        WHERE DATABASE_NAME = %(database_name)s AND SCHEMA_NAME = %(schema_name)s
          AND TABLE_NAME = %(table_name)s AND ASSET_TYPE = 'table'
        """,
        {"database_name": database_name, "schema_name": schema_name, "table_name": table_name},
    )
    return _asset_from_row(rows[0]) if rows else None


# ═══════════════════════════════════════════════════════════════════════════
# SCANS
# ═══════════════════════════════════════════════════════════════════════════

def _scan_from_row(row: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id=row["ID"],
        asset_id=row["ASSET_ID"],
        connection_id=row.get("CONNECTION_ID"),
        scan_type=row["SCAN_TYPE"],
        status=row["STATUS"],
        started_at=row["STARTED_AT"],
        completed_at=row["COMPLETED_AT"],
        rules_checked=row["RULES_CHECKED"],
        findings_count=row["FINDINGS_COUNT"],
        error_message=row["ERROR_MESSAGE"],
        scan_config=_parse_json(row.get("SCAN_CONFIG")),
        scan_results=_parse_json(row.get("SCAN_RESULTS")),
        created_at=row["CREATED_AT"],
    )


def create_scan(
    asset_id: str,
    scan_type: str = "metadata",
    status: str = "pending",
    started_at: Optional[datetime.datetime] = None,
    connection_id: Optional[str] = None,
) -> SimpleNamespace:
    scan_id = _new_id()
    sf_session.execute(
        """
        INSERT INTO SCANS (ID, ASSET_ID, CONNECTION_ID, SCAN_TYPE, STATUS, STARTED_AT)
        VALUES (%(id)s, %(asset_id)s, %(connection_id)s, %(scan_type)s, %(status)s,
                %(started_at)s)
        """,
        {
            "id": scan_id,
            "asset_id": asset_id,
            "connection_id": connection_id,
            "scan_type": scan_type,
            "status": status,
            "started_at": started_at,
        },
    )
    return get_scan(scan_id)


def get_scan(scan_id: str) -> Optional[SimpleNamespace]:
    rows = sf_session.query("SELECT * FROM SCANS WHERE ID = %(id)s", {"id": scan_id})
    return _scan_from_row(rows[0]) if rows else None


def list_scans(asset_id: Optional[str] = None, limit: int = 50) -> list[SimpleNamespace]:
    where_sql = "WHERE ASSET_ID = %(asset_id)s" if asset_id else ""
    params = {"asset_id": asset_id} if asset_id else {}
    rows = sf_session.query(
        f"SELECT * FROM SCANS {where_sql} ORDER BY CREATED_AT DESC LIMIT %(limit)s",
        {**params, "limit": limit},
    )
    return [_scan_from_row(r) for r in rows]


def update_scan(scan_id: str, **fields: Any) -> SimpleNamespace:
    """Partial update. Supports: asset_id, status, started_at, completed_at,
    rules_checked, findings_count, error_message, scan_config, scan_results."""
    json_cols = {"scan_config", "scan_results"}
    sets, params = [], {"id": scan_id}
    for key, value in fields.items():
        col = key.upper()
        if key in json_cols:
            sets.append(f"{col} = PARSE_JSON(%({key})s)")
            params[key] = _json_or_null(value)
        else:
            sets.append(f"{col} = %({key})s")
            params[key] = value
    if sets:
        sf_session.execute(f"UPDATE SCANS SET {', '.join(sets)} WHERE ID = %(id)s", params)
    return get_scan(scan_id)


# ═══════════════════════════════════════════════════════════════════════════
# FINDINGS
# ═══════════════════════════════════════════════════════════════════════════

def _finding_from_row(row: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id=row["ID"],
        asset_id=row["ASSET_ID"],
        scan_id=row["SCAN_ID"],
        instance_id=row["INSTANCE_ID"],
        title=row["TITLE"],
        description=row["DESCRIPTION"],
        status=row["STATUS"],
        severity=row["SEVERITY"],
        context=_parse_json(row.get("CONTEXT")),
        evidence=_parse_json(row.get("EVIDENCE")),
        assigned_to=row["ASSIGNED_TO"],
        resolution_notes=row["RESOLUTION_NOTES"],
        detected_at=row["DETECTED_AT"],
        validated_at=row["VALIDATED_AT"],
        resolved_at=row["RESOLVED_AT"],
        closed_at=row["CLOSED_AT"],
        updated_at=row["UPDATED_AT"],
        # Incident-lifecycle columns (04_migrations). Older rows may not have
        # them backfilled yet, so fall back to DETECTED_AT for compatibility.
        first_detected_at=row.get("FIRST_DETECTED_AT") or row["DETECTED_AT"],
        last_seen_at=row.get("LAST_SEEN_AT") or row["UPDATED_AT"],
        last_scan_id=row.get("LAST_SCAN_ID") or row["SCAN_ID"],
        reopened_count=row.get("REOPENED_COUNT") or 0,
        current_fail_count=row.get("CURRENT_FAIL_COUNT"),
        current_total_count=row.get("CURRENT_TOTAL_COUNT"),
        fail_history=_parse_json(row.get("FAIL_HISTORY")) or [],
    )


def create_finding(
    asset_id: str,
    scan_id: str,
    instance_id: Optional[str],
    title: str,
    description: str,
    severity: str,
    status: str = "detected",
    context: Optional[dict] = None,
    evidence: Optional[dict] = None,
) -> SimpleNamespace:
    finding_id = _new_id()
    sf_session.execute(
        """
        INSERT INTO FINDINGS
            (ID, ASSET_ID, SCAN_ID, INSTANCE_ID, TITLE, DESCRIPTION, STATUS, SEVERITY,
             CONTEXT, EVIDENCE)
        SELECT
            %(id)s, %(asset_id)s, %(scan_id)s, %(instance_id)s, %(title)s, %(description)s,
            %(status)s, %(severity)s, PARSE_JSON(%(context)s), PARSE_JSON(%(evidence)s)
        """,
        {
            "id": finding_id,
            "asset_id": asset_id,
            "scan_id": scan_id,
            "instance_id": instance_id,
            "title": title,
            "description": description,
            "status": status,
            "severity": severity,
            "context": _json_or_null(context),
            "evidence": _json_or_null(evidence),
        },
    )
    return get_finding(finding_id)


def create_findings_bulk(findings_data: list[dict]) -> list[SimpleNamespace]:
    """Insert many finding dicts (same shape as create_finding's kwargs, plus
    optional 'status' defaulting to 'detected'). Returns the created findings."""
    created = []
    for fd in findings_data:
        created.append(
            create_finding(
                asset_id=fd["asset_id"],
                scan_id=fd["scan_id"],
                instance_id=fd.get("instance_id"),
                title=fd["title"],
                description=fd["description"],
                severity=fd["severity"],
                status=fd.get("status", "detected"),
                context=fd.get("context"),
                evidence=fd.get("evidence"),
            )
        )
    return created


def get_finding(finding_id: str) -> Optional[SimpleNamespace]:
    rows = sf_session.query("SELECT * FROM FINDINGS WHERE ID = %(id)s", {"id": finding_id})
    return _finding_from_row(rows[0]) if rows else None


def _is_snowflake_connection(connection_id: Optional[str]) -> bool:
    """True when the connection is Snowflake-typed. Legacy findings/scans/runs
    predating connection tracking have a NULL connection and are attributed to
    the Snowflake source, so a Snowflake connection scope must also include
    those NULL rows (see _findings_connection_clause / list_agent_runs)."""
    if not connection_id:
        return False
    conn = get_connection_record(connection_id)
    if not conn:
        return False
    ctype = conn.type.value if hasattr(conn.type, "value") else str(conn.type)
    return (ctype or "").lower() == "snowflake"


def _findings_connection_clause(connection_id: str, params: dict, alias: str = "") -> str:
    """SQL predicate restricting FINDINGS to one connection, via the SCANS join
    (FINDINGS has no CONNECTION_ID column). `alias` (e.g. "f") qualifies the
    SCAN_ID column when the query aliases FINDINGS.

    Snowflake connections short-circuit to a no-op: the connection row is just
    credentials for the shared warehouse — deleting/recreating it must not hide
    historical findings — so all Snowflake findings are always visible."""
    # Snowflake connections are just credentials pointing at the shared
    # warehouse — the underlying data (and thus findings) belongs to Snowflake
    # itself, not to any particular CONNECTIONS row. Deleting/recreating a
    # connection row must not orphan historical findings, so we don't filter
    # Snowflake findings by connection_id at all.
    if _is_snowflake_connection(connection_id):
        return "1=1"
    params["conn_id"] = connection_id
    col = f"{alias}.SCAN_ID" if alias else "SCAN_ID"
    return f"{col} IN (SELECT ID FROM SCANS WHERE CONNECTION_ID = %(conn_id)s)"


def list_findings(
    asset_id: Optional[str] = None,
    scan_id: Optional[str] = None,
    status: Optional[str] = None,
    severity: Optional[str] = None,
    connection_id: Optional[str] = None,
    skip: int = 0,
    limit: int = 5000,
) -> tuple[int, list[SimpleNamespace]]:
    where, params = [], {}
    if asset_id:
        where.append("ASSET_ID = %(asset_id)s")
        params["asset_id"] = asset_id
    if scan_id:
        where.append("SCAN_ID = %(scan_id)s")
        params["scan_id"] = scan_id
    if connection_id:
        where.append(_findings_connection_clause(connection_id, params))
    if status:
        where.append("STATUS = %(status)s")
        params["status"] = status
    else:
        # 'superseded' findings are stale rows replaced by a newer scan's run —
        # never surface them by default (they'd inflate totals and clutter the
        # list). Only shown if explicitly filtered for.
        where.append("STATUS <> 'superseded'")
    if severity:
        where.append("SEVERITY = %(severity)s")
        params["severity"] = severity
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    total_rows = sf_session.query(f"SELECT COUNT(*) AS CNT FROM FINDINGS {where_sql}", params)
    total = total_rows[0]["CNT"] if total_rows else 0

    rows = sf_session.query(
        f"""
        SELECT * FROM FINDINGS {where_sql}
        ORDER BY DETECTED_AT DESC
        LIMIT %(limit)s OFFSET %(skip)s
        """,
        {**params, "limit": limit, "skip": skip},
    )
    result = []
    for r in rows:
        try:
            result.append(_finding_from_row(r))
        except Exception as e:
            logger.warning(f"[storage] Skipping malformed finding {r.get('ID')}: {e}")
    return total, result


def list_findings_by_scan(scan_id: str) -> list[SimpleNamespace]:
    rows = sf_session.query("SELECT * FROM FINDINGS WHERE SCAN_ID = %(scan_id)s", {"scan_id": scan_id})
    return [_finding_from_row(r) for r in rows]


def update_finding(finding_id: str, **fields: Any) -> SimpleNamespace:
    """Partial update. Supports: status, assigned_to, resolution_notes,
    validated_at, resolved_at, closed_at, updated_at."""
    sets, params = [], {"id": finding_id}
    for key, value in fields.items():
        sets.append(f"{key.upper()} = %({key})s")
        params[key] = value
    if "updated_at" not in fields:
        sets.append("UPDATED_AT = CURRENT_TIMESTAMP()")
    if sets:
        sf_session.execute(f"UPDATE FINDINGS SET {', '.join(sets)} WHERE ID = %(id)s", params)
    return get_finding(finding_id)


def update_finding_evidence(finding_id: str, evidence: dict) -> None:
    """Update the EVIDENCE VARIANT column for a finding using PARSE_JSON."""
    sf_session.execute(
        """
        UPDATE FINDINGS
        SET EVIDENCE = PARSE_JSON(%(evidence)s), UPDATED_AT = CURRENT_TIMESTAMP()
        WHERE ID = %(id)s
        """,
        {"id": finding_id, "evidence": _json_or_null(evidence)},
    )


# Open (non-closed) finding statuses — anything not here is considered resolved/
# closed and won't be superseded or shown under "Detected".
_OPEN_FINDING_STATUSES = ("detected", "validated", "in_progress")


def findings_with_asset_not_closed(
    connection_id: Optional[str] = None,
) -> list[tuple[SimpleNamespace, SimpleNamespace]]:
    """(finding, asset) pairs for every finding not RESOLVED/CLOSED — dashboard chart.
    Optionally scoped to one connection via the FINDINGS → SCANS.CONNECTION_ID join."""
    params: dict = {}
    conn_sql = ""
    if connection_id:
        conn_sql = f"AND {_findings_connection_clause(connection_id, params, alias='f')}"
    rows = sf_session.query(
        f"""
        SELECT f.*, a.DATABASE_NAME AS A_DATABASE_NAME, a.SCHEMA_NAME AS A_SCHEMA_NAME,
               a.TABLE_NAME AS A_TABLE_NAME, a.FQN AS A_FQN
        FROM FINDINGS f
        JOIN ASSETS a ON a.ID = f.ASSET_ID
        WHERE f.STATUS NOT IN ('resolved', 'closed', 'superseded')
        {conn_sql}
        """,
        params,
    )
    result = []
    for row in rows:
        finding = _finding_from_row(row)
        asset = SimpleNamespace(
            database_name=row["A_DATABASE_NAME"],
            schema_name=row["A_SCHEMA_NAME"],
            table_name=row["A_TABLE_NAME"],
            fqn=row["A_FQN"],
        )
        result.append((finding, asset))
    return result


def findings_summary(connection_id: Optional[str] = None) -> dict:
    """total / by_status / by_severity counts — dashboard stats. Excludes
    'superseded' (stale rows replaced by a newer scan) so counts reflect real
    current findings, not re-scan duplicates. Optionally scoped to one
    connection via the FINDINGS → SCANS.CONNECTION_ID join."""
    params: dict = {}
    conn_sql = ""
    if connection_id:
        conn_sql = f"AND {_findings_connection_clause(connection_id, params)}"
    base = f"FROM FINDINGS WHERE STATUS <> 'superseded' {conn_sql}"

    total_rows = sf_session.query(f"SELECT COUNT(*) AS CNT {base}", params)
    total = total_rows[0]["CNT"] if total_rows else 0

    status_rows = sf_session.query(f"SELECT STATUS, COUNT(*) AS CNT {base} GROUP BY STATUS", params)
    by_status = {r["STATUS"]: r["CNT"] for r in status_rows}

    severity_rows = sf_session.query(f"SELECT SEVERITY, COUNT(*) AS CNT {base} GROUP BY SEVERITY", params)
    by_severity = {r["SEVERITY"]: r["CNT"] for r in severity_rows}

    return {"total": total, "by_status": by_status, "by_severity": by_severity}


# ═══════════════════════════════════════════════════════════════════════════
# RULE_DEFINITIONS — the rule library (the concept: what a check means)
# ═══════════════════════════════════════════════════════════════════════════

def _definition_from_row(row: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id=row["ID"],
        name=row["NAME"],
        category=row["CATEGORY"],
        description=row["DESCRIPTION"],
        check_kind=row["CHECK_KIND"],
        handler_key=row["HANDLER_KEY"],
        template_shape=row.get("TEMPLATE_SHAPE"),
        sql_template=row["SQL_TEMPLATE"],
        parameters_schema=_parse_json(row.get("PARAMETERS_SCHEMA")),
        default_threshold_config=_parse_json(row.get("DEFAULT_THRESHOLD_CONFIG")),
        default_severity=row["DEFAULT_SEVERITY"],
        allowed_scopes=_parse_json(row.get("ALLOWED_SCOPES")) or [],
        source=row["SOURCE"],
        status=row["STATUS"],
        instance_count=row["INSTANCE_COUNT"] or 0,
        approval_count=row["APPROVAL_COUNT"] or 0,
        owner=row["OWNER"],
        created_by=row["CREATED_BY"],
        created_at=row["CREATED_AT"],
        updated_at=row["UPDATED_AT"],
    )


def get_definition(definition_id: str) -> Optional[SimpleNamespace]:
    rows = sf_session.query("SELECT * FROM RULE_DEFINITIONS WHERE ID = %(id)s", {"id": definition_id})
    return _definition_from_row(rows[0]) if rows else None


def get_definitions_by_ids(definition_ids: list[str]) -> Dict[str, SimpleNamespace]:
    """Batch-fetch definitions in ONE query, keyed by id. Callers resolving a
    definition per instance in a loop (RuleEngine.get_active_instances) used
    to call get_definition() once per instance — an N+1 pattern that turned a
    24-instance scan into 24+ sequential Snowflake round-trips. Empty input
    short-circuits without a query (an empty SQL IN-list is invalid)."""
    if not definition_ids:
        return {}
    unique_ids = list(dict.fromkeys(definition_ids))
    placeholders = ", ".join(f"%(id_{i})s" for i in range(len(unique_ids)))
    params = {f"id_{i}": d_id for i, d_id in enumerate(unique_ids)}
    rows = sf_session.query(
        f"SELECT * FROM RULE_DEFINITIONS WHERE ID IN ({placeholders})", params,
    )
    return {row["ID"]: _definition_from_row(row) for row in rows}


def get_definition_by_handler_key(handler_key: str) -> Optional[SimpleNamespace]:
    rows = sf_session.query(
        "SELECT * FROM RULE_DEFINITIONS WHERE HANDLER_KEY = %(handler_key)s",
        {"handler_key": handler_key},
    )
    return _definition_from_row(rows[0]) if rows else None


def get_definition_by_template_shape(template_shape: str) -> Optional[SimpleNamespace]:
    """Exact lookup for the canonical, system-wide definition backing a
    sql_template shape (not_null, uniqueness, ...) — the fix for definition-
    library explosion: callers check this BEFORE falling back to fuzzy
    name/description similarity or creating a brand-new definition, so every
    table/column proposing the same shape reuses one definition via its own
    TARGET_CONFIG/THRESHOLD_CONFIG/SEVERITY/RATIONALE instead of spawning a
    duplicate. When more than one active definition exists for the shape,
    prefer the one with the most LIVE active instances — computed from
    RULE_INSTANCES, not the monotonic RULE_DEFINITIONS.APPROVAL_COUNT column,
    which never decrements on rejection/disable and so points at the most
    historically-approved definition rather than the most currently useful
    one (audit finding #8)."""
    rows = sf_session.query(
        """
        SELECT d.*, COALESCE(i.ACTIVE_COUNT, 0) AS ACTIVE_COUNT
        FROM RULE_DEFINITIONS d
        LEFT JOIN (
            SELECT DEFINITION_ID, COUNT(*) AS ACTIVE_COUNT
            FROM RULE_INSTANCES
            WHERE STATUS = 'active'
            GROUP BY DEFINITION_ID
        ) i ON i.DEFINITION_ID = d.ID
        WHERE d.TEMPLATE_SHAPE = %(shape)s AND d.STATUS != 'disabled'
        ORDER BY ACTIVE_COUNT DESC, d.CREATED_AT ASC
        LIMIT 1
        """,
        {"shape": template_shape},
    )
    return _definition_from_row(rows[0]) if rows else None


def list_definitions(
    source: Optional[str] = None,
    status: Optional[str] = None,
    category: Optional[str] = None,
    skip: int = 0,
    limit: int = 500,
) -> tuple[int, list[SimpleNamespace]]:
    where, where_prefixed, params = [], [], {}
    if source:
        where.append("SOURCE = %(source)s")
        where_prefixed.append("d.SOURCE = %(source)s")
        params["source"] = source
    if status:
        where.append("STATUS = %(status)s")
        where_prefixed.append("d.STATUS = %(status)s")
        params["status"] = status
    if category:
        where.append("CATEGORY = %(category)s")
        where_prefixed.append("d.CATEGORY = %(category)s")
        params["category"] = category
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    where_sql_prefixed = f"WHERE {' AND '.join(where_prefixed)}" if where_prefixed else ""

    total_rows = sf_session.query(f"SELECT COUNT(*) AS CNT FROM RULE_DEFINITIONS {where_sql}", params)
    total = total_rows[0]["CNT"] if total_rows else 0

    # Rank by live active-instance count instead of the monotonic
    # APPROVAL_COUNT column (audit finding #8) so the Rules Library shows the
    # definitions actually used today at the top, not the ones with the most
    # historical approvals (which never decrements on rejection/disable).
    rows = sf_session.query(
        f"""
        SELECT d.*, COALESCE(i.ACTIVE_COUNT, 0) AS ACTIVE_COUNT
        FROM RULE_DEFINITIONS d
        LEFT JOIN (
            SELECT DEFINITION_ID, COUNT(*) AS ACTIVE_COUNT
            FROM RULE_INSTANCES
            WHERE STATUS = 'active'
            GROUP BY DEFINITION_ID
        ) i ON i.DEFINITION_ID = d.ID
        {where_sql_prefixed}
        ORDER BY ACTIVE_COUNT DESC, d.CREATED_AT DESC
        LIMIT %(limit)s OFFSET %(skip)s
        """,
        {**params, "limit": limit, "skip": skip},
    )
    return total, [_definition_from_row(r) for r in rows]


def list_all_definitions() -> list[SimpleNamespace]:
    rows = sf_session.query("SELECT * FROM RULE_DEFINITIONS")
    return [_definition_from_row(r) for r in rows]


def list_active_definitions() -> list[SimpleNamespace]:
    """Single-query fetch of all active definitions — no COUNT, no JOIN.
    Used by RulesFetchAgent where only the definition objects are needed."""
    rows = sf_session.query(
        "SELECT * FROM RULE_DEFINITIONS WHERE STATUS = 'active' ORDER BY CREATED_AT DESC"
    )
    return [_definition_from_row(r) for r in rows]


def get_real_instance_counts() -> dict[str, int]:
    """Actual RULE_INSTANCES row count per definition_id, computed live.
    RULE_DEFINITIONS.INSTANCE_COUNT is only ever incremented on create — it
    has no decrement, so any row removed by something other than the normal
    app flow (e.g. a manual cleanup) leaves it stale. Callers that display
    instance counts to a human should use this, not the stored column."""
    rows = sf_session.query(
        "SELECT DEFINITION_ID, COUNT(*) AS CNT FROM RULE_INSTANCES GROUP BY DEFINITION_ID"
    )
    return {r["DEFINITION_ID"]: r["CNT"] for r in rows}


def create_definition(
    name: str,
    category: str,
    description: str,
    check_kind: str,
    default_severity: str,
    allowed_scopes: list[str],
    handler_key: Optional[str] = None,
    sql_template: Optional[str] = None,
    template_shape: Optional[str] = None,
    parameters_schema: Optional[dict] = None,
    default_threshold_config: Optional[dict] = None,
    source: str = "system",
    status: str = "active",
    owner: Optional[str] = None,
    created_by: Optional[str] = None,
) -> SimpleNamespace:
    definition_id = _new_id()
    sf_session.execute(
        """
        INSERT INTO RULE_DEFINITIONS
            (ID, NAME, CATEGORY, DESCRIPTION, CHECK_KIND, HANDLER_KEY, SQL_TEMPLATE,
             TEMPLATE_SHAPE, PARAMETERS_SCHEMA, DEFAULT_THRESHOLD_CONFIG, DEFAULT_SEVERITY,
             ALLOWED_SCOPES, SOURCE, STATUS, OWNER, CREATED_BY)
        SELECT
            %(id)s, %(name)s, %(category)s, %(description)s, %(check_kind)s, %(handler_key)s,
            %(sql_template)s, %(template_shape)s, PARSE_JSON(%(parameters_schema)s),
            PARSE_JSON(%(default_threshold_config)s), %(default_severity)s, PARSE_JSON(%(allowed_scopes)s),
            %(source)s, %(status)s, %(owner)s, %(created_by)s
        """,
        {
            "id": definition_id,
            "name": name,
            "category": category,
            "description": description,
            "check_kind": check_kind,
            "handler_key": handler_key,
            "sql_template": sql_template,
            "template_shape": template_shape,
            "parameters_schema": _json_or_null(parameters_schema),
            "default_threshold_config": _json_or_null(default_threshold_config),
            "default_severity": default_severity,
            "allowed_scopes": _json_or_null(allowed_scopes),
            "source": source,
            "status": status,
            "owner": owner,
            "created_by": created_by,
        },
    )
    return get_definition(definition_id)


def ensure_definition(
    handler_key: str,
    name: str,
    description: str,
    category: str,
    severity: str,
    allowed_scopes: list[str],
) -> SimpleNamespace:
    """Return the python_handler definition for `handler_key`, auto-creating it
    (as system/active) if missing.

    NOTE 2026-07-15: this used to also call ensure_global_instance() to create
    a DATABASE_NAME='*' instance so the check auto-fired on every scan of
    every table. That model is gone — a python_handler definition now runs on
    a table only when RuleIntelligence proposed it (with human review) as a
    per-table instance. The definition still exists in the library; Claude
    picks it per table like any other check. See rule_engine.initialize_default_rules
    and dynamic_rules._ensure_rule for the callers."""
    existing = get_definition_by_handler_key(handler_key)
    if not existing:
        existing = create_definition(
            name=name,
            category=category,
            description=description,
            check_kind="python_handler",
            handler_key=handler_key,
            default_severity=severity,
            allowed_scopes=allowed_scopes,
            source="system",
            status="active",
            owner="data-governance-team",
            created_by="system",
        )
    return existing


def ensure_template_definition(
    template_shape: str,
    name: str,
    description: str,
    category: str,
    severity: str,
    allowed_scopes: Optional[list[str]] = None,
) -> SimpleNamespace:
    """Return the canonical sql_template definition for `template_shape`,
    auto-creating it (as system/active) if missing. Mirrors ensure_definition
    but for the sql_template shapes — called by initialize_default_rules so
    they survive any full DB wipe and are always available for RuleIntelligence
    to find via get_definition_by_template_shape.

    allowed_scopes defaults to ["column"] for the 8 canonical shapes;
    anomaly shapes at table level pass ["table"]."""
    existing = get_definition_by_template_shape(template_shape)
    if not existing:
        existing = create_definition(
            name=name,
            category=category,
            description=description,
            check_kind="sql_template",
            template_shape=template_shape,
            default_severity=severity,
            allowed_scopes=allowed_scopes or ["column"],
            source="system",
            status="active",
            owner="data-governance-team",
            created_by="system",
        )
    return existing


def update_definition(definition_id: str, **fields: Any) -> SimpleNamespace:
    """Partial update. JSON fields (parameters_schema, default_threshold_config,
    allowed_scopes) are auto-detected."""
    json_cols = {"parameters_schema", "default_threshold_config", "allowed_scopes"}
    sets, params = [], {"id": definition_id}
    for key, value in fields.items():
        col = key.upper()
        if key in json_cols:
            sets.append(f"{col} = PARSE_JSON(%({key})s)")
            params[key] = _json_or_null(value)
        else:
            sets.append(f"{col} = %({key})s")
            params[key] = value
    if "updated_at" not in fields:
        sets.append("UPDATED_AT = CURRENT_TIMESTAMP()")
    if sets:
        sf_session.execute(f"UPDATE RULE_DEFINITIONS SET {', '.join(sets)} WHERE ID = %(id)s", params)
    return get_definition(definition_id)


def increment_definition_instance_count(definition_id: str, delta: int = 1) -> None:
    sf_session.execute(
        "UPDATE RULE_DEFINITIONS SET INSTANCE_COUNT = INSTANCE_COUNT + %(delta)s WHERE ID = %(id)s",
        {"id": definition_id, "delta": delta},
    )


def increment_definition_approval_count(definition_id: str, delta: int = 1) -> None:
    sf_session.execute(
        "UPDATE RULE_DEFINITIONS SET APPROVAL_COUNT = APPROVAL_COUNT + %(delta)s WHERE ID = %(id)s",
        {"id": definition_id, "delta": delta},
    )


# ═══════════════════════════════════════════════════════════════════════════
# RULE_INSTANCES — a specific application of a definition to a target
# ═══════════════════════════════════════════════════════════════════════════

def _instance_from_row(row: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id=row["ID"],
        definition_id=row["DEFINITION_ID"],
        scope=row["SCOPE"],
        database_name=row["DATABASE_NAME"],
        schema_name=row["SCHEMA_NAME"],
        table_name=row["TABLE_NAME"],
        target_config=_parse_json(row.get("TARGET_CONFIG")) or {},
        threshold_config=_parse_json(row.get("THRESHOLD_CONFIG")),
        severity=row["SEVERITY"],
        rule_sql=row["RULE_SQL"],
        rationale=row.get("RATIONALE"),
        status=row["STATUS"],
        fingerprint=row["FINGERPRINT"],
        is_active=row["IS_ACTIVE"],
        edited_by_human=row["EDITED_BY_HUMAN"],
        jira_ticket=row["JIRA_TICKET"],
        rejection_reason=row["REJECTION_REASON"],
        owner=row["OWNER"],
        created_by=row["CREATED_BY"],
        source_run_id=row["SOURCE_RUN_ID"],
        version=row["VERSION"],
        created_at=row["CREATED_AT"],
        updated_at=row["UPDATED_AT"],
        approved_at=row["APPROVED_AT"],
        rejected_at=row["REJECTED_AT"],
        approved_by=row.get("APPROVED_BY"),
        rejected_by=row.get("REJECTED_BY"),
    )


def get_instance(instance_id: str) -> Optional[SimpleNamespace]:
    rows = sf_session.query("SELECT * FROM RULE_INSTANCES WHERE ID = %(id)s", {"id": instance_id})
    return _instance_from_row(rows[0]) if rows else None


def get_instances_by_ids(instance_ids: list[str]) -> Dict[str, SimpleNamespace]:
    """Batch-fetch instances in ONE query, keyed by id — same N+1 fix as
    get_definitions_by_ids, for callers resolving instance rows in a loop
    (RuleEngine.execute_sql_instances)."""
    if not instance_ids:
        return {}
    unique_ids = list(dict.fromkeys(instance_ids))
    placeholders = ", ".join(f"%(id_{i})s" for i in range(len(unique_ids)))
    params = {f"id_{i}": i_id for i, i_id in enumerate(unique_ids)}
    rows = sf_session.query(
        f"SELECT * FROM RULE_INSTANCES WHERE ID IN ({placeholders})", params,
    )
    return {row["ID"]: _instance_from_row(row) for row in rows}


def get_instance_by_fingerprint(fingerprint: str) -> Optional[SimpleNamespace]:
    rows = sf_session.query(
        "SELECT * FROM RULE_INSTANCES WHERE FINGERPRINT = %(fp)s", {"fp": fingerprint}
    )
    return _instance_from_row(rows[0]) if rows else None


def list_instances(
    definition_id: Optional[str] = None,
    status: Optional[str] = None,
    scope: Optional[str] = None,
    database_name: Optional[str] = None,
    schema_name: Optional[str] = None,
    table_name: Optional[str] = None,
    is_active: Optional[bool] = None,
    skip: int = 0,
    limit: int = 500,
) -> tuple[int, list[SimpleNamespace]]:
    where, params = [], {}
    if definition_id:
        where.append("DEFINITION_ID = %(definition_id)s")
        params["definition_id"] = definition_id
    if status:
        where.append("STATUS = %(status)s")
        params["status"] = status
    if scope:
        where.append("SCOPE = %(scope)s")
        params["scope"] = scope
    if database_name:
        where.append("DATABASE_NAME = %(database_name)s")
        params["database_name"] = database_name
    if schema_name:
        where.append("SCHEMA_NAME = %(schema_name)s")
        params["schema_name"] = schema_name
    if table_name:
        where.append("TABLE_NAME = %(table_name)s")
        params["table_name"] = table_name
    if is_active is not None:
        where.append("IS_ACTIVE = %(is_active)s")
        params["is_active"] = is_active
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    total_rows = sf_session.query(f"SELECT COUNT(*) AS CNT FROM RULE_INSTANCES {where_sql}", params)
    total = total_rows[0]["CNT"] if total_rows else 0

    rows = sf_session.query(
        f"""
        SELECT * FROM RULE_INSTANCES {where_sql}
        ORDER BY CREATED_AT DESC
        LIMIT %(limit)s OFFSET %(skip)s
        """,
        {**params, "limit": limit, "skip": skip},
    )
    return total, [_instance_from_row(r) for r in rows]


def _instance_as_rule_view(instance: SimpleNamespace, definition: SimpleNamespace) -> SimpleNamespace:
    """Joins an instance + its definition into the flat `Rule`-shaped object
    the `rules.py` API / frontend still expects (code/name/description/
    category/severity/applies_to/...). `code` is synthesized from
    HANDLER_KEY (upper-cased) for python_handler definitions, or the
    definition id for sql_template ones. This is a read view only — writes
    go through the definition/instance functions above, never this shape."""
    code = (definition.handler_key or definition.id).upper()
    return SimpleNamespace(
        id=instance.id,
        code=code,
        name=definition.name,
        description=definition.description,
        category=definition.category,
        severity=instance.severity,
        applies_to=definition.allowed_scopes or [],
        rule_config={"definition_id": definition.id, "check_kind": definition.check_kind},
        status=instance.status,
        jira_ticket=instance.jira_ticket,
        rejection_reason=instance.rejection_reason,
        owner=instance.owner,
        created_by=instance.created_by,
        version=instance.version,
        is_active=instance.is_active,
        created_at=instance.created_at,
        updated_at=instance.updated_at,
        approved_at=instance.approved_at,
        rejected_at=instance.rejected_at,
        approved_by=getattr(instance, "approved_by", None),
        rejected_by=getattr(instance, "rejected_by", None),
        source=definition.source,
    )


def list_rules_view(
    category: Optional[str] = None,
    severity: Optional[str] = None,
    status: Optional[str] = None,
    is_active: Optional[bool] = None,
    skip: int = 0,
    limit: int = 500,
) -> tuple[int, list[SimpleNamespace]]:
    """Rule-shaped view over RULE_INSTANCES joined to RULE_DEFINITIONS, for
    the rules.py API / frontend Rules page. category filters on the
    definition's category."""
    definitions_by_id = {d.id: d for d in list_all_definitions()}

    _, instances = list_instances(status=status, is_active=is_active, skip=0, limit=5000)
    views = []
    for inst in instances:
        definition = definitions_by_id.get(inst.definition_id)
        if not definition:
            continue
        if category and definition.category != category:
            continue
        if severity and inst.severity != severity:
            continue
        views.append(_instance_as_rule_view(inst, definition))

    views.sort(key=lambda v: v.created_at, reverse=True)
    total = len(views)
    return total, views[skip:skip + limit]


def get_rule_view(instance_id: str) -> Optional[SimpleNamespace]:
    instance = get_instance(instance_id)
    if not instance:
        return None
    definition = get_definition(instance.definition_id)
    if not definition:
        return None
    return _instance_as_rule_view(instance, definition)


def list_active_instances_for_scope(scope: str) -> list[SimpleNamespace]:
    """Active instances with a given scope ('table' | 'column' | ...), globally
    (DATABASE_NAME='*') or for a specific target — callers filter further by
    database/schema/table as needed.

    Guard: an instance is only returned if BOTH the instance is active AND its
    parent definition is 'active'. This prevents a stale IS_ACTIVE=TRUE row
    from firing after the definition was disabled in the Rule Library — the
    definition toggle also cascades to instances (see
    set_definition_active_state), but this join is a belt-and-suspenders check
    so scan paths that read instances directly never execute a disabled rule."""
    rows = sf_session.query(
        """
        SELECT I.*
        FROM RULE_INSTANCES I
        JOIN RULE_DEFINITIONS D ON D.ID = I.DEFINITION_ID
        WHERE I.IS_ACTIVE = TRUE
          AND I.SCOPE = %(scope)s
          AND D.STATUS = 'active'
        """,
        {"scope": scope},
    )
    return [_instance_from_row(r) for r in rows]


def set_definition_active_state(definition_id: str, is_active: bool) -> None:
    """Toggle a rule definition and CASCADE the new state to every child
    RULE_INSTANCES row. Both columns (IS_ACTIVE + STATUS) are kept in sync on
    each side so subsequent scans respect the toggle immediately."""
    new_def_status = "active" if is_active else "disabled"
    new_inst_status = "active" if is_active else "disabled"
    # Definition row
    sf_session.execute(
        """
        UPDATE RULE_DEFINITIONS
        SET STATUS = %(s)s, UPDATED_AT = CURRENT_TIMESTAMP()
        WHERE ID = %(id)s
        """,
        {"id": definition_id, "s": new_def_status},
    )
    # Cascade to every child instance so the scan-time filter picks it up.
    sf_session.execute(
        """
        UPDATE RULE_INSTANCES
        SET IS_ACTIVE = %(a)s, STATUS = %(s)s, UPDATED_AT = CURRENT_TIMESTAMP()
        WHERE DEFINITION_ID = %(id)s
        """,
        {"id": definition_id, "a": is_active, "s": new_inst_status},
    )


def create_instance(
    definition_id: str,
    scope: str,
    database_name: str,
    fingerprint: str,
    severity: str,
    schema_name: Optional[str] = None,
    table_name: Optional[str] = None,
    target_config: Optional[dict] = None,
    threshold_config: Optional[dict] = None,
    rule_sql: Optional[str] = None,
    rationale: Optional[str] = None,
    status: str = "active",
    is_active: bool = True,
    jira_ticket: Optional[str] = None,
    owner: str = "data-governance-team",
    created_by: Optional[str] = None,
    source_run_id: Optional[str] = None,
) -> SimpleNamespace:
    instance_id = _new_id()
    sf_session.execute(
        """
        INSERT INTO RULE_INSTANCES
            (ID, DEFINITION_ID, SCOPE, DATABASE_NAME, SCHEMA_NAME, TABLE_NAME,
             TARGET_CONFIG, THRESHOLD_CONFIG, SEVERITY, RULE_SQL, RATIONALE, STATUS, FINGERPRINT,
             IS_ACTIVE, JIRA_TICKET, OWNER, CREATED_BY, SOURCE_RUN_ID)
        SELECT
            %(id)s, %(definition_id)s, %(scope)s, %(database_name)s, %(schema_name)s,
            %(table_name)s, PARSE_JSON(%(target_config)s), PARSE_JSON(%(threshold_config)s),
            %(severity)s, %(rule_sql)s, %(rationale)s, %(status)s, %(fingerprint)s, %(is_active)s,
            %(jira_ticket)s, %(owner)s, %(created_by)s, %(source_run_id)s
        """,
        {
            "id": instance_id,
            "definition_id": definition_id,
            "scope": scope,
            "database_name": database_name,
            "schema_name": schema_name,
            "table_name": table_name,
            "target_config": _json_or_null(target_config if target_config is not None else {}),
            "threshold_config": _json_or_null(threshold_config),
            "severity": severity,
            "rule_sql": rule_sql,
            "rationale": rationale,
            "status": status,
            "fingerprint": fingerprint,
            "is_active": is_active,
            "jira_ticket": jira_ticket,
            "owner": owner,
            "created_by": created_by,
            "source_run_id": source_run_id,
        },
    )
    increment_definition_instance_count(definition_id)
    return get_instance(instance_id)


def ensure_global_instance(definition: SimpleNamespace) -> SimpleNamespace:
    """Static/dynamic python_handler checks have no per-table target — one
    degenerate instance (DATABASE_NAME='*', TARGET_CONFIG={}) represents
    'runs everywhere'. Auto-creates it if the definition doesn't have one yet."""
    existing_rows = sf_session.query(
        "SELECT * FROM RULE_INSTANCES WHERE DEFINITION_ID = %(id)s AND DATABASE_NAME = '*'",
        {"id": definition.id},
    )
    if existing_rows:
        return _instance_from_row(existing_rows[0])

    scope = "table" if "table" in (definition.allowed_scopes or []) else "column"
    fingerprint = _sha256(f"{definition.id}|global")
    return create_instance(
        definition_id=definition.id,
        scope=scope,
        database_name="*",
        fingerprint=fingerprint,
        severity=definition.default_severity,
        target_config={},
        status="active",
        is_active=True,
        owner=definition.owner or "data-governance-team",
        created_by=definition.created_by,
    )


def list_stale_pending_instances(
    database_name: str,
    schema_name: str,
    table_name: str,
    except_run_id: str,
) -> list[SimpleNamespace]:
    """Pending RULE_INSTANCES on this table from ANY source_run_id other than
    `except_run_id`. Used by the coordinator to sweep leftover proposals from
    prior runs that were never approved — without this, each new scan on a
    table accumulates review-panel clutter that Claude also sees as "already
    pending" and won't re-propose."""
    rows = sf_session.query(
        """
        SELECT * FROM RULE_INSTANCES
        WHERE UPPER(DATABASE_NAME) = UPPER(%(db)s)
          AND UPPER(SCHEMA_NAME)   = UPPER(%(sc)s)
          AND UPPER(TABLE_NAME)    = UPPER(%(tb)s)
          AND STATUS = 'pending'
          AND (SOURCE_RUN_ID IS NULL OR SOURCE_RUN_ID != %(run_id)s)
        """,
        {"db": database_name, "sc": schema_name, "tb": table_name, "run_id": except_run_id},
    )
    return [_instance_from_row(r) for r in rows]


def update_instance(instance_id: str, **fields: Any) -> SimpleNamespace:
    """Partial update. JSON fields (target_config, threshold_config) are
    auto-detected."""
    json_cols = {"target_config", "threshold_config"}
    sets, params = [], {"id": instance_id}
    for key, value in fields.items():
        col = key.upper()
        if key in json_cols:
            sets.append(f"{col} = PARSE_JSON(%({key})s)")
            params[key] = _json_or_null(value)
        else:
            sets.append(f"{col} = %({key})s")
            params[key] = value
    if "updated_at" not in fields:
        sets.append("UPDATED_AT = CURRENT_TIMESTAMP()")
    if sets:
        sf_session.execute(f"UPDATE RULE_INSTANCES SET {', '.join(sets)} WHERE ID = %(id)s", params)
    return get_instance(instance_id)


def approve_instance(instance_id: str, approved_by: Optional[str] = None) -> SimpleNamespace:
    fields = dict(status="active", is_active=True, approved_at=datetime.datetime.utcnow())
    if approved_by:
        fields["approved_by"] = approved_by
    instance = update_instance(instance_id, **fields)
    increment_definition_approval_count(instance.definition_id)
    return instance


def reject_instance(instance_id: str, reason: str, rejected_by: Optional[str] = None) -> SimpleNamespace:
    fields = dict(status="rejected", is_active=False,
                  rejection_reason=reason, rejected_at=datetime.datetime.utcnow())
    if rejected_by:
        fields["rejected_by"] = rejected_by
    return update_instance(instance_id, **fields)


# ═══════════════════════════════════════════════════════════════════════════
# RELATIONSHIP_CATALOG — discovered cross-table FK relationships, cached
# per (database, schema) so RuleIntelligenceAgent's referential_integrity
# proposals have real ref_table/ref_column candidates to draw from instead
# of never being reachable (see relationship_discovery.py).
# ═══════════════════════════════════════════════════════════════════════════

def _relationship_from_row(row: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id=row["ID"],
        database_name=row["DATABASE_NAME"],
        schema_name=row["SCHEMA_NAME"],
        from_table=row["FROM_TABLE"],
        from_column=row["FROM_COLUMN"],
        to_table=row["TO_TABLE"],
        to_column=row["TO_COLUMN"],
        status=row["STATUS"],
        confidence=row["CONFIDENCE"],
        orphan_rate=row["ORPHAN_RATE"],
        sample_total=row["SAMPLE_TOTAL"],
        sample_orphans=row["SAMPLE_ORPHANS"],
        discovered_at=row["DISCOVERED_AT"],
        last_verified_at=row["LAST_VERIFIED_AT"],
        created_at=row["CREATED_AT"],
    )


def list_relationships(
    database_name: str,
    schema_name: str,
    status: Optional[str] = None,
) -> list[SimpleNamespace]:
    where = ["DATABASE_NAME = %(database_name)s", "SCHEMA_NAME = %(schema_name)s"]
    params = {"database_name": database_name, "schema_name": schema_name}
    if status:
        where.append("STATUS = %(status)s")
        params["status"] = status
    rows = sf_session.query(
        f"SELECT * FROM RELATIONSHIP_CATALOG WHERE {' AND '.join(where)} ORDER BY FROM_TABLE, FROM_COLUMN",
        params,
    )
    return [_relationship_from_row(r) for r in rows]


def replace_relationships(
    database_name: str,
    schema_name: str,
    rows: list[dict],
) -> list[SimpleNamespace]:
    """Replace the ENTIRE relationship catalog for one (database, schema) with a
    fresh set, in 2 round-trips (one DELETE + one multi-row INSERT) instead of
    3 per candidate. A discovery run always recomputes the full candidate set,
    so delete-all-for-schema + insert-all is correct and far cheaper than
    per-row upsert-diffing. Each row dict may carry: from_table, from_column,
    to_table, to_column, status, confidence, orphan_rate, sample_total,
    sample_orphans. Returns the freshly-stored rows."""
    sf_session.execute(
        "DELETE FROM RELATIONSHIP_CATALOG WHERE DATABASE_NAME = %(database_name)s AND SCHEMA_NAME = %(schema_name)s",
        {"database_name": database_name, "schema_name": schema_name},
    )
    if not rows:
        return []

    # One multi-row INSERT ... SELECT ... UNION ALL. Snowflake rejects function
    # calls (CURRENT_TIMESTAMP()) inside a multi-row VALUES clause, so we use the
    # SELECT form — same reason the single-row upsert above uses INSERT ... SELECT.
    select_clauses = []
    params: Dict[str, Any] = {"database_name": database_name, "schema_name": schema_name}
    for i, r in enumerate(rows):
        select_clauses.append(
            f"SELECT %(id{i})s, %(database_name)s, %(schema_name)s, %(ft{i})s, %(fc{i})s, "
            f"%(tt{i})s, %(tc{i})s, %(st{i})s, %(cf{i})s, %(orr{i})s, %(stot{i})s, %(sorp{i})s, "
            f"CURRENT_TIMESTAMP()"
        )
        params[f"id{i}"]   = _new_id()
        params[f"ft{i}"]   = r.get("from_table")
        params[f"fc{i}"]   = r.get("from_column")
        params[f"tt{i}"]   = r.get("to_table")
        params[f"tc{i}"]   = r.get("to_column")
        params[f"st{i}"]   = r.get("status", "confirmed")
        params[f"cf{i}"]   = r.get("confidence", "name_match")
        params[f"orr{i}"]  = r.get("orphan_rate")
        params[f"stot{i}"] = r.get("sample_total")
        params[f"sorp{i}"] = r.get("sample_orphans")

    sf_session.execute(
        f"""
        INSERT INTO RELATIONSHIP_CATALOG
            (ID, DATABASE_NAME, SCHEMA_NAME, FROM_TABLE, FROM_COLUMN, TO_TABLE, TO_COLUMN,
             STATUS, CONFIDENCE, ORPHAN_RATE, SAMPLE_TOTAL, SAMPLE_ORPHANS, LAST_VERIFIED_AT)
        {' UNION ALL '.join(select_clauses)}
        """,
        params,
    )
    return list_relationships(database_name, schema_name)


# ═══════════════════════════════════════════════════════════════════════════
# RULE_EXECUTIONS — one row per instance per run (pass/fail/error log)
# ═══════════════════════════════════════════════════════════════════════════

def _execution_from_row(row: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id=row["ID"],
        instance_id=row["INSTANCE_ID"],
        scan_id=row["SCAN_ID"],
        run_id=row["RUN_ID"],
        status=row["STATUS"],
        evidence=_parse_json(row.get("EVIDENCE")),
        executed_at=row["EXECUTED_AT"],
    )


def create_execution(
    instance_id: str,
    status: str,
    scan_id: Optional[str] = None,
    run_id: Optional[str] = None,
    evidence: Optional[dict] = None,
) -> SimpleNamespace:
    execution_id = _new_id()
    sf_session.execute(
        """
        INSERT INTO RULE_EXECUTIONS (ID, INSTANCE_ID, SCAN_ID, RUN_ID, STATUS, EVIDENCE)
        SELECT %(id)s, %(instance_id)s, %(scan_id)s, %(run_id)s, %(status)s, PARSE_JSON(%(evidence)s)
        """,
        {
            "id": execution_id,
            "instance_id": instance_id,
            "scan_id": scan_id,
            "run_id": run_id,
            "status": status,
            "evidence": _json_or_null(evidence),
        },
    )
    rows = sf_session.query("SELECT * FROM RULE_EXECUTIONS WHERE ID = %(id)s", {"id": execution_id})
    return _execution_from_row(rows[0])


def list_executions_for_instance(instance_id: str, limit: int = 50) -> list[SimpleNamespace]:
    rows = sf_session.query(
        "SELECT * FROM RULE_EXECUTIONS WHERE INSTANCE_ID = %(id)s ORDER BY EXECUTED_AT DESC LIMIT %(limit)s",
        {"id": instance_id, "limit": limit},
    )
    return [_execution_from_row(r) for r in rows]


def list_executions_for_instances(
    instance_ids: list[str], limit_per_instance: int = 50,
) -> Dict[str, list[SimpleNamespace]]:
    """Batched version of list_executions_for_instance — one query for all
    instance_ids instead of one query per instance. Used by table-health
    aggregation to avoid N+1 round-trips to Snowflake."""
    if not instance_ids:
        return {}
    placeholders = ", ".join(f"%(iid{n})s" for n in range(len(instance_ids)))
    params = {f"iid{n}": iid for n, iid in enumerate(instance_ids)}
    params["limit"] = limit_per_instance
    rows = sf_session.query(
        f"""
        SELECT * FROM RULE_EXECUTIONS
        WHERE INSTANCE_ID IN ({placeholders})
        QUALIFY ROW_NUMBER() OVER (PARTITION BY INSTANCE_ID ORDER BY EXECUTED_AT DESC) <= %(limit)s
        ORDER BY INSTANCE_ID, EXECUTED_AT DESC
        """,
        params,
    )
    by_instance: Dict[str, list[SimpleNamespace]] = defaultdict(list)
    for r in rows:
        by_instance[r["INSTANCE_ID"]].append(_execution_from_row(r))
    return by_instance


# ═══════════════════════════════════════════════════════════════════════════
# AGENT_RUNS / AGENT_TASKS
# ═══════════════════════════════════════════════════════════════════════════

def _agent_run_from_row(row: dict, tasks: Optional[list] = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=row["ID"],
        connection_id=row.get("CONNECTION_ID"),
        batch_id=row["BATCH_ID"],
        batch_index=row["BATCH_INDEX"] or 0,
        database=row["DATABASE_NAME"],
        schema_name=row["SCHEMA_NAME"],
        table=row["TABLE_NAME"],
        status=row["STATUS"],
        scan_id=row["SCAN_ID"],
        started_at=row["STARTED_AT"],
        completed_at=row["COMPLETED_AT"],
        findings_count=row["FINDINGS_COUNT"] or 0,
        ai_rules_count=row["AI_RULES_COUNT"] or 0,
        instance_review_state=_parse_json(row.get("INSTANCE_REVIEW_STATE")),
        error_message=row["ERROR_MESSAGE"],
        created_at=row["CREATED_AT"],
        workflow_template_id=row.get("WORKFLOW_TEMPLATE_ID"),
        schedule_id=row.get("SCHEDULE_ID"),
        tasks=tasks if tasks is not None else [],
    )


def _agent_task_from_row(row: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id=row["ID"],
        run_id=row["RUN_ID"],
        agent_name=row["AGENT_NAME"],
        status=row["STATUS"],
        started_at=row["STARTED_AT"],
        completed_at=row["COMPLETED_AT"],
        output=_parse_json(row.get("OUTPUT")),
        error_message=row["ERROR_MESSAGE"],
        created_at=row["CREATED_AT"],
    )


def create_agent_run(
    database: str,
    schema_name: str,
    table: str,
    status: str = "pending",
    batch_id: Optional[str] = None,
    batch_index: int = 0,
    run_id: Optional[str] = None,
    connection_id: Optional[str] = None,
    workflow_template_id: Optional[str] = None,
    schedule_id: Optional[str] = None,
) -> SimpleNamespace:
    run_id = run_id or _new_id()
    sf_session.execute(
        """
        INSERT INTO AGENT_RUNS
            (ID, CONNECTION_ID, BATCH_ID, BATCH_INDEX, DATABASE_NAME, SCHEMA_NAME,
             TABLE_NAME, STATUS, WORKFLOW_TEMPLATE_ID, SCHEDULE_ID)
        VALUES
            (%(id)s, %(connection_id)s, %(batch_id)s, %(batch_index)s, %(database)s,
             %(schema_name)s, %(table)s, %(status)s, %(workflow_template_id)s, %(schedule_id)s)
        """,
        {
            "id": run_id,
            "connection_id": connection_id,
            "batch_id": batch_id,
            "batch_index": batch_index,
            "database": database,
            "schema_name": schema_name,
            "table": table,
            "status": status,
            "workflow_template_id": workflow_template_id,
            "schedule_id": schedule_id,
        },
    )
    return get_agent_run(run_id)


def create_agent_tasks(run_id: str, agent_names: list[str]) -> None:
    for name in agent_names:
        sf_session.execute(
            """
            INSERT INTO AGENT_TASKS (ID, RUN_ID, AGENT_NAME, STATUS)
            VALUES (%(id)s, %(run_id)s, %(agent_name)s, 'pending')
            """,
            {"id": _new_id(), "run_id": run_id, "agent_name": name},
        )


def create_agent_task(run_id: str, agent_name: str) -> SimpleNamespace:
    """Insert one AGENT_TASKS row and return it. Used by the coordinator's
    self-heal path when AGENT_ORDER gains a new agent but pre-existing runs
    have no row for it — creating on demand keeps state transitions visible
    instead of silently no-op'ing (audit finding #3)."""
    create_agent_tasks(run_id, [agent_name])
    return get_agent_task(run_id, agent_name)


def get_agent_run(run_id: str, with_tasks: bool = True) -> Optional[SimpleNamespace]:
    rows = sf_session.query("SELECT * FROM AGENT_RUNS WHERE ID = %(id)s", {"id": run_id})
    if not rows:
        return None
    tasks = list_agent_tasks(run_id) if with_tasks else []
    return _agent_run_from_row(rows[0], tasks=tasks)


def _agent_runs_where(
    status: str | None = None,
    origin: str | None = None,
    database: str | None = None,
    schema_name: str | None = None,
    table: str | None = None,
    search: str | None = None,
    connection_id: str | None = None,
) -> tuple[str, dict]:
    """Build a reusable WHERE clause + params for agent-run queries."""
    clauses = []
    params: dict = {}
    if status:
        clauses.append("STATUS = %(status)s")
        params["status"] = status
    if origin == "scheduled":
        clauses.append("SCHEDULE_ID IS NOT NULL")
    elif origin == "manual":
        clauses.append("SCHEDULE_ID IS NULL")
    if database:
        clauses.append("DATABASE_NAME = %(database)s")
        params["database"] = database
    if schema_name:
        clauses.append("SCHEMA_NAME = %(schema_name)s")
        params["schema_name"] = schema_name
    if table:
        clauses.append("TABLE_NAME = %(table)s")
        params["table"] = table
    if search:
        clauses.append(
            "(LOWER(DATABASE_NAME) LIKE %(search)s OR LOWER(SCHEMA_NAME) LIKE %(search)s OR LOWER(TABLE_NAME) LIKE %(search)s)"
        )
        params["search"] = f"%{search.lower()}%"
    if connection_id:
        clauses.append("CONNECTION_ID = %(connection_id)s")
        params["connection_id"] = connection_id
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def count_agent_runs(
    status: str | None = None,
    origin: str | None = None,
    database: str | None = None,
    schema_name: str | None = None,
    table: str | None = None,
    search: str | None = None,
    connection_id: str | None = None,
) -> int:
    where, params = _agent_runs_where(status, origin, database, schema_name, table, search, connection_id)
    rows = sf_session.query(f"SELECT COUNT(*) AS CNT FROM AGENT_RUNS {where}", params)
    return int(rows[0]["CNT"]) if rows else 0


def list_agent_runs(
    limit: int = 20,
    offset: int = 0,
    status: str | None = None,
    origin: str | None = None,
    database: str | None = None,
    schema_name: str | None = None,
    table: str | None = None,
    search: str | None = None,
    connection_id: str | None = None,
) -> list[SimpleNamespace]:
    where, params = _agent_runs_where(status, origin, database, schema_name, table, search, connection_id)
    params["limit"] = limit
    params["offset"] = offset
    rows = sf_session.query(
        f"SELECT * FROM AGENT_RUNS {where} ORDER BY CREATED_AT DESC LIMIT %(limit)s OFFSET %(offset)s",
        params,
    )
    result = []
    for r in rows:
        try:
            result.append(_agent_run_from_row(r, tasks=[]))
        except Exception as e:
            logger.warning(f"[storage] Skipping malformed run {r.get('ID')}: {e}")
    return result


def list_agent_run_filter_options() -> dict:
    """Distinct databases, schemas, and tables that appear in AGENT_RUNS,
    used to populate the cascading filter dropdowns in the Run History UI."""
    rows = sf_session.query(
        "SELECT DISTINCT DATABASE_NAME, SCHEMA_NAME, TABLE_NAME FROM AGENT_RUNS "
        "WHERE DATABASE_NAME IS NOT NULL ORDER BY DATABASE_NAME, SCHEMA_NAME, TABLE_NAME"
    )
    databases: list[str] = []
    schemas: dict[str, list[str]] = {}
    tables: dict[str, list[str]] = {}
    seen_db: set = set()
    for r in rows:
        db = r.get("DATABASE_NAME") or ""
        sc = r.get("SCHEMA_NAME") or ""
        tb = r.get("TABLE_NAME") or ""
        if db and db not in seen_db:
            databases.append(db)
            seen_db.add(db)
        if db and sc:
            schemas.setdefault(db, [])
            if sc not in schemas[db]:
                schemas[db].append(sc)
        if db and sc and tb:
            key = f"{db}.{sc}"
            tables.setdefault(key, [])
            if tb not in tables[key]:
                tables[key].append(tb)
    return {"databases": databases, "schemas": schemas, "tables": tables}


def recover_orphaned_runs() -> int:
    """Mark any AGENT_RUNS still stuck in 'running' as 'failed'.

    Called once at server startup. A run left in 'running' means the process
    was killed (restart, OOM, etc.) while a background thread was executing
    the pipeline — daemon threads die silently, so the DB row never gets
    updated. Without this, those rows show 'Running' in the UI forever and,
    if they were part of a batch, the batch never advances to the next table.

    Returns the number of runs recovered.
    """
    rows = sf_session.query(
        "SELECT ID, BATCH_ID FROM AGENT_RUNS WHERE STATUS = 'running'"
    )
    if not rows:
        return 0
    for r in rows:
        run_id = r["ID"]
        sf_session.execute(
            "UPDATE AGENT_RUNS SET STATUS = 'failed', "
            "COMPLETED_AT = CURRENT_TIMESTAMP(), "
            "ERROR_MESSAGE = 'Server restart: run was interrupted mid-execution.' "
            "WHERE ID = %(id)s",
            {"id": run_id},
        )
        # Mark any still-running tasks as failed too
        sf_session.execute(
            "UPDATE AGENT_TASKS SET STATUS = 'failed', "
            "COMPLETED_AT = CURRENT_TIMESTAMP(), "
            "ERROR_MESSAGE = 'Server restart: parent run was interrupted.' "
            "WHERE RUN_ID = %(run_id)s AND STATUS = 'running'",
            {"run_id": run_id},
        )
        logger.info(f"[storage] Recovered orphaned run {run_id}")
    return len(rows)


def list_agent_runs_by_batch(batch_id: str) -> list[SimpleNamespace]:
    rows = sf_session.query(
        "SELECT * FROM AGENT_RUNS WHERE BATCH_ID = %(batch_id)s ORDER BY BATCH_INDEX ASC",
        {"batch_id": batch_id},
    )
    return [_agent_run_from_row(r, tasks=list_agent_tasks(r["ID"])) for r in rows]


def get_agent_run_by_scan(scan_id: str) -> Optional[SimpleNamespace]:
    """The most recent agent run for a given scan, or None. Used to re-check a
    run's completion when its findings are resolved (see findings PATCH)."""
    if not scan_id:
        return None
    rows = sf_session.query(
        "SELECT * FROM AGENT_RUNS WHERE SCAN_ID = %(scan_id)s ORDER BY CREATED_AT DESC LIMIT 1",
        {"scan_id": scan_id},
    )
    return _agent_run_from_row(rows[0], tasks=list_agent_tasks(rows[0]["ID"])) if rows else None


def count_open_findings_for_scan(scan_id: str) -> int:
    """Number of still-open (detected/validated/in_progress) findings for a scan.
    Used to decide whether resolving a finding has cleared a run's whole queue."""
    if not scan_id:
        return 0
    in_open = ", ".join(f"'{s}'" for s in _OPEN_FINDING_STATUSES)
    rows = sf_session.query(
        f"SELECT COUNT(*) AS CNT FROM FINDINGS WHERE SCAN_ID = %(scan_id)s AND STATUS IN ({in_open})",
        {"scan_id": scan_id},
    )
    return rows[0]["CNT"] if rows else 0


def get_next_pending_batch_run(batch_id: str) -> Optional[SimpleNamespace]:
    rows = sf_session.query(
        """
        SELECT * FROM AGENT_RUNS
        WHERE BATCH_ID = %(batch_id)s AND STATUS = 'pending'
        ORDER BY BATCH_INDEX ASC
        LIMIT 1
        """,
        {"batch_id": batch_id},
    )
    return _agent_run_from_row(rows[0], tasks=[]) if rows else None


def update_agent_run(run_id: str, **fields: Any) -> SimpleNamespace:
    """Partial update. Supports: status, scan_id, started_at, completed_at,
    findings_count, ai_rules_count, instance_review_state, error_message."""
    json_cols = {"instance_review_state"}
    sets, params = [], {"id": run_id}
    for key, value in fields.items():
        col = key.upper()
        if key in json_cols:
            sets.append(f"{col} = PARSE_JSON(%({key})s)")
            params[key] = _json_or_null(value)
        else:
            sets.append(f"{col} = %({key})s")
            params[key] = value
    if sets:
        sf_session.execute(f"UPDATE AGENT_RUNS SET {', '.join(sets)} WHERE ID = %(id)s", params)
    return get_agent_run(run_id)


def list_agent_tasks(run_id: str) -> list[SimpleNamespace]:
    rows = sf_session.query(
        "SELECT * FROM AGENT_TASKS WHERE RUN_ID = %(run_id)s ORDER BY CREATED_AT ASC",
        {"run_id": run_id},
    )
    return [_agent_task_from_row(r) for r in rows]


def get_agent_task(run_id: str, agent_name: str) -> Optional[SimpleNamespace]:
    rows = sf_session.query(
        "SELECT * FROM AGENT_TASKS WHERE RUN_ID = %(run_id)s AND AGENT_NAME = %(agent_name)s",
        {"run_id": run_id, "agent_name": agent_name},
    )
    return _agent_task_from_row(rows[0]) if rows else None


def get_agent_task_by_id(task_id: str) -> Optional[SimpleNamespace]:
    rows = sf_session.query("SELECT * FROM AGENT_TASKS WHERE ID = %(id)s", {"id": task_id})
    return _agent_task_from_row(rows[0]) if rows else None


def update_agent_task(task_id: str, **fields: Any) -> SimpleNamespace:
    """Partial update. Supports: status, started_at, completed_at, output, error_message."""
    json_cols = {"output"}
    sets, params = [], {"id": task_id}
    for key, value in fields.items():
        col = key.upper()
        if key in json_cols:
            sets.append(f"{col} = PARSE_JSON(%({key})s)")
            params[key] = _json_or_null(value)
        else:
            sets.append(f"{col} = %({key})s")
            params[key] = value
    if sets:
        sf_session.execute(f"UPDATE AGENT_TASKS SET {', '.join(sets)} WHERE ID = %(id)s", params)
    return get_agent_task_by_id(task_id)


# ═══════════════════════════════════════════════════════════════════════════
# RECOMMENDATION_CACHE
# ═══════════════════════════════════════════════════════════════════════════

def _cache_entry_from_row(row: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id=row["ID"],
        cache_key=row["CACHE_KEY"],
        rule_code=row["RULE_CODE"],
        data_type=row["DATA_TYPE"],
        explanation_template=row["EXPLANATION_TEMPLATE"],
        sql_template=row["SQL_TEMPLATE"],
        confidence=row["CONFIDENCE"],
        impact=row["IMPACT"],
        hit_count=row["HIT_COUNT"],
        created_at=row["CREATED_AT"],
        updated_at=row["UPDATED_AT"],
    )


def get_cache_entry(cache_key: str) -> Optional[SimpleNamespace]:
    rows = sf_session.query(
        "SELECT * FROM RECOMMENDATION_CACHE WHERE CACHE_KEY = %(cache_key)s",
        {"cache_key": cache_key},
    )
    return _cache_entry_from_row(rows[0]) if rows else None


def create_cache_entry(
    cache_key: str,
    rule_code: str,
    data_type: str,
    explanation_template: str,
    sql_template: str,
    confidence: int,
    impact: str,
) -> SimpleNamespace:
    entry_id = _new_id()
    sf_session.execute(
        """
        INSERT INTO RECOMMENDATION_CACHE
            (ID, CACHE_KEY, RULE_CODE, DATA_TYPE, EXPLANATION_TEMPLATE, SQL_TEMPLATE,
             CONFIDENCE, IMPACT, HIT_COUNT)
        VALUES
            (%(id)s, %(cache_key)s, %(rule_code)s, %(data_type)s, %(explanation_template)s,
             %(sql_template)s, %(confidence)s, %(impact)s, 0)
        """,
        {
            "id": entry_id,
            "cache_key": cache_key,
            "rule_code": rule_code,
            "data_type": data_type or "",
            "explanation_template": explanation_template,
            "sql_template": sql_template,
            "confidence": confidence,
            "impact": impact,
        },
    )
    return get_cache_entry(cache_key)


def increment_cache_hit(cache_key: str) -> None:
    sf_session.execute(
        """
        UPDATE RECOMMENDATION_CACHE
        SET HIT_COUNT = HIT_COUNT + 1, UPDATED_AT = CURRENT_TIMESTAMP()
        WHERE CACHE_KEY = %(cache_key)s
        """,
        {"cache_key": cache_key},
    )


# ─────────────────────────────────────────────────────────────────────────
# CONNECTIONS  (ported from old app/models/connection.py)
# EXTRA is VARIANT. The "SCHEMA" column is reserved so it is quoted; it maps
# to the .schema_ attribute (old ORM used Column("schema") -> attr schema_).
# TYPE is stored as the lowercase enum value ("snowflake" / "postgres").
# ─────────────────────────────────────────────────────────────────────────

def _connection_from_row(row: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id=row["ID"],
        name=row["NAME"],
        type=row["TYPE"],
        host=row.get("HOST"),
        port=row.get("PORT"),
        database=row.get("DATABASE"),
        schema_=row.get("SCHEMA"),
        username=row.get("USERNAME"),
        secret=row.get("SECRET"),
        auth_method=row.get("AUTH_METHOD"),
        extra=_parse_json(row.get("EXTRA")) or {},
        is_active=row.get("IS_ACTIVE"),
        created_at=row["CREATED_AT"],
        updated_at=row["UPDATED_AT"],
    )


def get_connection_record(connection_id: str) -> Optional[SimpleNamespace]:
    rows = sf_session.query("SELECT * FROM CONNECTIONS WHERE ID = %(id)s", {"id": connection_id})
    return _connection_from_row(rows[0]) if rows else None


def list_connections() -> list[SimpleNamespace]:
    rows = sf_session.query("SELECT * FROM CONNECTIONS ORDER BY CREATED_AT ASC")
    return [_connection_from_row(r) for r in rows]


def get_first_connection(prefer_type: Optional[str] = None) -> Optional[SimpleNamespace]:
    """First connection, optionally preferring a type (used as a fallback when
    no connection_id is supplied — mirrors the old registry fallback)."""
    if prefer_type:
        rows = sf_session.query(
            "SELECT * FROM CONNECTIONS WHERE TYPE = %(t)s ORDER BY CREATED_AT ASC LIMIT 1",
            {"t": prefer_type},
        )
        if rows:
            return _connection_from_row(rows[0])
    rows = sf_session.query("SELECT * FROM CONNECTIONS ORDER BY CREATED_AT ASC LIMIT 1")
    return _connection_from_row(rows[0]) if rows else None


def create_connection(
    name: str,
    type: str,
    host: Optional[str] = None,
    port: Optional[int] = None,
    database: Optional[str] = None,
    schema_: Optional[str] = None,
    username: Optional[str] = None,
    secret: Optional[str] = None,
    auth_method: Optional[str] = None,
    extra: Optional[dict] = None,
    is_active: bool = True,
) -> SimpleNamespace:
    connection_id = _new_id()
    sf_session.execute(
        """
        INSERT INTO CONNECTIONS
            (ID, NAME, TYPE, HOST, PORT, DATABASE, "SCHEMA", USERNAME, SECRET,
             AUTH_METHOD, EXTRA, IS_ACTIVE)
        SELECT
            %(id)s, %(name)s, %(type)s, %(host)s, %(port)s, %(database)s,
            %(schema_)s, %(username)s, %(secret)s, %(auth_method)s,
            PARSE_JSON(%(extra)s), %(is_active)s
        """,
        {
            "id": connection_id,
            "name": name,
            "type": type,
            "host": host,
            "port": port,
            "database": database,
            "schema_": schema_,
            "username": username,
            "secret": secret,
            "auth_method": auth_method,
            "extra": _json_or_null(extra),
            "is_active": is_active,
        },
    )
    return get_connection_record(connection_id)


def update_connection(connection_id: str, **fields: Any) -> Optional[SimpleNamespace]:
    """Partial update. Accepts schema_ (-> "SCHEMA" column) and extra (VARIANT)."""
    col_map = {"schema_": '"SCHEMA"'}
    json_cols = {"extra"}
    sets, params = [], {"id": connection_id}
    for key, value in fields.items():
        col = col_map.get(key, key.upper())
        if key in json_cols:
            sets.append(f"{col} = PARSE_JSON(%({key})s)")
            params[key] = _json_or_null(value)
        else:
            sets.append(f"{col} = %({key})s")
            params[key] = value
    if "updated_at" not in fields:
        sets.append("UPDATED_AT = CURRENT_TIMESTAMP()")
    if sets:
        sf_session.execute(
            f"UPDATE CONNECTIONS SET {', '.join(sets)} WHERE ID = %(id)s", params
        )
    return get_connection_record(connection_id)


def delete_connection(connection_id: str) -> None:
    sf_session.execute("DELETE FROM CONNECTIONS WHERE ID = %(id)s", {"id": connection_id})


# ═══════════════════════════════════════════════════════════════════════════
# RULE_CHATS  — persistent conversation sessions for the AI rule chat panel
# ═══════════════════════════════════════════════════════════════════════════

def _chat_from_row(row: dict) -> SimpleNamespace:
    raw = row.get("MESSAGES")
    if isinstance(raw, str):
        try:
            import json as _json
            messages = _json.loads(raw)
        except Exception:
            messages = []
    elif isinstance(raw, list):
        messages = raw
    else:
        messages = []
    return SimpleNamespace(
        id=row["ID"],
        title=row.get("TITLE"),
        messages=messages,
        created_by=row.get("CREATED_BY"),
        created_at=row.get("CREATED_AT"),
        updated_at=row.get("UPDATED_AT"),
    )


def create_rule_chat(title: Optional[str], messages: list, created_by: Optional[str] = None) -> SimpleNamespace:
    import json as _json
    chat_id = _new_id()
    sf_session.execute(
        """
        INSERT INTO RULE_CHATS (ID, TITLE, MESSAGES, CREATED_BY)
        SELECT %(id)s, %(title)s, PARSE_JSON(%(messages)s), %(created_by)s
        """,
        {"id": chat_id, "title": title, "messages": _json.dumps(messages), "created_by": created_by},
    )
    rows = sf_session.query("SELECT * FROM RULE_CHATS WHERE ID = %(id)s", {"id": chat_id})
    return _chat_from_row(rows[0])


def get_rule_chat(chat_id: str) -> Optional[SimpleNamespace]:
    rows = sf_session.query("SELECT * FROM RULE_CHATS WHERE ID = %(id)s", {"id": chat_id})
    return _chat_from_row(rows[0]) if rows else None


def list_rule_chats(created_by: Optional[str] = None, limit: int = 50) -> list:
    if created_by:
        rows = sf_session.query(
            "SELECT * FROM RULE_CHATS WHERE UPPER(CREATED_BY) = UPPER(%(u)s) ORDER BY UPDATED_AT DESC LIMIT %(lim)s",
            {"u": created_by, "lim": limit},
        )
    else:
        rows = sf_session.query(
            "SELECT * FROM RULE_CHATS ORDER BY UPDATED_AT DESC LIMIT %(lim)s",
            {"lim": limit},
        )
    return [_chat_from_row(r) for r in rows]


def update_rule_chat(chat_id: str, messages: list, title: Optional[str] = None) -> Optional[SimpleNamespace]:
    import json as _json
    sets = ["MESSAGES = PARSE_JSON(%(messages)s)", "UPDATED_AT = CURRENT_TIMESTAMP()"]
    params: dict = {"id": chat_id, "messages": _json.dumps(messages)}
    if title is not None:
        sets.append("TITLE = %(title)s")
        params["title"] = title
    sf_session.execute(f"UPDATE RULE_CHATS SET {', '.join(sets)} WHERE ID = %(id)s", params)
    return get_rule_chat(chat_id)


def delete_rule_chat(chat_id: str) -> None:
    sf_session.execute("DELETE FROM RULE_CHATS WHERE ID = %(id)s", {"id": chat_id})


def get_findings_count_per_definition() -> dict:
    rows = sf_session.query(
        """
        SELECT ri.DEFINITION_ID, COUNT(f.ID) AS CNT
        FROM RULE_INSTANCES ri
        JOIN FINDINGS f ON f.INSTANCE_ID = ri.ID
        WHERE f.STATUS <> 'superseded'
        GROUP BY ri.DEFINITION_ID
        """
    )
    return {r["DEFINITION_ID"]: r["CNT"] for r in rows}


def get_top_assets_per_definition(top_n: int = 3) -> dict:
    """Returns {definition_id: [(asset_fqn, count), ...]} for the top-N assets per rule."""
    rows = sf_session.query(
        """
        SELECT ri.DEFINITION_ID, a.FQN, COUNT(f.ID) AS CNT
        FROM RULE_INSTANCES ri
        JOIN FINDINGS f ON f.INSTANCE_ID = ri.ID
        JOIN ASSETS a   ON a.ID = f.ASSET_ID
        WHERE f.STATUS <> 'superseded'
        GROUP BY ri.DEFINITION_ID, a.FQN
        ORDER BY ri.DEFINITION_ID, CNT DESC
        """
    )
    result: dict = {}
    for r in rows:
        def_id = r["DEFINITION_ID"]
        if def_id not in result:
            result[def_id] = []
        if len(result[def_id]) < top_n:
            result[def_id].append((r["FQN"], r["CNT"]))
    return result


# ─────────────────────────────────────────────────────────────────────────
# APP_SETTINGS  (ported from old app/models/app_setting.py)
# "KEY"/"VALUE" are reserved words -> quoted. VALUE is VARIANT (holds a
# JSON-encoded scalar: int/float/bool/str).
# ─────────────────────────────────────────────────────────────────────────

def get_all_settings() -> dict[str, Any]:
    """Returns {key: value} for every stored setting (values already parsed)."""
    rows = sf_session.query('SELECT "KEY" AS K, "VALUE" AS V FROM APP_SETTINGS')
    return {r["K"]: _parse_json(r.get("V")) for r in rows}


def get_setting(key: str) -> Any:
    rows = sf_session.query(
        'SELECT "VALUE" AS V FROM APP_SETTINGS WHERE "KEY" = %(k)s', {"k": key}
    )
    return _parse_json(rows[0].get("V")) if rows else None


def upsert_setting(key: str, value: Any) -> None:
    """Insert or update one setting. VALUE is VARIANT so uses PARSE_JSON via a
    MERGE (INSERT ... SELECT for the VARIANT bind, matching the storage
    convention of never binding raw JSON into VALUES)."""
    sf_session.execute(
        """
        MERGE INTO APP_SETTINGS t
        USING (SELECT %(k)s AS K, PARSE_JSON(%(v)s) AS V) s
        ON t."KEY" = s.K
        WHEN MATCHED THEN UPDATE SET t."VALUE" = s.V, t.UPDATED_AT = CURRENT_TIMESTAMP()
        WHEN NOT MATCHED THEN INSERT ("KEY", "VALUE") VALUES (s.K, s.V)
        """,
        {"k": key, "v": _json_or_null(value)},
    )


# ═══════════════════════════════════════════════════════════════════════════
# WORKFLOW TEMPLATES
# ═══════════════════════════════════════════════════════════════════════════

def _workflow_from_row(row: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id=row["ID"],
        label=row["LABEL"],
        description=row.get("DESCRIPTION"),
        rule_patterns=_parse_json(row.get("RULE_PATTERNS")) or [],
        created_by=row.get("CREATED_BY"),
        created_at=row["CREATED_AT"],
        updated_at=row["UPDATED_AT"],
        # Origin — where this workflow was created (null for pre-migration rows).
        origin_scope=row.get("ORIGIN_SCOPE"),
        origin_database=row.get("ORIGIN_DATABASE"),
        origin_schema=row.get("ORIGIN_SCHEMA"),
        origin_table=row.get("ORIGIN_TABLE"),
    )


def create_workflow(
    label: str,
    rule_patterns: list,
    description: str = "",
    created_by: str = "",
    origin_scope: Optional[str] = None,
    origin_database: Optional[str] = None,
    origin_schema: Optional[str] = None,
    origin_table: Optional[str] = None,
) -> SimpleNamespace:
    wf_id = _new_id()
    sf_session.execute(
        """
        INSERT INTO WORKFLOW_TEMPLATES
            (ID, LABEL, DESCRIPTION, RULE_PATTERNS, CREATED_BY,
             ORIGIN_SCOPE, ORIGIN_DATABASE, ORIGIN_SCHEMA, ORIGIN_TABLE)
        SELECT %(id)s, %(label)s, %(description)s, PARSE_JSON(%(patterns)s), %(created_by)s,
               %(origin_scope)s, %(origin_database)s, %(origin_schema)s, %(origin_table)s
        """,
        {
            "id": wf_id,
            "label": label,
            "description": description,
            "patterns": _json_or_null(rule_patterns),
            "created_by": created_by,
            "origin_scope": origin_scope,
            "origin_database": origin_database,
            "origin_schema": origin_schema,
            "origin_table": origin_table,
        },
    )
    return get_workflow(wf_id)


def get_workflow(workflow_id: str) -> Optional[SimpleNamespace]:
    rows = sf_session.query(
        "SELECT * FROM WORKFLOW_TEMPLATES WHERE ID = %(id)s", {"id": workflow_id}
    )
    return _workflow_from_row(rows[0]) if rows else None


def list_workflows() -> list[SimpleNamespace]:
    rows = sf_session.query(
        "SELECT * FROM WORKFLOW_TEMPLATES ORDER BY CREATED_AT DESC"
    )
    return [_workflow_from_row(r) for r in rows]


def update_workflow(
    workflow_id: str,
    label: str = None,
    description: str = None,
    rule_patterns: list = None,
) -> SimpleNamespace:
    fields = []
    params: dict = {"id": workflow_id}
    if label is not None:
        fields.append("LABEL = %(label)s")
        params["label"] = label
    if description is not None:
        fields.append("DESCRIPTION = %(description)s")
        params["description"] = description
    if rule_patterns is not None:
        fields.append("RULE_PATTERNS = PARSE_JSON(%(patterns)s)")
        params["patterns"] = _json_or_null(rule_patterns)
    fields.append("UPDATED_AT = CURRENT_TIMESTAMP()")
    sf_session.execute(
        f"UPDATE WORKFLOW_TEMPLATES SET {', '.join(fields)} WHERE ID = %(id)s",
        params,
    )
    return get_workflow(workflow_id)


def delete_workflow(workflow_id: str) -> None:
    sf_session.execute(
        "DELETE FROM WORKFLOW_TEMPLATES WHERE ID = %(id)s", {"id": workflow_id}
    )


# ═══════════════════════════════════════════════════════════════════════════
# SCHEDULES
# ═══════════════════════════════════════════════════════════════════════════

# Columns a caller may set on create/update. Kept explicit so an accidental
# field name never turns into a malformed UPDATE.
_SCHEDULE_COLS = {
    "name", "enabled", "connection_id", "scope", "database_name", "schema_name",
    "table_name", "workflow_template_id", "cadence", "time_of_day", "day_of_week",
    "day_of_month", "month_of_year", "interval_value", "interval_unit",
    "next_run_at", "last_run_at", "last_batch_id", "last_status", "last_error",
    "created_by",
}


def _schedule_from_row(row: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id=row["ID"],
        name=row["NAME"],
        enabled=bool(row["ENABLED"]),
        connection_id=row.get("CONNECTION_ID"),
        scope=row["SCOPE"],
        database_name=row.get("DATABASE_NAME"),
        schema_name=row.get("SCHEMA_NAME"),
        table_name=row.get("TABLE_NAME"),
        workflow_template_id=row.get("WORKFLOW_TEMPLATE_ID"),
        cadence=row["CADENCE"],
        time_of_day=row.get("TIME_OF_DAY"),
        day_of_week=row.get("DAY_OF_WEEK"),
        day_of_month=row.get("DAY_OF_MONTH"),
        month_of_year=row.get("MONTH_OF_YEAR"),
        interval_value=row.get("INTERVAL_VALUE"),
        interval_unit=row.get("INTERVAL_UNIT"),
        next_run_at=row.get("NEXT_RUN_AT"),
        last_run_at=row.get("LAST_RUN_AT"),
        last_batch_id=row.get("LAST_BATCH_ID"),
        last_status=row.get("LAST_STATUS"),
        last_error=row.get("LAST_ERROR"),
        created_at=row.get("CREATED_AT"),
        created_by=row.get("CREATED_BY"),
    )


def create_schedule(**fields: Any) -> SimpleNamespace:
    schedule_id = fields.pop("id", None) or _new_id()
    cols = {k: v for k, v in fields.items() if k in _SCHEDULE_COLS}
    cols["id"] = schedule_id
    col_names = ", ".join(k.upper() for k in cols)
    placeholders = ", ".join(f"%({k})s" for k in cols)
    sf_session.execute(
        f"INSERT INTO SCHEDULES ({col_names}) VALUES ({placeholders})", cols
    )
    return get_schedule(schedule_id)


def get_schedule(schedule_id: str) -> Optional[SimpleNamespace]:
    rows = sf_session.query("SELECT * FROM SCHEDULES WHERE ID = %(id)s", {"id": schedule_id})
    return _schedule_from_row(rows[0]) if rows else None


def list_schedules(enabled_only: bool = False) -> list[SimpleNamespace]:
    where = "WHERE ENABLED = TRUE" if enabled_only else ""
    rows = sf_session.query(f"SELECT * FROM SCHEDULES {where} ORDER BY CREATED_AT DESC")
    return [_schedule_from_row(r) for r in rows]


def update_schedule(schedule_id: str, **fields: Any) -> Optional[SimpleNamespace]:
    """Partial update over the whitelisted schedule columns."""
    sets, params = [], {"id": schedule_id}
    for key, value in fields.items():
        if key not in _SCHEDULE_COLS:
            continue
        sets.append(f"{key.upper()} = %({key})s")
        params[key] = value
    if sets:
        sf_session.execute(
            f"UPDATE SCHEDULES SET {', '.join(sets)} WHERE ID = %(id)s", params
        )
    return get_schedule(schedule_id)


def delete_schedule(schedule_id: str) -> None:
    sf_session.execute("DELETE FROM SCHEDULES WHERE ID = %(id)s", {"id": schedule_id})


def list_due_schedules(now: Any) -> list[SimpleNamespace]:
    """Enabled schedules whose NEXT_RUN_AT has arrived (<= now)."""
    rows = sf_session.query(
        """
        SELECT * FROM SCHEDULES
        WHERE ENABLED = TRUE
          AND NEXT_RUN_AT IS NOT NULL
          AND NEXT_RUN_AT <= %(now)s
        ORDER BY NEXT_RUN_AT ASC
        """,
        {"now": now},
    )
    return [_schedule_from_row(r) for r in rows]


def claim_schedule(schedule_id: str, expected_next_run_at: Any, new_next_run_at: Any) -> bool:
    """
    Atomically advance NEXT_RUN_AT from its expected value to the newly-computed
    one. Returns True only for the caller that won the race — the guard against a
    schedule firing twice (multiple workers / overlapping ticks). expected_next_
    run_at is what the tick read; if another caller already advanced it, the
    conditional UPDATE matches zero rows and this returns False.
    """
    rowcount = sf_session.execute(
        """
        UPDATE SCHEDULES
        SET NEXT_RUN_AT = %(new_next)s, LAST_RUN_AT = CURRENT_TIMESTAMP()
        WHERE ID = %(id)s AND NEXT_RUN_AT = %(expected)s
        """,
        {"id": schedule_id, "new_next": new_next_run_at, "expected": expected_next_run_at},
    )
    return (rowcount or 0) > 0


# ═══════════════════════════════════════════════════════════════════════════
# RULE INTELLIGENCE LOGS
# ═══════════════════════════════════════════════════════════════════════════

def _intelligence_log_from_row(row: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id=row["ID"],
        run_id=row["RUN_ID"],
        table_fqn=row["TABLE_FQN"],
        table_type=row["TABLE_TYPE"],
        table_type_confidence=row["TABLE_TYPE_CONFIDENCE"],
        thinking=row["THINKING"],
        signals_used=_parse_json(row.get("SIGNALS_USED")),
        proposals_count=row["PROPOSALS_COUNT"] or 0,
        suppressed_count=row["SUPPRESSED_COUNT"] or 0,
        approved_count=row["APPROVED_COUNT"] or 0,
        rejected_count=row["REJECTED_COUNT"] or 0,
        model_used=row["MODEL_USED"],
        created_at=row["CREATED_AT"],
    )


def create_intelligence_log(
    run_id: str,
    table_fqn: str,
    table_type: str,
    table_type_confidence: int,
    thinking: str,
    signals_used: dict,
    proposals_count: int = 0,
    suppressed_count: int = 0,
    model_used: str = "us.anthropic.claude-opus-4-8",
) -> SimpleNamespace:
    log_id = _new_id()
    sf_session.execute(
        """
        INSERT INTO RULE_INTELLIGENCE_LOGS
            (ID, RUN_ID, TABLE_FQN, TABLE_TYPE, TABLE_TYPE_CONFIDENCE,
             THINKING, SIGNALS_USED, PROPOSALS_COUNT, SUPPRESSED_COUNT,
             APPROVED_COUNT, REJECTED_COUNT, MODEL_USED)
        SELECT
            %(id)s, %(run_id)s, %(table_fqn)s, %(table_type)s,
            %(table_type_confidence)s, %(thinking)s,
            PARSE_JSON(%(signals_used)s),
            %(proposals_count)s, %(suppressed_count)s, 0, 0, %(model_used)s
        """,
        {
            "id": log_id,
            "run_id": run_id,
            "table_fqn": table_fqn,
            "table_type": table_type,
            "table_type_confidence": table_type_confidence,
            "thinking": thinking,
            "signals_used": _json_or_null(signals_used),
            "proposals_count": proposals_count,
            "suppressed_count": suppressed_count,
            "model_used": model_used,
        },
    )
    # Return a minimal namespace with just the id — avoids INSERT→SELECT
    # timing issues where Snowflake hasn't made the row visible yet.
    result = get_intelligence_log(log_id)
    if result:
        return result
    ns = SimpleNamespace(
        id=log_id, run_id=run_id, table_fqn=table_fqn,
        table_type=table_type, table_type_confidence=table_type_confidence,
        thinking=thinking, proposals_count=proposals_count,
        suppressed_count=suppressed_count, approved_count=0, rejected_count=0,
        model_used=model_used, signals_used=signals_used,
    )
    return ns


def get_intelligence_log(log_id: str) -> Optional[SimpleNamespace]:
    rows = sf_session.query(
        "SELECT * FROM RULE_INTELLIGENCE_LOGS WHERE ID = %(id)s", {"id": log_id}
    )
    return _intelligence_log_from_row(rows[0]) if rows else None


def get_intelligence_log_for_run(run_id: str) -> Optional[SimpleNamespace]:
    rows = sf_session.query(
        "SELECT * FROM RULE_INTELLIGENCE_LOGS WHERE RUN_ID = %(run_id)s LIMIT 1",
        {"run_id": run_id},
    )
    return _intelligence_log_from_row(rows[0]) if rows else None


def log_critique_drop(
    run_id: str,
    table_fqn: str,
    proposal: dict,
    scores: dict,
    mean_score: float,
    drop_reason: str,
) -> None:
    """Persist a self-critique drop so future runs can see the shapes that
    keep getting cut and why. Best-effort — never blocks the pipeline."""
    try:
        proposal_name = (
            (proposal.get("new_definition") or {}).get("name")
            or proposal.get("definition_id")
            or ""
        )[:500]
        sf_session.execute(
            """
            INSERT INTO RULE_CRITIQUE_DROPS
                (ID, RUN_ID, TABLE_FQN, PROPOSAL_NAME, DEFINITION_ID,
                 COLUMN_NAME, TEMPLATE_SHAPE, EVIDENCE_SCORE, IMPACT_SCORE,
                 APPROVAL_SCORE, MEAN_SCORE, DROP_REASON, PROPOSAL_JSON)
            SELECT %(id)s, %(run_id)s, %(table_fqn)s, %(proposal_name)s,
                   %(definition_id)s, %(column_name)s, %(template_shape)s,
                   %(evidence)s, %(impact)s, %(approval)s, %(mean)s,
                   %(reason)s, PARSE_JSON(%(proposal_json)s)
            """,
            {
                "id": _new_id(),
                "run_id": run_id,
                "table_fqn": table_fqn,
                "proposal_name": proposal_name,
                "definition_id": (proposal.get("definition_id") or "")[:500],
                "column_name": (proposal.get("column_name") or "")[:500],
                "template_shape": (proposal.get("template_shape") or "")[:100],
                "evidence": scores.get("evidence"),
                "impact": scores.get("impact"),
                "approval": scores.get("approval"),
                "mean": mean_score,
                "reason": (drop_reason or "")[:2000],
                "proposal_json": _json_or_null(proposal),
            },
        )
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"[storage] log_critique_drop failed: {e}")


def get_intelligence_logs_for_table(table_fqn: str, limit: int = 3) -> list:
    """Most recent intelligence logs for THIS exact table — used by
    _format_past_context to inject same-table history into the prompt so
    Claude can see what it decided last time on the same target instead of
    silently re-deriving everything."""
    try:
        rows = sf_session.query(
            """
            SELECT * FROM RULE_INTELLIGENCE_LOGS
            WHERE TABLE_FQN = %(fqn)s
            ORDER BY CREATED_AT DESC
            LIMIT %(limit)s
            """,
            {"fqn": table_fqn, "limit": limit},
        )
        return [_intelligence_log_from_row(r) for r in rows]
    except Exception as e:
        # A past-context read that dies silently was audit finding #6 — every
        # scan pretended "no history yet" whether that was true or a broken
        # query. Log the type + message so the log at least says "this
        # channel is broken", without breaking the caller.
        logger.warning(f"[storage] get_intelligence_logs_for_table failed: {type(e).__name__}: {e}")
        return []


def append_intelligence_log_lessons(
    run_id: str, table_fqn: str, lessons: list
) -> None:
    """Persist structured review lessons (approve/reject decisions with reasons)
    into a dedicated RULE_REVIEW_LESSONS table so future runs on similar tables
    can read them as grounded guidance rather than raw thinking blobs.

    Table DDL (run once in Snowflake):
        CREATE TABLE IF NOT EXISTS DQ_APP.RULE_REVIEW_LESSONS (
            ID          VARCHAR PRIMARY KEY,
            RUN_ID      VARCHAR,
            TABLE_FQN   VARCHAR,
            VERDICT     VARCHAR,        -- 'approved' | 'rejected'
            CHECK_CONCEPT VARCHAR,
            COLUMN_NAME VARCHAR,
            SEVERITY    VARCHAR,
            REASON      VARCHAR,
            CREATED_AT  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """
    try:
        for lesson in lessons:
            sf_session.execute(
                """
                INSERT INTO DQ_APP.RULE_REVIEW_LESSONS
                    (ID, RUN_ID, TABLE_FQN, VERDICT, CHECK_CONCEPT, COLUMN_NAME, SEVERITY, REASON)
                SELECT %(id)s, %(run_id)s, %(table_fqn)s, %(verdict)s,
                       %(check_concept)s, %(column_name)s, %(severity)s, %(reason)s
                """,
                {
                    "id": _new_id(),
                    "run_id": run_id,
                    "table_fqn": table_fqn,
                    "verdict": lesson.get("verdict", ""),
                    "check_concept": (lesson.get("check_concept") or "")[:200],
                    "column_name": (lesson.get("column") or "")[:200],
                    "severity": (lesson.get("severity") or "")[:50],
                    "reason": (lesson.get("reason") or "")[:1000],
                },
            )
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"[storage] append_intelligence_log_lessons failed: {e}")


def get_review_lessons_for_table(table_fqn: str, limit: int = 20) -> list:
    """Fetch recent review lessons for tables sharing the same bare table name.
    Used by RuleIntelligenceAgent._format_past_context to inject structured
    human feedback into the prompt rather than raw thinking blobs."""
    try:
        parts = table_fqn.upper().split(".")
        table_name = parts[-1] if parts else table_fqn.upper()
        rows = sf_session.query(
            """
            SELECT VERDICT, CHECK_CONCEPT, COLUMN_NAME, SEVERITY, REASON, CREATED_AT
            FROM DQ_APP.RULE_REVIEW_LESSONS
            WHERE UPPER(SPLIT_PART(TABLE_FQN, '.', 3)) = %(table_name)s
               OR TABLE_FQN = %(fqn)s
            ORDER BY CREATED_AT DESC
            LIMIT %(limit)s
            """,
            {"table_name": table_name, "fqn": table_fqn, "limit": limit},
        )
        return [
            {
                "verdict": r.get("VERDICT"),
                "check_concept": r.get("CHECK_CONCEPT"),
                "column": r.get("COLUMN_NAME"),
                "severity": r.get("SEVERITY"),
                "reason": r.get("REASON"),
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning(f"[storage] get_review_lessons_for_table failed: {type(e).__name__}: {e}")
        return []


def update_intelligence_log_outcomes(
    log_id: str, approved_count: int, rejected_count: int
) -> None:
    sf_session.execute(
        """
        UPDATE RULE_INTELLIGENCE_LOGS
        SET APPROVED_COUNT = %(approved)s,
            REJECTED_COUNT = %(rejected)s
        WHERE ID = %(id)s
        """,
        {"id": log_id, "approved": approved_count, "rejected": rejected_count},
    )


def search_similar_intelligence(table_fqn: str, limit: int = 5) -> list[SimpleNamespace]:
    """
    Find past intelligence logs for tables similar to table_fqn.

    Cortex Search (semantic/vector) is the preferred path — uncomment the block
    below and comment out the SQL fallback once CORTEX_USER is granted:

    # try:
    #     query = f"data quality analysis for table {table_fqn}"
    #     rows = sf_session.query(
    #         \"\"\"
    #         SELECT *
    #         FROM TABLE(
    #             SNOWFLAKE.CORTEX.SEARCH_PREVIEW(
    #                 'DQ_APP.RULE_INTELLIGENCE_SEARCH',
    #                 %(query)s,
    #                 %(limit)s
    #             )
    #         )
    #         \"\"\",
    #         {"query": query, "limit": limit},
    #     )
    #     return [_intelligence_log_from_row(r) for r in rows]
    # except Exception:
    #     return []

    SQL fallback (no CORTEX_USER grant needed). Three match tiers:
    1. Same table name in a different schema/db (same concept, different env)
    2. Table name shares a keyword (ORDER in ORDERS, ORDER_ITEMS, PURCHASE_ORDERS)
    3. Same table type classification with at least one past approval
    Results ranked: tier first, then approved_count, then most recent.
    """
    try:
        # Extract the bare table name from db.schema.table
        parts = table_fqn.upper().split(".")
        table_name = parts[-1] if parts else table_fqn.upper()

        # Keyword: longest word in the table name that is >3 chars
        # (strips common suffixes like _STG, _FACT, _DIM for better matching)
        words = [w for w in table_name.replace("_", " ").split() if len(w) > 3]
        keyword = words[0] if words else table_name

        rows = sf_session.query(
            """
            WITH scored AS (
                SELECT *,
                    CASE
                        -- Tier 1: same bare table name (different env/schema)
                        WHEN UPPER(SPLIT_PART(TABLE_FQN, '.', 3)) = %(table_name)s
                            THEN 3
                        -- Tier 2: table name contains the same keyword
                        WHEN UPPER(SPLIT_PART(TABLE_FQN, '.', 3)) ILIKE %(keyword_like)s
                            THEN 2
                        -- Tier 3: same table type with at least one approval
                        WHEN TABLE_TYPE = (
                            SELECT TABLE_TYPE FROM RULE_INTELLIGENCE_LOGS
                            WHERE TABLE_FQN = %(fqn)s
                            ORDER BY CREATED_AT DESC LIMIT 1
                        ) AND APPROVED_COUNT > 0
                            THEN 1
                        ELSE 0
                    END AS match_tier
                FROM RULE_INTELLIGENCE_LOGS
                WHERE TABLE_FQN != %(fqn)s
                  AND THINKING IS NOT NULL
                  AND LENGTH(THINKING) > 0
            )
            SELECT * FROM scored
            WHERE match_tier > 0
            ORDER BY match_tier DESC, APPROVED_COUNT DESC, CREATED_AT DESC
            LIMIT %(limit)s
            """,
            {
                "fqn": table_fqn,
                "table_name": table_name,
                "keyword_like": f"%{keyword}%",
                "limit": limit,
            },
        )
        return [_intelligence_log_from_row(r) for r in rows]
    except Exception as e:
        logger.warning(f"[storage] search_similar_intelligence failed: {type(e).__name__}: {e}")
        return []


# ═══════════════════════════════════════════════════════════════════════════
# FEEDBACK MEMOS  (synthesised cross-run patterns)
# ═══════════════════════════════════════════════════════════════════════════

def get_feedback_memo(bare_table_name: str, table_type: str) -> Optional[dict]:
    """Return the synthesised feedback memo for this (bare_table_name, table_type)
    pair, or None if no memo exists yet."""
    try:
        rows = sf_session.query(
            """
            SELECT MEMO, LESSON_COUNT, UPDATED_AT
            FROM DQ_APP.RULE_FEEDBACK_MEMOS
            WHERE BARE_TABLE_NAME = %(name)s
              AND TABLE_TYPE      = %(type)s
            LIMIT 1
            """,
            {"name": bare_table_name.upper(), "type": table_type.lower()},
        )
        if not rows:
            return None
        memo = _parse_json(rows[0].get("MEMO"))
        if not isinstance(memo, dict):
            return None
        memo["_lesson_count"] = rows[0].get("LESSON_COUNT", 0)
        memo["_updated_at"] = str(rows[0].get("UPDATED_AT", ""))
        return memo
    except Exception as e:
        logger.warning(f"[storage] get_feedback_memo failed: {type(e).__name__}: {e}")
        return None


def upsert_feedback_memo(
    bare_table_name: str,
    table_type: str,
    memo: dict,
    lesson_count: int,
) -> None:
    """Insert or replace the feedback memo for this (bare_table_name, table_type)."""
    existing = sf_session.query(
        """
        SELECT ID FROM DQ_APP.RULE_FEEDBACK_MEMOS
        WHERE BARE_TABLE_NAME = %(name)s AND TABLE_TYPE = %(type)s
        LIMIT 1
        """,
        {"name": bare_table_name.upper(), "type": table_type.lower()},
    )
    if existing:
        sf_session.execute(
            """
            UPDATE DQ_APP.RULE_FEEDBACK_MEMOS
            SET MEMO         = PARSE_JSON(%(memo)s),
                LESSON_COUNT = %(count)s,
                UPDATED_AT   = CURRENT_TIMESTAMP()
            WHERE BARE_TABLE_NAME = %(name)s
              AND TABLE_TYPE      = %(type)s
            """,
            {
                "memo": json.dumps(memo, default=_json_default),
                "count": lesson_count,
                "name": bare_table_name.upper(),
                "type": table_type.lower(),
            },
        )
    else:
        sf_session.execute(
            """
            INSERT INTO DQ_APP.RULE_FEEDBACK_MEMOS
                (ID, BARE_TABLE_NAME, TABLE_TYPE, MEMO, LESSON_COUNT)
            SELECT %(id)s, %(name)s, %(type)s, PARSE_JSON(%(memo)s), %(count)s
            """,
            {
                "id": _new_id(),
                "name": bare_table_name.upper(),
                "type": table_type.lower(),
                "memo": json.dumps(memo, default=_json_default),
                "count": lesson_count,
            },
        )


def get_lessons_for_synthesis(
    table_fqn: str, table_type: str, limit: int = 40
) -> list:
    """Fetch lessons for synthesis: all lessons for this bare table name PLUS
    lessons for any table of the same type that have 5+ data points.
    Used by FeedbackSynthesisAgent to build a representative training set."""
    try:
        parts = table_fqn.upper().split(".")
        bare = parts[-1] if parts else table_fqn.upper()
        rows = sf_session.query(
            """
            SELECT VERDICT, CHECK_CONCEPT, COLUMN_NAME, SEVERITY, REASON
            FROM DQ_APP.RULE_REVIEW_LESSONS
            WHERE UPPER(SPLIT_PART(TABLE_FQN, '.', 3)) = %(bare)s
            ORDER BY CREATED_AT DESC
            LIMIT %(limit)s
            """,
            {"bare": bare, "limit": limit},
        )
        return [
            {
                "verdict": r.get("VERDICT"),
                "check_concept": r.get("CHECK_CONCEPT") or "",
                "column": r.get("COLUMN_NAME"),
                "severity": r.get("SEVERITY"),
                "reason": r.get("REASON") or "",
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning(f"[storage] get_lessons_for_synthesis failed: {type(e).__name__}: {e}")
        return []


# ═══════════════════════════════════════════════════════════════════════════
# Incident-lifecycle helpers — one FINDINGS row per (INSTANCE_ID, ASSET_ID),
# updated across scans. See services/scan_finalizer.py for the state machine.
# ═══════════════════════════════════════════════════════════════════════════

_LIFECYCLE_OPEN = ("detected", "validated", "in_progress", "assigned", "acknowledged")
_LIFECYCLE_RESOLVED = ("resolved", "false_positive", "wont_fix", "closed")
_FAIL_HISTORY_MAX = 50


def find_open_finding(instance_id: str, asset_id: str) -> Optional[SimpleNamespace]:
    """The currently-open finding for this (rule, asset), if any. Used by the
    scan finalizer to decide UPDATE vs CREATE / RESOLVE."""
    in_open = ", ".join(f"'{s}'" for s in _LIFECYCLE_OPEN)
    rows = sf_session.query(
        f"""
        SELECT * FROM FINDINGS
        WHERE INSTANCE_ID = %(iid)s AND ASSET_ID = %(aid)s
          AND STATUS IN ({in_open})
        ORDER BY LAST_SEEN_AT DESC NULLS LAST, DETECTED_AT DESC
        LIMIT 1
        """,
        {"iid": instance_id, "aid": asset_id},
    )
    return _finding_from_row(rows[0]) if rows else None


def find_open_findings(instance_ids: list[str], asset_id: str) -> Dict[str, SimpleNamespace]:
    """Batched version of find_open_finding — one query for all instance_ids
    against a single asset. Used by table-health aggregation to avoid N+1
    round-trips to Snowflake."""
    if not instance_ids:
        return {}
    in_open = ", ".join(f"'{s}'" for s in _LIFECYCLE_OPEN)
    placeholders = ", ".join(f"%(iid{n})s" for n in range(len(instance_ids)))
    params = {f"iid{n}": iid for n, iid in enumerate(instance_ids)}
    params["aid"] = asset_id
    rows = sf_session.query(
        f"""
        SELECT * FROM FINDINGS
        WHERE INSTANCE_ID IN ({placeholders}) AND ASSET_ID = %(aid)s
          AND STATUS IN ({in_open})
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY INSTANCE_ID
            ORDER BY LAST_SEEN_AT DESC NULLS LAST, DETECTED_AT DESC
        ) = 1
        """,
        params,
    )
    return {r["INSTANCE_ID"]: _finding_from_row(r) for r in rows}


def find_recently_resolved_finding(
    instance_id: str, asset_id: str, within_days: int = 7,
) -> Optional[SimpleNamespace]:
    """The most-recently-resolved finding for this (rule, asset), if closed
    within `within_days`. Used by the scan finalizer for REOPEN."""
    in_res = ", ".join(f"'{s}'" for s in _LIFECYCLE_RESOLVED)
    rows = sf_session.query(
        f"""
        SELECT * FROM FINDINGS
        WHERE INSTANCE_ID = %(iid)s AND ASSET_ID = %(aid)s
          AND STATUS IN ({in_res})
          AND COALESCE(RESOLVED_AT, CLOSED_AT, UPDATED_AT)
              >= DATEADD('day', -%(days)s, CURRENT_TIMESTAMP())
        ORDER BY COALESCE(RESOLVED_AT, CLOSED_AT, UPDATED_AT) DESC
        LIMIT 1
        """,
        {"iid": instance_id, "aid": asset_id, "days": int(within_days)},
    )
    return _finding_from_row(rows[0]) if rows else None


def _bump_fail_history(existing_history, entry: dict) -> list:
    hist = list(existing_history or [])
    hist.append(entry)
    if len(hist) > _FAIL_HISTORY_MAX:
        hist = hist[-_FAIL_HISTORY_MAX:]
    return hist


def apply_finding_update(
    finding_id: str, scan_id: str,
    fail_count: int, total_count: int,
    severity: Optional[str] = None, evidence: Optional[dict] = None,
) -> SimpleNamespace:
    """UPDATE branch: this finding is still failing this scan. Bump
    last_seen_at + counts, append to fail_history. first_detected_at is
    NEVER touched — preserves "broken since Tuesday"."""
    existing = get_finding(finding_id)
    if not existing:
        raise ValueError(f"Finding not found: {finding_id}")
    hist = _bump_fail_history(existing.fail_history, {
        "scan_id": scan_id,
        "at": datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z',
        "fail_count": int(fail_count),
        "total_count": int(total_count),
    })
    set_clauses = [
        "LAST_SEEN_AT = CURRENT_TIMESTAMP()",
        "LAST_SCAN_ID = %(last_scan_id)s",
        "CURRENT_FAIL_COUNT = %(fail_count)s",
        "CURRENT_TOTAL_COUNT = %(total_count)s",
        "FAIL_HISTORY = PARSE_JSON(%(fail_history)s)",
        "UPDATED_AT = CURRENT_TIMESTAMP()",
    ]
    params = {
        "id": finding_id, "last_scan_id": scan_id,
        "fail_count": int(fail_count), "total_count": int(total_count),
        "fail_history": json.dumps(hist, default=_json_default),
    }
    if severity:
        set_clauses.append("SEVERITY = %(severity)s"); params["severity"] = severity
    if evidence is not None:
        set_clauses.append("EVIDENCE = PARSE_JSON(%(evidence)s)")
        params["evidence"] = _json_or_null(evidence)
    sf_session.execute(
        f"UPDATE FINDINGS SET {', '.join(set_clauses)} WHERE ID = %(id)s", params,
    )
    return get_finding(finding_id)


def auto_resolve_finding(finding_id: str, scan_id: str) -> SimpleNamespace:
    """RESOLVE branch: rule passed this scan but the finding was open — auto-close."""
    sf_session.execute(
        """
        UPDATE FINDINGS
        SET STATUS = 'resolved',
            RESOLVED_AT = CURRENT_TIMESTAMP(),
            RESOLUTION_NOTES = COALESCE(RESOLUTION_NOTES, 'Auto-resolved by rescan'),
            LAST_SCAN_ID = %(scan_id)s,
            UPDATED_AT = CURRENT_TIMESTAMP()
        WHERE ID = %(id)s
        """,
        {"id": finding_id, "scan_id": scan_id},
    )
    return get_finding(finding_id)


def reopen_finding(
    finding_id: str, scan_id: str,
    fail_count: int, total_count: int,
    severity: Optional[str] = None, evidence: Optional[dict] = None,
) -> SimpleNamespace:
    """REOPEN branch: recently-resolved finding failing again. Revive it
    (reopened_count++, status='detected') instead of creating a duplicate;
    first_detected_at stays put so the original break is preserved."""
    existing = get_finding(finding_id)
    if not existing:
        raise ValueError(f"Finding not found: {finding_id}")
    hist = _bump_fail_history(existing.fail_history, {
        "scan_id": scan_id,
        "at": datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z',
        "fail_count": int(fail_count),
        "total_count": int(total_count),
        "event": "reopened",
    })
    set_clauses = [
        "STATUS = 'detected'",
        "REOPENED_COUNT = COALESCE(REOPENED_COUNT, 0) + 1",
        "LAST_SEEN_AT = CURRENT_TIMESTAMP()",
        "LAST_SCAN_ID = %(scan_id)s",
        "CURRENT_FAIL_COUNT = %(fail_count)s",
        "CURRENT_TOTAL_COUNT = %(total_count)s",
        "FAIL_HISTORY = PARSE_JSON(%(fail_history)s)",
        "RESOLVED_AT = NULL", "CLOSED_AT = NULL", "RESOLUTION_NOTES = NULL",
        "UPDATED_AT = CURRENT_TIMESTAMP()",
    ]
    params = {
        "id": finding_id, "scan_id": scan_id,
        "fail_count": int(fail_count), "total_count": int(total_count),
        "fail_history": json.dumps(hist, default=_json_default),
    }
    if severity:
        set_clauses.append("SEVERITY = %(severity)s"); params["severity"] = severity
    if evidence is not None:
        set_clauses.append("EVIDENCE = PARSE_JSON(%(evidence)s)")
        params["evidence"] = _json_or_null(evidence)
    sf_session.execute(
        f"UPDATE FINDINGS SET {', '.join(set_clauses)} WHERE ID = %(id)s", params,
    )
    return get_finding(finding_id)


def create_finding_with_lifecycle(
    asset_id: str, scan_id: str, instance_id: str,
    title: str, description: str, severity: str,
    context: Optional[dict], evidence: Optional[dict],
    fail_count: int, total_count: int,
) -> SimpleNamespace:
    """CREATE branch: brand-new incident. Sets first_detected_at = now,
    initializes fail_history with the current run."""
    finding_id = _new_id()
    hist = [{
        "scan_id": scan_id,
        "at": datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z',
        "fail_count": int(fail_count),
        "total_count": int(total_count),
    }]
    sf_session.execute(
        """
        INSERT INTO FINDINGS
            (ID, ASSET_ID, SCAN_ID, INSTANCE_ID, TITLE, DESCRIPTION, STATUS, SEVERITY,
             CONTEXT, EVIDENCE,
             FIRST_DETECTED_AT, LAST_SEEN_AT, LAST_SCAN_ID,
             CURRENT_FAIL_COUNT, CURRENT_TOTAL_COUNT, FAIL_HISTORY, REOPENED_COUNT)
        SELECT
            %(id)s, %(asset_id)s, %(scan_id)s, %(instance_id)s, %(title)s, %(description)s,
            'detected', %(severity)s,
            PARSE_JSON(%(context)s), PARSE_JSON(%(evidence)s),
            CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP(), %(scan_id)s,
            %(fail_count)s, %(total_count)s, PARSE_JSON(%(fail_history)s), 0
        """,
        {
            "id": finding_id, "asset_id": asset_id, "scan_id": scan_id,
            "instance_id": instance_id, "title": title, "description": description,
            "severity": severity,
            "context": _json_or_null(context), "evidence": _json_or_null(evidence),
            "fail_count": int(fail_count), "total_count": int(total_count),
            "fail_history": json.dumps(hist, default=_json_default),
        },
    )
    return get_finding(finding_id)


# ═══════════════════════════════════════════════════════════════════════════
# MUTES — silence a specific (instance, asset) for a window. Scans still run
# and RULE_EXECUTIONS still logs; the lifecycle skips update/reopen/create on
# failure during a mute, so no new incident surfaces.
# ═══════════════════════════════════════════════════════════════════════════

def _mute_from_row(row: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id=row["ID"],
        instance_id=row["INSTANCE_ID"],
        asset_id=row["ASSET_ID"],
        muted_until=row["MUTED_UNTIL"],
        reason=row["REASON"],
        muted_by=row["MUTED_BY"],
        created_at=row["CREATED_AT"],
    )


def is_muted(instance_id: str, asset_id: str) -> bool:
    rows = sf_session.query(
        """
        SELECT ID FROM MUTES
        WHERE INSTANCE_ID = %(iid)s AND ASSET_ID = %(aid)s
          AND MUTED_UNTIL > CURRENT_TIMESTAMP()
        LIMIT 1
        """,
        {"iid": instance_id, "aid": asset_id},
    )
    return bool(rows)


def muted_instance_ids(instance_ids: list[str], asset_id: str) -> set[str]:
    """Batched version of is_muted — one query for all instance_ids against a
    single asset. Used by table-health aggregation to avoid N+1 round-trips."""
    if not instance_ids:
        return set()
    placeholders = ", ".join(f"%(iid{n})s" for n in range(len(instance_ids)))
    params = {f"iid{n}": iid for n, iid in enumerate(instance_ids)}
    params["aid"] = asset_id
    rows = sf_session.query(
        f"""
        SELECT DISTINCT INSTANCE_ID FROM MUTES
        WHERE INSTANCE_ID IN ({placeholders}) AND ASSET_ID = %(aid)s
          AND MUTED_UNTIL > CURRENT_TIMESTAMP()
        """,
        params,
    )
    return {r["INSTANCE_ID"] for r in rows}


def create_mute(
    instance_id: str, asset_id: str, muted_until: Any,
    reason: Optional[str] = None, muted_by: Optional[str] = None,
) -> SimpleNamespace:
    mute_id = _new_id()
    sf_session.execute(
        """
        INSERT INTO MUTES (ID, INSTANCE_ID, ASSET_ID, MUTED_UNTIL, REASON, MUTED_BY)
        VALUES (%(id)s, %(iid)s, %(aid)s, %(until)s, %(reason)s, %(by)s)
        """,
        {"id": mute_id, "iid": instance_id, "aid": asset_id,
         "until": muted_until, "reason": reason, "by": muted_by},
    )
    rows = sf_session.query("SELECT * FROM MUTES WHERE ID = %(id)s", {"id": mute_id})
    return _mute_from_row(rows[0])


def delete_mute(mute_id: str) -> None:
    sf_session.execute("DELETE FROM MUTES WHERE ID = %(id)s", {"id": mute_id})


def list_mutes(
    instance_id: Optional[str] = None, asset_id: Optional[str] = None,
    active_only: bool = True,
) -> list[SimpleNamespace]:
    where, params = [], {}
    if instance_id:
        where.append("INSTANCE_ID = %(iid)s"); params["iid"] = instance_id
    if asset_id:
        where.append("ASSET_ID = %(aid)s");    params["aid"] = asset_id
    if active_only:
        where.append("MUTED_UNTIL > CURRENT_TIMESTAMP()")
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    rows = sf_session.query(f"SELECT * FROM MUTES {where_sql} ORDER BY CREATED_AT DESC", params)
    return [_mute_from_row(r) for r in rows]


# ═══════════════════════════════════════════════════════════════════════════
# MAINTENANCE_PROPOSALS — MaintenanceAgent-generated suggestions to pause/
# retire stale or flapping rule instances. Reviewed in a queue UI, same
# shape as anomaly proposals.
# ═══════════════════════════════════════════════════════════════════════════

def _maintenance_proposal_from_row(row: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id=row["ID"],
        instance_id=row["INSTANCE_ID"],
        action=row["ACTION"],
        reason=row.get("REASON"),
        evidence=_parse_json(row.get("EVIDENCE")) or {},
        status=row["STATUS"],
        decision_reason=row.get("DECISION_REASON"),
        decided_by=row.get("DECIDED_BY"),
        decided_at=row.get("DECIDED_AT"),
        created_at=row["CREATED_AT"],
    )


def create_maintenance_proposal(
    instance_id: str, action: str, reason: str,
    evidence: Optional[dict] = None,
) -> SimpleNamespace:
    proposal_id = _new_id()
    sf_session.execute(
        """
        INSERT INTO MAINTENANCE_PROPOSALS
            (ID, INSTANCE_ID, ACTION, REASON, EVIDENCE, STATUS)
        SELECT %(id)s, %(iid)s, %(action)s, %(reason)s,
               PARSE_JSON(%(evidence)s), 'pending'
        """,
        {"id": proposal_id, "iid": instance_id, "action": action,
         "reason": (reason or "")[:2000],
         "evidence": _json_or_null(evidence or {})},
    )
    rows = sf_session.query(
        "SELECT * FROM MAINTENANCE_PROPOSALS WHERE ID = %(id)s",
        {"id": proposal_id},
    )
    return _maintenance_proposal_from_row(rows[0])


def get_maintenance_proposal(proposal_id: str) -> Optional[SimpleNamespace]:
    rows = sf_session.query(
        "SELECT * FROM MAINTENANCE_PROPOSALS WHERE ID = %(id)s",
        {"id": proposal_id},
    )
    return _maintenance_proposal_from_row(rows[0]) if rows else None


def list_maintenance_proposals(
    status: Optional[str] = "pending", limit: int = 500,
) -> list[SimpleNamespace]:
    where, params = [], {"limit": max(1, min(limit, 1000))}
    if status:
        where.append("STATUS = %(status)s"); params["status"] = status
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    rows = sf_session.query(
        f"""
        SELECT * FROM MAINTENANCE_PROPOSALS {where_sql}
        ORDER BY CREATED_AT DESC LIMIT %(limit)s
        """,
        params,
    )
    return [_maintenance_proposal_from_row(r) for r in rows]


def has_pending_maintenance_proposal(instance_id: str, action: str) -> bool:
    rows = sf_session.query(
        """
        SELECT ID FROM MAINTENANCE_PROPOSALS
        WHERE INSTANCE_ID = %(iid)s AND ACTION = %(action)s
          AND STATUS = 'pending'
        LIMIT 1
        """,
        {"iid": instance_id, "action": action},
    )
    return bool(rows)


def decide_maintenance_proposal(
    proposal_id: str, status: str,
    decided_by: Optional[str] = None, reason: Optional[str] = None,
) -> None:
    sf_session.execute(
        """
        UPDATE MAINTENANCE_PROPOSALS
        SET STATUS = %(status)s,
            DECIDED_BY = %(by)s,
            DECIDED_AT = CURRENT_TIMESTAMP(),
            DECISION_REASON = %(reason)s
        WHERE ID = %(id)s
        """,
        {"id": proposal_id, "status": status,
         "by": decided_by or "user",
         "reason": (reason or "")[:2000] if reason else None},
    )


# ═══════════════════════════════════════════════════════════════════════════
# Health scoring constants — shared between /table-health (per-table) and
# /lineage (batched, per-schema). Keep in one place so the two aggregations
# can never drift.
# ═══════════════════════════════════════════════════════════════════════════

SEVERITY_WEIGHT = {"critical": 5, "high": 3, "medium": 2, "low": 1, "info": 1}
HISTORY_LIMIT = 20  # last N executions per instance for pass-rate


# ═══════════════════════════════════════════════════════════════════════════
# LINEAGE — edge cache + refresh state + capability probe cache.
# Mirrors the RELATIONSHIP_CATALOG pattern (delete-scope + multi-row INSERT
# SELECT). Populated by app.services.lineage.refresh_database.
# ═══════════════════════════════════════════════════════════════════════════

def _edge_from_row(row: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id=row["ID"],
        connection_id=row["CONNECTION_ID"],
        source_database=row["SOURCE_DATABASE"],
        source_schema=row["SOURCE_SCHEMA"],
        source_table=row["SOURCE_TABLE"],
        source_fqn=row["SOURCE_FQN"],
        source_kind=row.get("SOURCE_KIND"),
        target_database=row["TARGET_DATABASE"],
        target_schema=row["TARGET_SCHEMA"],
        target_table=row["TARGET_TABLE"],
        target_fqn=row["TARGET_FQN"],
        target_kind=row.get("TARGET_KIND"),
        edge_type=row.get("EDGE_TYPE"),
        discovery_source=row.get("DISCOVERY_SOURCE"),
        evidence=_parse_json(row.get("EVIDENCE")),
        first_discovered_at=row.get("FIRST_DISCOVERED_AT"),
        last_seen_at=row.get("LAST_SEEN_AT"),
    )


def list_lineage_edges(
    connection_id: str,
    database_name: Optional[str] = None,
    schema_name: Optional[str] = None,
    table_name: Optional[str] = None,
) -> list[SimpleNamespace]:
    """List edges scoped to a connection. When a table is passed, returns
    edges where the table is EITHER source or target (single-table drill-down
    needs both directions). Otherwise applies AND-filtering on the source
    side only — schema/database filters mean 'edges originating in this
    scope', which is what the schema/DB-level graphs want."""
    where = ["CONNECTION_ID = %(cid)s"]
    params: Dict[str, Any] = {"cid": connection_id}

    if table_name:
        where.append(
            "((SOURCE_DATABASE = %(db)s AND SOURCE_SCHEMA = %(sc)s AND SOURCE_TABLE = %(tb)s)"
            " OR (TARGET_DATABASE = %(db)s AND TARGET_SCHEMA = %(sc)s AND TARGET_TABLE = %(tb)s))"
        )
        params["db"] = database_name
        params["sc"] = schema_name
        params["tb"] = table_name
    else:
        if database_name:
            where.append("(SOURCE_DATABASE = %(db)s OR TARGET_DATABASE = %(db)s)")
            params["db"] = database_name
        if schema_name:
            where.append("(SOURCE_SCHEMA = %(sc)s OR TARGET_SCHEMA = %(sc)s)")
            params["sc"] = schema_name

    rows = sf_session.query(
        f"SELECT * FROM LINEAGE_EDGES WHERE {' AND '.join(where)} ORDER BY SOURCE_FQN, TARGET_FQN",
        params,
    )
    return [_edge_from_row(r) for r in rows]


def replace_lineage_edges_for_database(
    connection_id: str,
    database_name: str,
    rows: list[dict],
) -> int:
    """Atomic replace: DELETE all edges for (connection, database) on either
    side, then one multi-row INSERT ... SELECT ... UNION ALL. Mirrors
    replace_relationships. Each row dict may carry: source_database,
    source_schema, source_table, source_fqn, source_kind, target_database,
    target_schema, target_table, target_fqn, target_kind, edge_type,
    discovery_source, evidence."""
    sf_session.execute(
        """
        DELETE FROM LINEAGE_EDGES
        WHERE CONNECTION_ID = %(cid)s
          AND (SOURCE_DATABASE = %(db)s OR TARGET_DATABASE = %(db)s)
        """,
        {"cid": connection_id, "db": database_name},
    )
    if not rows:
        return 0

    select_clauses = []
    params: Dict[str, Any] = {"cid": connection_id}
    for i, r in enumerate(rows):
        select_clauses.append(
            f"SELECT %(id{i})s, %(cid)s, "
            f"%(sdb{i})s, %(ssc{i})s, %(stb{i})s, %(sfq{i})s, %(sk{i})s, "
            f"%(tdb{i})s, %(tsc{i})s, %(ttb{i})s, %(tfq{i})s, %(tk{i})s, "
            f"%(et{i})s, %(ds{i})s, PARSE_JSON(%(ev{i})s), "
            f"CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP()"
        )
        params[f"id{i}"]  = _new_id()
        params[f"sdb{i}"] = r.get("source_database")
        params[f"ssc{i}"] = r.get("source_schema")
        params[f"stb{i}"] = r.get("source_table")
        params[f"sfq{i}"] = r.get("source_fqn")
        params[f"sk{i}"]  = r.get("source_kind")
        params[f"tdb{i}"] = r.get("target_database")
        params[f"tsc{i}"] = r.get("target_schema")
        params[f"ttb{i}"] = r.get("target_table")
        params[f"tfq{i}"] = r.get("target_fqn")
        params[f"tk{i}"]  = r.get("target_kind")
        params[f"et{i}"]  = r.get("edge_type")
        params[f"ds{i}"]  = r.get("discovery_source")
        params[f"ev{i}"]  = _json_or_null(r.get("evidence"))

    sf_session.execute(
        f"""
        INSERT INTO LINEAGE_EDGES
            (ID, CONNECTION_ID,
             SOURCE_DATABASE, SOURCE_SCHEMA, SOURCE_TABLE, SOURCE_FQN, SOURCE_KIND,
             TARGET_DATABASE, TARGET_SCHEMA, TARGET_TABLE, TARGET_FQN, TARGET_KIND,
             EDGE_TYPE, DISCOVERY_SOURCE, EVIDENCE,
             FIRST_DISCOVERED_AT, LAST_SEEN_AT)
        {' UNION ALL '.join(select_clauses)}
        """,
        params,
    )
    return len(rows)


# ── Refresh state ─────────────────────────────────────────────────────────

def _refresh_state_from_row(row: dict) -> SimpleNamespace:
    return SimpleNamespace(
        connection_id=row["CONNECTION_ID"],
        database_name=row["DATABASE_NAME"],
        last_refreshed_at=row.get("LAST_REFRESHED_AT"),
        last_status=row.get("LAST_STATUS"),
        last_error=row.get("LAST_ERROR"),
        edge_count=row.get("EDGE_COUNT") or 0,
        discovery_method_used=row.get("DISCOVERY_METHOD_USED"),
        partial_failures=_parse_json(row.get("PARTIAL_FAILURES")) or [],
    )


def get_refresh_state(connection_id: str, database_name: str) -> Optional[SimpleNamespace]:
    rows = sf_session.query(
        """
        SELECT * FROM LINEAGE_REFRESH_STATE
        WHERE CONNECTION_ID = %(cid)s AND DATABASE_NAME = %(db)s
        """,
        {"cid": connection_id, "db": database_name},
    )
    return _refresh_state_from_row(rows[0]) if rows else None


def list_refresh_states(connection_id: str) -> list[SimpleNamespace]:
    rows = sf_session.query(
        """
        SELECT * FROM LINEAGE_REFRESH_STATE
        WHERE CONNECTION_ID = %(cid)s
        ORDER BY LAST_REFRESHED_AT DESC NULLS LAST
        """,
        {"cid": connection_id},
    )
    return [_refresh_state_from_row(r) for r in rows]


def upsert_refresh_state(
    connection_id: str,
    database_name: str,
    status: str,
    edge_count: int,
    method_used: Optional[str],
    error: Optional[str] = None,
    partial_failures: Optional[list] = None,
) -> None:
    """MERGE by (connection_id, database_name). Snowflake accepts MERGE with
    PARSE_JSON in the SELECT sub-query the same way our INSERT ... SELECT
    pattern does."""
    sf_session.execute(
        """
        MERGE INTO LINEAGE_REFRESH_STATE t USING (
            SELECT %(cid)s AS CONNECTION_ID, %(db)s AS DATABASE_NAME,
                   %(status)s AS LAST_STATUS, %(ec)s AS EDGE_COUNT,
                   %(m)s AS DISCOVERY_METHOD_USED, %(err)s AS LAST_ERROR,
                   PARSE_JSON(%(pf)s) AS PARTIAL_FAILURES
        ) s
        ON t.CONNECTION_ID = s.CONNECTION_ID AND t.DATABASE_NAME = s.DATABASE_NAME
        WHEN MATCHED THEN UPDATE SET
            LAST_REFRESHED_AT = CURRENT_TIMESTAMP(),
            LAST_STATUS = s.LAST_STATUS,
            EDGE_COUNT = s.EDGE_COUNT,
            DISCOVERY_METHOD_USED = s.DISCOVERY_METHOD_USED,
            LAST_ERROR = s.LAST_ERROR,
            PARTIAL_FAILURES = s.PARTIAL_FAILURES
        WHEN NOT MATCHED THEN INSERT
            (CONNECTION_ID, DATABASE_NAME, LAST_REFRESHED_AT, LAST_STATUS,
             EDGE_COUNT, DISCOVERY_METHOD_USED, LAST_ERROR, PARTIAL_FAILURES)
            VALUES
            (s.CONNECTION_ID, s.DATABASE_NAME, CURRENT_TIMESTAMP(), s.LAST_STATUS,
             s.EDGE_COUNT, s.DISCOVERY_METHOD_USED, s.LAST_ERROR, s.PARTIAL_FAILURES)
        """,
        {
            "cid": connection_id, "db": database_name,
            "status": status, "ec": edge_count, "m": method_used,
            "err": (error or "")[:2000] if error else None,
            "pf": _json_or_null(partial_failures or []),
        },
    )


# ── Capability cache (GET_LINEAGE availability probe) ─────────────────────

def _capability_from_row(row: dict) -> SimpleNamespace:
    return SimpleNamespace(
        connection_id=row["CONNECTION_ID"],
        get_lineage_available=row.get("GET_LINEAGE_AVAILABLE"),
        probed_at=row.get("PROBED_AT"),
        probe_error=row.get("PROBE_ERROR"),
    )


def get_lineage_capability(connection_id: str) -> Optional[SimpleNamespace]:
    rows = sf_session.query(
        "SELECT * FROM LINEAGE_CAPABILITY_CACHE WHERE CONNECTION_ID = %(cid)s",
        {"cid": connection_id},
    )
    return _capability_from_row(rows[0]) if rows else None


def set_lineage_capability(
    connection_id: str, available: bool, error: Optional[str] = None,
) -> None:
    sf_session.execute(
        """
        MERGE INTO LINEAGE_CAPABILITY_CACHE t USING (
            SELECT %(cid)s AS CONNECTION_ID, %(av)s AS GET_LINEAGE_AVAILABLE,
                   %(err)s AS PROBE_ERROR
        ) s
        ON t.CONNECTION_ID = s.CONNECTION_ID
        WHEN MATCHED THEN UPDATE SET
            GET_LINEAGE_AVAILABLE = s.GET_LINEAGE_AVAILABLE,
            PROBED_AT = CURRENT_TIMESTAMP(),
            PROBE_ERROR = s.PROBE_ERROR
        WHEN NOT MATCHED THEN INSERT
            (CONNECTION_ID, GET_LINEAGE_AVAILABLE, PROBED_AT, PROBE_ERROR)
            VALUES (s.CONNECTION_ID, s.GET_LINEAGE_AVAILABLE, CURRENT_TIMESTAMP(), s.PROBE_ERROR)
        """,
        {"cid": connection_id, "av": bool(available),
         "err": (error or "")[:2000] if error else None},
    )


# ── Batched overlay counts (used by /lineage/graph/{db}/{schema}) ─────────

def count_open_findings_by_asset(asset_ids: list[str]) -> Dict[str, int]:
    """One aggregate query returning {asset_id: open_finding_count} for a set
    of asset ids. Powers the red-circle badge on lineage table nodes without
    N per-node round-trips."""
    if not asset_ids:
        return {}
    in_open = ", ".join(f"'{s}'" for s in _LIFECYCLE_OPEN)
    placeholders = ", ".join(f"%(a{n})s" for n in range(len(asset_ids)))
    params = {f"a{n}": aid for n, aid in enumerate(asset_ids)}
    rows = sf_session.query(
        f"""
        SELECT ASSET_ID, COUNT(*) AS CT
        FROM FINDINGS
        WHERE ASSET_ID IN ({placeholders}) AND STATUS IN ({in_open})
        GROUP BY ASSET_ID
        """,
        params,
    )
    return {r["ASSET_ID"]: int(r["CT"] or 0) for r in rows}


def count_rules_run_by_table(
    database_name: str, schema_name: Optional[str] = None,
) -> Dict[tuple, int]:
    """{(db, schema, table): rule_execution_count} across the given scope.
    Joins RULE_EXECUTIONS × RULE_INSTANCES (only active instances) so counts
    reflect rules currently in effect."""
    where = ["I.DATABASE_NAME = %(db)s", "I.IS_ACTIVE = TRUE"]
    params: Dict[str, Any] = {"db": database_name}
    if schema_name:
        where.append("I.SCHEMA_NAME = %(sc)s")
        params["sc"] = schema_name
    rows = sf_session.query(
        f"""
        SELECT I.DATABASE_NAME AS DB, I.SCHEMA_NAME AS SC, I.TABLE_NAME AS TB,
               COUNT(E.ID) AS CT
        FROM RULE_EXECUTIONS E
        JOIN RULE_INSTANCES I ON I.ID = E.INSTANCE_ID
        WHERE {' AND '.join(where)}
        GROUP BY I.DATABASE_NAME, I.SCHEMA_NAME, I.TABLE_NAME
        """,
        params,
    )
    return {(r["DB"], r["SC"], r["TB"]): int(r["CT"] or 0) for r in rows}


def batch_health_scores(
    database_name: str, schema_name: Optional[str] = None,
) -> Dict[tuple, Optional[float]]:
    """Severity-weighted pass-rate per (db, schema, table) computed in one
    Snowflake round-trip. Formula matches api/table_health.py::get_table_health
    exactly (see SEVERITY_WEIGHT + HISTORY_LIMIT above): for each active
    instance, take pass_rate over the last HISTORY_LIMIT executions, then
    weight by severity and average across the table's instances.

    Returns {(db, sc, tb): score in [0.0, 1.0]}. Tables with no runs are
    absent from the returned dict (frontend treats absent as null/grey)."""
    where = ["I.DATABASE_NAME = %(db)s", "I.IS_ACTIVE = TRUE"]
    params: Dict[str, Any] = {"db": database_name, "hist": HISTORY_LIMIT}
    if schema_name:
        where.append("I.SCHEMA_NAME = %(sc)s")
        params["sc"] = schema_name

    # Per-instance pass-rate over last HISTORY_LIMIT executions, then average
    # weighted by SEVERITY_WEIGHT for the containing table. All in one query.
    weight_case = " ".join(
        f"WHEN '{sev}' THEN {w}" for sev, w in SEVERITY_WEIGHT.items()
    )
    rows = sf_session.query(
        f"""
        WITH RECENT AS (
            SELECT E.INSTANCE_ID, E.STATUS,
                   ROW_NUMBER() OVER (PARTITION BY E.INSTANCE_ID ORDER BY E.EXECUTED_AT DESC) AS RN
            FROM RULE_EXECUTIONS E
            JOIN RULE_INSTANCES I ON I.ID = E.INSTANCE_ID
            WHERE {' AND '.join(where)}
        ),
        PER_INSTANCE AS (
            SELECT R.INSTANCE_ID,
                   SUM(CASE WHEN R.STATUS = 'passed' THEN 1 ELSE 0 END) AS P,
                   SUM(CASE WHEN R.STATUS IN ('passed','failed','error') THEN 1 ELSE 0 END) AS T
            FROM RECENT R
            WHERE R.RN <= %(hist)s
            GROUP BY R.INSTANCE_ID
        )
        SELECT I.DATABASE_NAME AS DB, I.SCHEMA_NAME AS SC, I.TABLE_NAME AS TB,
               SUM(
                 (CASE LOWER(COALESCE(I.SEVERITY,'medium')) {weight_case} ELSE 2 END)
                 * (P.P * 1.0 / P.T)
               ) AS WEIGHTED_PASS,
               SUM(
                 CASE LOWER(COALESCE(I.SEVERITY,'medium')) {weight_case} ELSE 2 END
               ) AS TOTAL_WEIGHT
        FROM PER_INSTANCE P
        JOIN RULE_INSTANCES I ON I.ID = P.INSTANCE_ID
        WHERE P.T > 0
        GROUP BY I.DATABASE_NAME, I.SCHEMA_NAME, I.TABLE_NAME
        """,
        params,
    )
    out: Dict[tuple, Optional[float]] = {}
    for r in rows:
        tw = r.get("TOTAL_WEIGHT")
        wp = r.get("WEIGHTED_PASS")
        if tw and float(tw) > 0:
            out[(r["DB"], r["SC"], r["TB"])] = float(wp) / float(tw)
    return out


def list_databases_for_connection(connection_id: str) -> list[str]:
    """Distinct database names present in ASSETS (tables) — regardless of
    whether they've been lineage-refreshed yet. Feeds the all-databases
    graph's node list so unlinked DBs still appear."""
    rows = sf_session.query(
        """
        SELECT DISTINCT DATABASE_NAME
        FROM ASSETS
        WHERE ASSET_TYPE = 'table' AND DATABASE_NAME IS NOT NULL
        ORDER BY DATABASE_NAME
        """,
    )
    return [r["DATABASE_NAME"] for r in rows]


# ── LINEAGE_CATALOG — full Snowflake object catalog (not just scanned rows) ─

def _catalog_from_row(row: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id=row["ID"],
        connection_id=row["CONNECTION_ID"],
        database_name=row["DATABASE_NAME"],
        schema_name=row.get("SCHEMA_NAME"),
        table_name=row.get("TABLE_NAME"),
        object_kind=row["OBJECT_KIND"],
        fqn=row.get("FQN"),
        row_count=row.get("ROW_COUNT"),
        size_bytes=row.get("SIZE_BYTES"),
        comment=row.get("COMMENT"),
        indexed_at=row.get("INDEXED_AT"),
    )


def list_catalog(
    connection_id: str,
    database_name: Optional[str] = None,
    schema_name: Optional[str] = None,
    kinds: Optional[list[str]] = None,
) -> list[SimpleNamespace]:
    where = ["CONNECTION_ID = %(cid)s"]
    params: Dict[str, Any] = {"cid": connection_id}
    if database_name:
        where.append("DATABASE_NAME = %(db)s")
        params["db"] = database_name
    if schema_name:
        where.append("SCHEMA_NAME = %(sc)s")
        params["sc"] = schema_name
    if kinds:
        ks = ", ".join(f"'{k}'" for k in kinds)
        where.append(f"OBJECT_KIND IN ({ks})")
    rows = sf_session.query(
        f"""
        SELECT * FROM LINEAGE_CATALOG
        WHERE {' AND '.join(where)}
        ORDER BY DATABASE_NAME, SCHEMA_NAME NULLS FIRST, TABLE_NAME NULLS FIRST
        """,
        params,
    )
    return [_catalog_from_row(r) for r in rows]


def replace_catalog_for_database(
    connection_id: str,
    database_name: str,
    rows: list[dict],
) -> int:
    """Atomic replace of catalog rows for one (connection, database). Row
    dicts carry: object_kind, database_name, schema_name, table_name, fqn,
    row_count, size_bytes, comment."""
    sf_session.execute(
        """
        DELETE FROM LINEAGE_CATALOG
        WHERE CONNECTION_ID = %(cid)s AND DATABASE_NAME = %(db)s
        """,
        {"cid": connection_id, "db": database_name},
    )
    if not rows:
        return 0

    select_clauses = []
    params: Dict[str, Any] = {"cid": connection_id}
    for i, r in enumerate(rows):
        select_clauses.append(
            f"SELECT %(id{i})s, %(cid)s, %(db{i})s, %(sc{i})s, %(tb{i})s, "
            f"%(k{i})s, %(fq{i})s, %(rc{i})s, %(sb{i})s, %(cm{i})s, CURRENT_TIMESTAMP()"
        )
        params[f"id{i}"] = _new_id()
        params[f"db{i}"] = r.get("database_name")
        params[f"sc{i}"] = r.get("schema_name")
        params[f"tb{i}"] = r.get("table_name")
        params[f"k{i}"]  = r.get("object_kind")
        params[f"fq{i}"] = r.get("fqn")
        params[f"rc{i}"] = r.get("row_count")
        params[f"sb{i}"] = r.get("size_bytes")
        params[f"cm{i}"] = (r.get("comment") or "")[:2000] if r.get("comment") else None

    sf_session.execute(
        f"""
        INSERT INTO LINEAGE_CATALOG
            (ID, CONNECTION_ID, DATABASE_NAME, SCHEMA_NAME, TABLE_NAME,
             OBJECT_KIND, FQN, ROW_COUNT, SIZE_BYTES, COMMENT, INDEXED_AT)
        {' UNION ALL '.join(select_clauses)}
        """,
        params,
    )
    return len(rows)


def indexed_databases(connection_id: str) -> list[str]:
    """Distinct DATABASE_NAME rows present in LINEAGE_CATALOG for this
    connection — i.e. databases the user has clicked 'Index catalog' for."""
    rows = sf_session.query(
        """
        SELECT DISTINCT DATABASE_NAME FROM LINEAGE_CATALOG
        WHERE CONNECTION_ID = %(cid)s
        ORDER BY DATABASE_NAME
        """,
        {"cid": connection_id},
    )
    return [r["DATABASE_NAME"] for r in rows]

