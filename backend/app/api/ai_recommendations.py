from fastapi import APIRouter, HTTPException
from app.services import storage
from app.services.snowflake_session import session as sf_session
from app.services import recommendation_cache_service as rec_cache
from app.services.cortex_client import ask_for_recommendation, _sanitize_sql
from pydantic import BaseModel
from typing import List, Any, Optional
from datetime import datetime
import logging

router = APIRouter()
logger = logging.getLogger(__name__)


def _build_context(finding: Any) -> dict:
    """Build a context dict with all substitution values for cache templatization."""
    ctx = finding.context or {}
    asset = storage.get_asset(finding.asset_id)
    data_type = ""
    if asset and asset.raw_metadata:
        data_type = asset.raw_metadata.get("data_type", "")
    return {
        "fqn":           ctx.get("fqn", ""),
        "table_name":    ctx.get("table_name", ""),
        "column_name":   ctx.get("column_name", ""),
        "schema_name":   ctx.get("schema_name", ""),
        "database_name": ctx.get("database_name", ""),
        "data_type":     data_type,
    }


def _call_claude_for_finding(finding: Any, rule_code: str, context: dict):
    """
    Get a fix recommendation for a finding.
    1. Check persistent DB cache (keyed by rule_code + data_type) — instant if hit.
    2. Cache miss → call Cortex (claude-opus-4-8 with live DESCRIBE TABLE schema)
       → falls back to Claude/Bedrock if Cortex unavailable.
    3. Templatize and store result to cache for future reuse.
    """
    full_context = _build_context(finding)
    data_type = full_context["data_type"]
    cache_key = rec_cache.build_cache_key(rule_code, data_type)

    # Resolve the finding's connection up front so BOTH the cache-hit and
    # cache-miss paths can report the source type (snowflake vs postgres) and,
    # for postgres, which connection/user the fix will run as. The AI-Fix UI
    # uses this to show role+warehouse (Snowflake) or a plain connection line
    # (Postgres) instead of forcing Snowflake controls onto every finding.
    conn_info = _resolve_connection_info(finding)

    # ── Cache hit ──────────────────────────────────────────────────────────────
    cached = rec_cache.get_cached(cache_key, full_context)
    if cached:
        return AIRecommendation(
            finding_id=finding.id,
            explanation=cached["explanation"],
            sql_query=cached["sql_query"],
            confidence=cached["confidence"],
            impact=cached["impact"],
            from_cache=True,
            source="cache",
            **conn_info,
        )

    # ── Cache miss: call Cortex (with live schema) → fallback Claude ──────────
    rule = storage.get_rule_view(finding.instance_id) if finding.instance_id else None
    # Resolve the finding's data source (via its scan) so recommendations use
    # the right dialect and skip Cortex for non-Snowflake sources.
    from app.services.datasources import get_source
    scan = storage.get_scan(finding.scan_id) if finding.scan_id else None
    source = None
    try:
        source = get_source(scan.connection_id if scan else None)
    except Exception:
        source = None

    result = ask_for_recommendation(
        rule_code=rule_code,
        rule_description=rule.description if rule else "",
        finding_title=finding.title,
        fqn=full_context["fqn"],
        database=full_context["database_name"],
        schema=full_context["schema_name"],
        table=full_context["table_name"],
        column_name=full_context["column_name"],
        data_type=data_type,
        evidence=finding.evidence or {},
        source=source,
    )

    explanation = result.get("explanation", "No explanation provided")
    sql_query   = _sanitize_sql(result.get("sql_query", "-- No SQL generated"))
    confidence  = int(result.get("confidence", 75))
    impact      = result.get("impact", "")
    source      = result.get("source", "unknown")

    # Store to persistent cache (templatized) — skip if generation failed
    if source != "error":
        rec_cache.store(
            cache_key=cache_key,
            rule_code=rule_code,
            data_type=data_type,
            context=full_context,
            explanation=explanation,
            sql_query=sql_query,
            confidence=confidence,
            impact=impact,
        )

    return AIRecommendation(
        finding_id=finding.id,
        explanation=explanation,
        sql_query=sql_query,
        confidence=confidence,
        impact=impact,
        from_cache=False,
        source=source,
        **conn_info,
    )


