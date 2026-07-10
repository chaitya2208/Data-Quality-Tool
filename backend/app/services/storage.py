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
import uuid
from types import SimpleNamespace
from typing import Any, Optional

from app.services.snowflake_session import session as sf_session


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
    return json.loads(value)


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
) -> SimpleNamespace:
    scan_id = _new_id()
    sf_session.execute(
        """
        INSERT INTO SCANS (ID, ASSET_ID, SCAN_TYPE, STATUS, STARTED_AT)
        VALUES (%(id)s, %(asset_id)s, %(scan_type)s, %(status)s, %(started_at)s)
        """,
        {
            "id": scan_id,
            "asset_id": asset_id,
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


def list_findings(
    asset_id: Optional[str] = None,
    scan_id: Optional[str] = None,
    status: Optional[str] = None,
    severity: Optional[str] = None,
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
    if status:
        where.append("STATUS = %(status)s")
        params["status"] = status
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
    return total, [_finding_from_row(r) for r in rows]


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


def findings_with_asset_not_closed() -> list[tuple[SimpleNamespace, SimpleNamespace]]:
    """(finding, asset) pairs for every finding not RESOLVED/CLOSED — dashboard chart."""
    rows = sf_session.query(
        """
        SELECT f.*, a.DATABASE_NAME AS A_DATABASE_NAME, a.SCHEMA_NAME AS A_SCHEMA_NAME,
               a.TABLE_NAME AS A_TABLE_NAME, a.FQN AS A_FQN
        FROM FINDINGS f
        JOIN ASSETS a ON a.ID = f.ASSET_ID
        WHERE f.STATUS NOT IN ('resolved', 'closed')
        """
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


def findings_summary() -> dict:
    """total / by_status / by_severity counts — dashboard stats."""
    total_rows = sf_session.query("SELECT COUNT(*) AS CNT FROM FINDINGS")
    total = total_rows[0]["CNT"] if total_rows else 0

    status_rows = sf_session.query("SELECT STATUS, COUNT(*) AS CNT FROM FINDINGS GROUP BY STATUS")
    by_status = {r["STATUS"]: r["CNT"] for r in status_rows}

    severity_rows = sf_session.query("SELECT SEVERITY, COUNT(*) AS CNT FROM FINDINGS GROUP BY SEVERITY")
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


def get_definition_by_handler_key(handler_key: str) -> Optional[SimpleNamespace]:
    rows = sf_session.query(
        "SELECT * FROM RULE_DEFINITIONS WHERE HANDLER_KEY = %(handler_key)s",
        {"handler_key": handler_key},
    )
    return _definition_from_row(rows[0]) if rows else None


def get_definition_by_name(name: str) -> Optional[SimpleNamespace]:
    rows = sf_session.query("SELECT * FROM RULE_DEFINITIONS WHERE NAME = %(name)s", {"name": name})
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


def create_definition(
    name: str,
    category: str,
    description: str,
    check_kind: str,
    default_severity: str,
    allowed_scopes: list[str],
    handler_key: Optional[str] = None,
    sql_template: Optional[str] = None,
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
             PARAMETERS_SCHEMA, DEFAULT_THRESHOLD_CONFIG, DEFAULT_SEVERITY, ALLOWED_SCOPES,
             SOURCE, STATUS, OWNER, CREATED_BY)
        SELECT
            %(id)s, %(name)s, %(category)s, %(description)s, %(check_kind)s, %(handler_key)s,
            %(sql_template)s, PARSE_JSON(%(parameters_schema)s), PARSE_JSON(%(default_threshold_config)s),
            %(default_severity)s, PARSE_JSON(%(allowed_scopes)s), %(source)s, %(status)s,
            %(owner)s, %(created_by)s
        """,
        {
            "id": definition_id,
            "name": name,
            "category": category,
            "description": description,
            "check_kind": check_kind,
            "handler_key": handler_key,
            "sql_template": sql_template,
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
             TARGET_CONFIG, THRESHOLD_CONFIG, SEVERITY, RULE_SQL, STATUS, FINGERPRINT,
             IS_ACTIVE, JIRA_TICKET, OWNER, CREATED_BY, SOURCE_RUN_ID)
        SELECT
            %(id)s, %(definition_id)s, %(scope)s, %(database_name)s, %(schema_name)s,
            %(table_name)s, PARSE_JSON(%(target_config)s), PARSE_JSON(%(threshold_config)s),
            %(severity)s, %(rule_sql)s, %(status)s, %(fingerprint)s, %(is_active)s,
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
) -> SimpleNamespace:
    run_id = run_id or _new_id()
    sf_session.execute(
        """
        INSERT INTO AGENT_RUNS
            (ID, BATCH_ID, BATCH_INDEX, DATABASE_NAME, SCHEMA_NAME, TABLE_NAME, STATUS)
        VALUES
            (%(id)s, %(batch_id)s, %(batch_index)s, %(database)s, %(schema_name)s,
             %(table)s, %(status)s)
        """,
        {
            "id": run_id,
            "batch_id": batch_id,
            "batch_index": batch_index,
            "database": database,
            "schema_name": schema_name,
            "table": table,
            "status": status,
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
    return [_agent_run_from_row(r, tasks=list_agent_tasks(r["ID"])) for r in rows]


def list_agent_runs_by_batch(batch_id: str) -> list[SimpleNamespace]:
    rows = sf_session.query(
        "SELECT * FROM AGENT_RUNS WHERE BATCH_ID = %(batch_id)s ORDER BY BATCH_INDEX ASC",
        {"batch_id": batch_id},
    )
    return [_agent_run_from_row(r, tasks=list_agent_tasks(r["ID"])) for r in rows]


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
