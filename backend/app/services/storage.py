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
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
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
    (FINDINGS has no CONNECTION_ID column). Snowflake also absorbs legacy rows
    whose scan is missing or has a NULL connection. `alias` (e.g. "f") qualifies
    the SCAN_ID column when the query aliases FINDINGS."""
    params["conn_id"] = connection_id
    col = f"{alias}.SCAN_ID" if alias else "SCAN_ID"
    scan_match = f"{col} IN (SELECT ID FROM SCANS WHERE CONNECTION_ID = %(conn_id)s)"
    if _is_snowflake_connection(connection_id):
        return (
            f"({scan_match} OR {col} IS NULL "
            f"OR {col} IN (SELECT ID FROM SCANS WHERE CONNECTION_ID IS NULL))"
        )
    return scan_match


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


# Open (non-closed) finding statuses — anything not here is considered resolved/
# closed and won't be superseded or shown under "Detected".
_OPEN_FINDING_STATUSES = ("detected", "validated", "in_progress")


def supersede_open_findings(
    table_asset_id: str,
    instance_ids,
    except_scan_id: str,
) -> int:
    """
    Mark still-open findings from PRIOR scans as 'superseded' when a new scan is
    about to re-create findings for the same instances. Without this, re-running
    a workflow on a table leaves stale 'detected' twins from the old scan, so one
    real issue appears in both Detected and Resolved.

    Scoped by INSTANCE_ID (these are this run's approved instances, all bound to
    this table), still-open status only, and a DIFFERENT scan than the one being
    created. `table_asset_id` is accepted for clarity/future use. Returns rows
    affected.
    """
    ids = [i for i in (instance_ids or []) if i]
    if not ids:
        return 0
    in_open = ", ".join(f"'{s}'" for s in _OPEN_FINDING_STATUSES)
    placeholders = ", ".join(f"%(iid{n})s" for n in range(len(ids)))
    params = {"except_scan": except_scan_id}
    for n, iid in enumerate(ids):
        params[f"iid{n}"] = iid
    affected = sf_session.execute(
        f"""
        UPDATE FINDINGS
        SET STATUS = 'superseded', CLOSED_AT = CURRENT_TIMESTAMP(),
            UPDATED_AT = CURRENT_TIMESTAMP()
        WHERE INSTANCE_ID IN ({placeholders})
          AND STATUS IN ({in_open})
          AND (SCAN_ID IS NULL OR SCAN_ID <> %(except_scan)s)
        """,
        params,
    )
    if affected:
        logger.info(f"[storage] Superseded {affected} stale open findings for table asset {table_asset_id}")
    return affected or 0


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


def get_definition_by_name(name: str) -> Optional[SimpleNamespace]:
    rows = sf_session.query("SELECT * FROM RULE_DEFINITIONS WHERE NAME = %(name)s", {"name": name})
    return _definition_from_row(rows[0]) if rows else None


def get_definition_by_template_shape(template_shape: str) -> Optional[SimpleNamespace]:
    """Exact lookup for the canonical, system-wide definition backing a
    sql_template shape (not_null, uniqueness, ...) — the fix for definition-
    library explosion: callers check this BEFORE falling back to fuzzy
    name/description similarity or creating a brand-new definition, so every
    table/column proposing the same shape reuses one definition via its own
    TARGET_CONFIG/THRESHOLD_CONFIG/SEVERITY/RATIONALE instead of spawning a
    duplicate. Prefers the highest-approval-count match if more than one
    exists (e.g. a pre-canonicalization duplicate that's still active)."""
    rows = sf_session.query(
        "SELECT * FROM RULE_DEFINITIONS WHERE TEMPLATE_SHAPE = %(shape)s AND STATUS != 'disabled' "
        "ORDER BY APPROVAL_COUNT DESC, CREATED_AT ASC LIMIT 1",
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
    where, params = [], {}
    if source:
        where.append("SOURCE = %(source)s")
        params["source"] = source
    if status:
        where.append("STATUS = %(status)s")
        params["status"] = status
    if category:
        where.append("CATEGORY = %(category)s")
        params["category"] = category
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    total_rows = sf_session.query(f"SELECT COUNT(*) AS CNT FROM RULE_DEFINITIONS {where_sql}", params)
    total = total_rows[0]["CNT"] if total_rows else 0

    rows = sf_session.query(
        f"""
        SELECT * FROM RULE_DEFINITIONS {where_sql}
        ORDER BY APPROVAL_COUNT DESC, CREATED_AT DESC
        LIMIT %(limit)s OFFSET %(skip)s
        """,
        {**params, "limit": limit, "skip": skip},
    )
    return total, [_definition_from_row(r) for r in rows]


def list_all_definitions() -> list[SimpleNamespace]:
    rows = sf_session.query("SELECT * FROM RULE_DEFINITIONS")
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
    (as system/active) if missing. Also ensures one global instance exists."""
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
    ensure_global_instance(existing)
    return existing


def ensure_template_definition(
    template_shape: str,
    name: str,
    description: str,
    category: str,
    severity: str,
    allowed_scopes: list[str],
) -> SimpleNamespace:
    """Return the canonical sql_template definition for `template_shape`,
    auto-creating it (as system/active) if missing — the sql_template analog
    of ensure_definition() above. Unlike ensure_definition, does NOT create a
    global instance: sql_template checks are always table/column-scoped via
    their own TARGET_CONFIG, there is no 'runs everywhere' degenerate case."""
    existing = get_definition_by_template_shape(template_shape)
    if existing:
        return existing
    return create_definition(
        name=name,
        category=category,
        description=description,
        check_kind="sql_template",
        template_shape=template_shape,
        default_severity=severity,
        allowed_scopes=allowed_scopes,
        source="system",
        status="active",
        owner="data-governance-team",
        created_by="system",
    )


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


def list_all_instances() -> list[SimpleNamespace]:
    rows = sf_session.query("SELECT * FROM RULE_INSTANCES")
    return [_instance_from_row(r) for r in rows]


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
    database/schema/table as needed."""
    rows = sf_session.query(
        "SELECT * FROM RULE_INSTANCES WHERE IS_ACTIVE = TRUE AND SCOPE = %(scope)s",
        {"scope": scope},
    )
    return [_instance_from_row(r) for r in rows]


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


def approve_instance(instance_id: str) -> SimpleNamespace:
    instance = update_instance(
        instance_id, status="active", is_active=True, approved_at=datetime.datetime.utcnow(),
    )
    increment_definition_approval_count(instance.definition_id)
    return instance


def reject_instance(instance_id: str, reason: str) -> SimpleNamespace:
    return update_instance(
        instance_id, status="rejected", is_active=False,
        rejection_reason=reason, rejected_at=datetime.datetime.utcnow(),
    )


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


def upsert_relationship(
    database_name: str,
    schema_name: str,
    from_table: str,
    from_column: str,
    to_table: str,
    to_column: str,
    status: str = "confirmed",
    confidence: str = "name_match",
    orphan_rate: Optional[float] = None,
    sample_total: Optional[int] = None,
    sample_orphans: Optional[int] = None,
) -> SimpleNamespace:
    """Insert or refresh one relationship candidate, keyed on
    (database, schema, from_table, from_column, to_table, to_column). Callers
    re-discovering a schema simply upsert every candidate again — no need to
    diff against the previous run's rows first."""
    existing_rows = sf_session.query(
        """
        SELECT ID FROM RELATIONSHIP_CATALOG
        WHERE DATABASE_NAME = %(database_name)s AND SCHEMA_NAME = %(schema_name)s
          AND FROM_TABLE = %(from_table)s AND FROM_COLUMN = %(from_column)s
          AND TO_TABLE = %(to_table)s AND TO_COLUMN = %(to_column)s
        """,
        {
            "database_name": database_name, "schema_name": schema_name,
            "from_table": from_table, "from_column": from_column,
            "to_table": to_table, "to_column": to_column,
        },
    )
    params = {
        "status": status, "confidence": confidence, "orphan_rate": orphan_rate,
        "sample_total": sample_total, "sample_orphans": sample_orphans,
    }
    if existing_rows:
        relationship_id = existing_rows[0]["ID"]
        sf_session.execute(
            """
            UPDATE RELATIONSHIP_CATALOG
            SET STATUS = %(status)s, CONFIDENCE = %(confidence)s, ORPHAN_RATE = %(orphan_rate)s,
                SAMPLE_TOTAL = %(sample_total)s, SAMPLE_ORPHANS = %(sample_orphans)s,
                LAST_VERIFIED_AT = CURRENT_TIMESTAMP()
            WHERE ID = %(id)s
            """,
            {**params, "id": relationship_id},
        )
    else:
        relationship_id = _new_id()
        sf_session.execute(
            """
            INSERT INTO RELATIONSHIP_CATALOG
                (ID, DATABASE_NAME, SCHEMA_NAME, FROM_TABLE, FROM_COLUMN, TO_TABLE, TO_COLUMN,
                 STATUS, CONFIDENCE, ORPHAN_RATE, SAMPLE_TOTAL, SAMPLE_ORPHANS)
            SELECT
                %(id)s, %(database_name)s, %(schema_name)s, %(from_table)s, %(from_column)s,
                %(to_table)s, %(to_column)s, %(status)s, %(confidence)s, %(orphan_rate)s,
                %(sample_total)s, %(sample_orphans)s
            """,
            {
                **params, "id": relationship_id,
                "database_name": database_name, "schema_name": schema_name,
                "from_table": from_table, "from_column": from_column,
                "to_table": to_table, "to_column": to_column,
            },
        )
    rows = sf_session.query("SELECT * FROM RELATIONSHIP_CATALOG WHERE ID = %(id)s", {"id": relationship_id})
    return _relationship_from_row(rows[0])


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


def get_agent_run(run_id: str, with_tasks: bool = True) -> Optional[SimpleNamespace]:
    rows = sf_session.query("SELECT * FROM AGENT_RUNS WHERE ID = %(id)s", {"id": run_id})
    if not rows:
        return None
    tasks = list_agent_tasks(run_id) if with_tasks else []
    return _agent_run_from_row(rows[0], tasks=tasks)


def list_agent_runs(limit: int = 20) -> list[SimpleNamespace]:
    rows = sf_session.query(
        "SELECT * FROM AGENT_RUNS ORDER BY CREATED_AT DESC LIMIT %(limit)s", {"limit": limit}
    )
    result = []
    for r in rows:
        try:
            result.append(_agent_run_from_row(r, tasks=list_agent_tasks(r["ID"])))
        except Exception as e:
            logger.warning(f"[storage] Skipping malformed run {r.get('ID')}: {e}")
    return result


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
    return get_cache_entry(entry_id)


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
    return get_intelligence_log(log_id)


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
    except Exception:
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
    except Exception:
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
    except Exception:
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
    except Exception:
        return []