def _resolve_connection_info(finding: Any) -> dict:
    """
    {source_type, connection_name, connection_user} for the finding's data
    source, derived from its scan's connection. Defaults to snowflake with no
    name/user (the legacy single-source case) when the connection can't be
    resolved. Never raises — recommendation generation shouldn't fail over this.
    """
    info = {"source_type": "snowflake", "connection_name": None, "connection_user": None}
    try:
        scan = storage.get_scan(finding.scan_id) if finding.scan_id else None
        conn = storage.get_connection_record(scan.connection_id) if scan and scan.connection_id else None
        if conn:
            ctype = conn.type.value if hasattr(conn.type, "value") else str(conn.type)
            info["source_type"] = ctype or "snowflake"
            info["connection_name"] = conn.name
            info["connection_user"] = conn.username
    except Exception as e:
        logger.debug(f"[AI] Could not resolve connection info for finding {getattr(finding, 'id', '?')}: {e}")
    return info


class AIRecommendation(BaseModel):
    finding_id: str
    explanation: str
    sql_query: str
    confidence: int
    impact: str
    from_cache: bool = False
    source: str = "unknown"  # cortex | claude | cache | error
    # Which data source the fix will run against, so the UI shows the right
    # execution controls (Snowflake role+warehouse vs. a Postgres connection).
    source_type: str = "snowflake"        # snowflake | postgres
    connection_name: Optional[str] = None  # for the Postgres "runs on <conn>" line
    connection_user: Optional[str] = None  # the Postgres user the fix runs as


class WarehouseInfo(BaseModel):
    name: str
    size: str
    state: str


class RoleInfo(BaseModel):
    name: str
    is_current: bool
    is_default: bool


class SnowflakeContext(BaseModel):
    user: str
    current_role: str
    roles: List[RoleInfo]
    warehouses: List[WarehouseInfo]
    databases: List[str]


class ExecuteSQLRequest(BaseModel):
    finding_id: str
    sql_query: str
    # Snowflake-only execution context. Omitted for Postgres/RDS findings, whose
    # execute path ignores them (the fix runs as the connection's Postgres user).
    warehouse: Optional[str] = None
    role: Optional[str] = None


class ExecuteSQLResponse(BaseModel):
    success: bool
    message: str
    finding_id: str
    warehouse_used: Optional[str] = None
    role_used: Optional[str] = None
    executed_at: datetime


@router.get("/context", response_model=SnowflakeContext)
def get_snowflake_context():
    """
    Return everything the frontend needs in one call — user info, roles,
    warehouses, databases — all served from the startup cache.
    No Snowflake round-trip, no SSO.
    """
    ctx = sf_session.get_cached_context()
    if not ctx:
        raise HTTPException(
            status_code=503,
            detail="Snowflake context not ready. Backend may still be starting up."
        )
    return SnowflakeContext(
        user=ctx["user"],
        current_role=ctx["current_role"],
        roles=[RoleInfo(**r) for r in ctx["roles"]],
        warehouses=[WarehouseInfo(**w) for w in ctx["warehouses"]],
        databases=ctx["databases"],
    )


@router.get("/warehouses", response_model=List[WarehouseInfo])
def get_warehouses():
    """Serves from startup cache — instant, no SSO."""
    ctx = sf_session.get_cached_context()
    if not ctx:
        raise HTTPException(status_code=503, detail="Context not ready")
    return [WarehouseInfo(**w) for w in ctx["warehouses"]]


@router.get("/roles", response_model=List[RoleInfo])
def get_roles():
    """Serves from startup cache — instant, no SSO."""
    ctx = sf_session.get_cached_context()
    if not ctx:
        raise HTTPException(status_code=503, detail="Context not ready")
    return [RoleInfo(**r) for r in ctx["roles"]]


@router.post("/recommendations", response_model=List[AIRecommendation])
def get_ai_recommendations(finding_ids: List[str]):
    """
    Generate AI recommendations for selected findings using Claude.
    Cache-first: same rule_code + data_type reuses a prior response.
    Falls back to template-based SQL if Claude call fails.
    """
    recommendations = []
    for finding_id in finding_ids:
        finding = storage.get_finding(finding_id)
        if not finding:
            continue
        context = finding.context or {}
        rule_code = context.get("rule_code", "")

        try:
            rec = _call_claude_for_finding(finding, rule_code, context)
        except Exception as e:
            logger.warning(f"Claude call failed for {finding_id}, using template: {e}")
            rec = _generate_recommendation(finding, rule_code, context)
            # The template fallback doesn't resolve the source; stamp the
            # connection info so the UI still branches correctly (Postgres vs
            # Snowflake) instead of defaulting every fallback to Snowflake.
            conn_info = _resolve_connection_info(finding)
            rec.source_type      = conn_info["source_type"]
            rec.connection_name  = conn_info["connection_name"]
            rec.connection_user  = conn_info["connection_user"]

        recommendations.append(rec)
    return recommendations


@router.post("/execute", response_model=ExecuteSQLResponse)
def execute_sql_fix(request: ExecuteSQLRequest):
    """
    Execute the SQL fix using the SAME connection opened at startup.
    Switches role+warehouse, runs SQL, then restores original state.
    No new SSO prompt ever.
    """
    finding = storage.get_finding(request.finding_id)
    if not finding:
        raise HTTPException(status_code=404, detail="Finding not found")

    # Block execution if SQL still contains unfilled placeholders
    if "<REFERENCED_TABLE>" in request.sql_query or "<REFERENCED_COLUMN>" in request.sql_query:
        raise HTTPException(
            status_code=400,
            detail=(
                "SQL contains placeholder <REFERENCED_TABLE> or <REFERENCED_COLUMN>. "
                "Edit the SQL to replace the placeholder with the actual referenced table "
                "and column before executing."
            )
        )

    try:
        # Resolve the data source this finding belongs to (via its scan's
        # connection_id) and execute against it. Snowflake honors role/warehouse;
        # Postgres runs the statement plainly (ignores them).
        from app.services.datasources import get_source
        scan = storage.get_scan(finding.scan_id) if finding.scan_id else None
        connection_id = scan.connection_id if scan else None
        source = get_source(connection_id)
        source.execute_sql(
            request.sql_query,
            role=request.role,
            warehouse=request.warehouse,
        )

        # Only mention role/warehouse when they were actually used (Snowflake);
        # Postgres runs as the connection's user, so omit them.
        ctx_note = ""
        if request.role or request.warehouse:
            ctx_note = f"Role: {request.role}. Warehouse: {request.warehouse}. "
        storage.update_finding(
            finding.id,
            status="resolved",
            resolution_notes=(
                f"Fixed via AI recommendation. {ctx_note}SQL: {request.sql_query}"
            ),
            resolved_at=datetime.utcnow(),
        )

        return ExecuteSQLResponse(
            success=True,
            message="SQL executed successfully and finding resolved",
            finding_id=request.finding_id,
            warehouse_used=request.warehouse,
            role_used=request.role,
            executed_at=datetime.utcnow(),
        )

    except Exception as e:
        logger.error(f"Execution failed for finding {request.finding_id}: {e}")
        ctx_note = ""
        if request.role or request.warehouse:
            ctx_note = f"Role: {request.role}, Warehouse: {request.warehouse}. "
        storage.update_finding(
            finding.id,
            resolution_notes=f"Execution failed. {ctx_note}Error: {str(e)}",
        )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to execute SQL: {str(e)}"
        )


def _generate_recommendation(finding: Any, rule_code: str, context: dict) -> "AIRecommendation":  # noqa: C901
    fqn         = context.get("fqn", "")
    table_fqn   = ".".join(filter(None, [
        context.get("database_name"),
        context.get("schema_name"),
        context.get("table_name"),
    ])) or fqn
    table_name  = context.get("table_name", "unknown")
    column_name = context.get("column_name", "")
    evidence    = finding.evidence or {}

    # ── Documentation rules ──────────────────────────────────────────────────
    if rule_code == "MISSING_TABLE_COMMENT":
        return AIRecommendation(
            finding_id=finding.id,
            explanation=f"Table {table_name} has no description. A comment makes it discoverable and understandable.",
            sql_query=f"COMMENT ON TABLE {fqn} IS 'Describe the purpose of this table here';",
            confidence=95,
            impact="Low risk — metadata only, no data changes",
        )

    if rule_code == "MISSING_COLUMN_COMMENT":
        return AIRecommendation(
            finding_id=finding.id,
            explanation=f"Column {column_name} has no description. Document what it stores.",
            sql_query=f"COMMENT ON COLUMN {fqn} IS 'Describe what {column_name} stores';",
            confidence=90,
            impact="Low risk — metadata only, no data changes",
        )

    if rule_code == "MISSING_TABLE_OWNER":
        return AIRecommendation(
            finding_id=finding.id,
            explanation=f"Table {table_name} has no assigned owner. Ownership ensures accountability.",
            sql_query=(
                f"-- Replace <ROLE_NAME> with the appropriate owning role\n"
                f"GRANT OWNERSHIP ON TABLE {fqn} TO ROLE <ROLE_NAME> COPY CURRENT GRANTS;"
            ),
            confidence=80,
            impact="Medium risk — changes ownership; confirm role before executing",
        )

    # ── Schema structural rules ──────────────────────────────────────────────
    if rule_code == "MISSING_CREATED_AT":
        return AIRecommendation(
            finding_id=finding.id,
            explanation="Adding CREATED_AT lets you track when rows were inserted for auditing and incremental loads.",
            sql_query=(
                f"ALTER TABLE {table_fqn}\n"
                f"  ADD COLUMN CREATED_AT TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP();"
            ),
            confidence=85,
            impact="Low risk — adds a nullable column with a default value",
        )

    if rule_code == "MISSING_UPDATED_AT":
        return AIRecommendation(
            finding_id=finding.id,
            explanation="Adding UPDATED_AT enables CDC and incremental ETL pipelines.",
            sql_query=(
                f"ALTER TABLE {table_fqn}\n"
                f"  ADD COLUMN UPDATED_AT TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP();\n\n"
                f"-- Note: Snowflake does not auto-update this on row changes.\n"
                f"-- Set it explicitly in your DML statements or via a Stream/Task."
            ),
            confidence=85,
            impact="Low risk — adds a nullable column with a default value",
        )

    if rule_code == "NO_PRIMARY_KEY_HINT":
        return AIRecommendation(
            finding_id=finding.id,
            explanation="Adding a surrogate key ensures row uniqueness and simplifies joins.",
            sql_query=(
                f"ALTER TABLE {table_fqn}\n"
                f"  ADD COLUMN {table_name.upper()}_ID NUMBER AUTOINCREMENT PRIMARY KEY;"
            ),
            confidence=70,
            impact="Medium risk — structural change; validate existing data first",
        )

    if rule_code == "TOO_MANY_COLUMNS":
        count = evidence.get("column_count", "?")
        return AIRecommendation(
            finding_id=finding.id,
            explanation=f"Table {table_name} has {count} columns. Consider splitting into focused tables.",
            sql_query=(
                f"-- Review {table_fqn} and identify logical column groupings.\n"
                f"-- Example split pattern:\n"
                f"-- CREATE TABLE {table_fqn}_CORE AS SELECT <core_columns> FROM {table_fqn};\n"
                f"-- CREATE TABLE {table_fqn}_EXTENDED AS SELECT <extended_columns> FROM {table_fqn};"
            ),
            confidence=60,
            impact="High risk — requires data migration and downstream query updates",
        )

    # ── Naming rules ─────────────────────────────────────────────────────────
    if rule_code == "GENERIC_COLUMN_NAME":
        return AIRecommendation(
            finding_id=finding.id,
            explanation=f"Column '{column_name}' is generic. Rename it to describe what it stores.",
            sql_query=(
                f"-- Replace <DESCRIPTIVE_NAME> with a meaningful column name\n"
                f"ALTER TABLE {table_fqn}\n"
                f"  RENAME COLUMN {column_name} TO <DESCRIPTIVE_NAME>;"
            ),
            confidence=75,
            impact="Medium risk — renaming breaks downstream queries; update all references",
        )

    if rule_code == "INCONSISTENT_COLUMN_NAMING":
        return AIRecommendation(
            finding_id=finding.id,
            explanation=f"Table {table_name} mixes naming styles. Standardise to UPPER_SNAKE_CASE.",
            sql_query=(
                f"-- Rename inconsistently-named columns one at a time.\n"
                f"-- Example:\n"
                f"-- ALTER TABLE {table_fqn} RENAME COLUMN firstName TO FIRST_NAME;\n"
                f"-- ALTER TABLE {table_fqn} RENAME COLUMN last_name TO LAST_NAME;"
            ),
            confidence=65,
            impact="Medium risk — renaming breaks downstream queries; coordinate with consumers",
        )

    # ── Security / PII rules ─────────────────────────────────────────────────
    if rule_code == "PII_COLUMN_NO_MASKING":
        col = column_name or "COLUMN_NAME"
        return AIRecommendation(
            finding_id=finding.id,
            explanation=f"Column {col} appears to contain PII. Apply a Dynamic Data Masking policy.",
            sql_query=(
                f"-- Step 1: Create a masking policy\n"
                f"CREATE MASKING POLICY IF NOT EXISTS mask_{col.lower()}_policy\n"
                f"  AS (val STRING) RETURNS STRING ->\n"
                f"  CASE\n"
                f"    WHEN CURRENT_ROLE() IN ('DATA_ADMIN', 'SYSADMIN') THEN val\n"
                f"    ELSE '***MASKED***'\n"
                f"  END;\n\n"
                f"-- Step 2: Apply to the column\n"
                f"ALTER TABLE {table_fqn}\n"
                f"  MODIFY COLUMN {col}\n"
                f"  SET MASKING POLICY mask_{col.lower()}_policy;"
            ),
            confidence=85,
            impact="Low risk — adds masking; does not alter stored data",
        )

    # ── Data quality / type rules ────────────────────────────────────────────
    if rule_code == "BOOLEAN_STORED_AS_VARCHAR":
        actual = evidence.get("actual_type", "VARCHAR")
        return AIRecommendation(
            finding_id=finding.id,
            explanation=f"Column {column_name} is a boolean/flag but stored as {actual}. Convert to BOOLEAN to enforce valid values.",
            sql_query=(
                f"-- Step 1: Add a properly-typed replacement column\n"
                f"ALTER TABLE {table_fqn}\n"
                f"  ADD COLUMN {column_name}_BOOL BOOLEAN;\n\n"
                f"-- Step 2: Migrate data (adjust mapping to match your actual values)\n"
                f"UPDATE {table_fqn}\n"
                f"  SET {column_name}_BOOL = CASE\n"
                f"    WHEN UPPER({column_name}) IN ('Y','YES','TRUE','1') THEN TRUE\n"
                f"    WHEN UPPER({column_name}) IN ('N','NO','FALSE','0') THEN FALSE\n"
                f"    ELSE NULL\n"
                f"  END;\n\n"
                f"-- Step 3: After validating, replace old column\n"
                f"-- ALTER TABLE {table_fqn} DROP COLUMN {column_name};\n"
                f"-- ALTER TABLE {table_fqn} RENAME COLUMN {column_name}_BOOL TO {column_name};"
            ),
            confidence=80,
            impact="Medium risk — validate the value mapping matches your data before dropping the original column",
        )

    if rule_code == "DATE_STORED_AS_VARCHAR":
        actual = evidence.get("actual_type", "VARCHAR")
        return AIRecommendation(
            finding_id=finding.id,
            explanation=f"Column {column_name} stores a date/time as {actual}. Convert to TIMESTAMP_NTZ.",
            sql_query=(
                f"-- Step 1: Add a properly-typed column\n"
                f"ALTER TABLE {table_fqn}\n"
                f"  ADD COLUMN {column_name}_CONVERTED TIMESTAMP_NTZ;\n\n"
                f"-- Step 2: Migrate data (adjust format to match your actual data)\n"
                f"UPDATE {table_fqn}\n"
                f"  SET {column_name}_CONVERTED = TRY_TO_TIMESTAMP({column_name}, 'YYYY-MM-DD HH24:MI:SS');\n\n"
                f"-- Step 3: After validation, rename and drop old column\n"
                f"-- ALTER TABLE {table_fqn} DROP COLUMN {column_name};\n"
                f"-- ALTER TABLE {table_fqn} RENAME COLUMN {column_name}_CONVERTED TO {column_name};"
            ),
            confidence=80,
            impact="Medium risk — verify the date format matches your data before running UPDATE",
        )

    if rule_code == "NULLABLE_ID_COLUMN":
        return AIRecommendation(
            finding_id=finding.id,
            explanation=f"ID column {column_name} allows NULLs. Add NOT NULL after cleaning data.",
            sql_query=(
                f"-- Step 1: Check for existing NULLs\n"
                f"SELECT COUNT(*) FROM {table_fqn} WHERE {column_name} IS NULL;\n\n"
                f"-- Step 2: After confirming no NULLs, add constraint\n"
                f"ALTER TABLE {table_fqn}\n"
                f"  MODIFY COLUMN {column_name} NOT NULL;"
            ),
            confidence=85,
            impact="Low risk if no NULLs exist; handle NULL rows first if they do",
        )

    if rule_code.startswith("COLUMN_") and "WRONG_TYPE" in rule_code:
        actual   = evidence.get("actual_type", "UNKNOWN")
        expected = ", ".join(evidence.get("expected_types", [])[:3])
        return AIRecommendation(
            finding_id=finding.id,
            explanation=f"Column {column_name} is {actual} but should be {expected}. Type mismatch causes silent conversion errors.",
            sql_query=(
                f"-- Step 1: Add a properly-typed replacement column\n"
                f"-- (replace TARGET_TYPE with the correct type, e.g. NUMBER, DATE)\n"
                f"ALTER TABLE {table_fqn}\n"
                f"  ADD COLUMN {column_name}_NEW <TARGET_TYPE>;\n\n"
                f"-- Step 2: Migrate data\n"
                f"UPDATE {table_fqn}\n"
                f"  SET {column_name}_NEW = TRY_CAST({column_name} AS <TARGET_TYPE>);\n\n"
                f"-- Step 3: After validation, replace old column\n"
                f"-- ALTER TABLE {table_fqn} DROP COLUMN {column_name};\n"
                f"-- ALTER TABLE {table_fqn} RENAME COLUMN {column_name}_NEW TO {column_name};"
            ),
            confidence=75,
            impact="Medium risk — validate data conversion before dropping original column",
        )

    if rule_code == "FK_COLUMN_NO_CONSTRAINT":
        return AIRecommendation(
            finding_id=finding.id,
            explanation=f"Column {column_name} looks like a FK. Add an unenforced REFERENCES clause for lineage.",
            sql_query=(
                f"-- Replace <REFERENCED_TABLE> and <REFERENCED_COLUMN> accordingly\n"
                f"ALTER TABLE {table_fqn}\n"
                f"  ADD FOREIGN KEY ({column_name})\n"
                f"  REFERENCES <REFERENCED_TABLE>(<REFERENCED_COLUMN>)\n"
                f"  NOT ENFORCED RELY;"
            ),
            confidence=65,
            impact="Low risk — unenforced constraint; no data validation occurs",
        )

    # ── Fallback ─────────────────────────────────────────────────────────────
    return AIRecommendation(
        finding_id=finding.id,
        explanation="Automated fix suggestion for this rule type is not yet available.",
        sql_query="-- No automated fix available for this rule type",
        confidence=0,
        impact="Unknown",
    )
