"""
Schema drift detection — Tier 1.

Diff live table + column metadata against the previous ASSETS snapshot and
emit findings for structural drift:

    TABLE_REMOVED               — a table that was scanned before is gone
    COLUMN_ADDED                — new column since last scan
    COLUMN_REMOVED              — column dropped since last scan
    COLUMN_TYPE_CHANGED         — data_type changed
    COLUMN_NULLABILITY_CHANGED  — is_nullable flipped

Findings ride the same incident lifecycle as every other rule:
each drift check has a python_handler-shape definition, and a per-table
instance is auto-created on demand (like ensure_global_instance but keyed
by table, since drift is inherently per-table structural monitoring —
not something a human opts into rule-by-rule).

The snapshot is captured BEFORE the scan's asset upserts run. The diff
runs AFTER, comparing the fresh live columns to the stashed prior
snapshot. Downstream (scan_service → FindingsAgent → finalize_scan)
treats these findings identically to dynamic-check findings.

TABLE_ADDED is intentionally not emitted: a scan already implies the
table exists, and every table added to the scan set would fire once —
noise, not signal. The "add" event is captured by ASSETS' created_at.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from app.services import storage

logger = logging.getLogger(__name__)

# handler_key → (name, description, severity)
DRIFT_RULES = {
    "column_added": (
        "Schema Drift — Column Added",
        "A new column appeared in the table since the last scan. New columns "
        "may need masking policies, PII tags, or updates to downstream models.",
        "low",
    ),
    "column_removed": (
        "Schema Drift — Column Removed",
        "A column that existed in the previous scan is no longer present. "
        "This is a breaking change for any downstream consumer that reads it.",
        "high",
    ),
    "column_type_changed": (
        "Schema Drift — Column Data Type Changed",
        "A column's data type changed since the last scan. Type widening is "
        "usually safe; type narrowing or category changes (NUMBER → VARCHAR) "
        "can silently corrupt downstream pipelines.",
        "high",
    ),
    "column_nullability_changed": (
        "Schema Drift — Column Nullability Changed",
        "A column flipped between NULLABLE and NOT NULL since the last scan. "
        "NOT NULL → NULLABLE can introduce nulls into consumers that assume "
        "the column is always present.",
        "medium",
    ),
    "table_removed": (
        "Schema Drift — Table Removed",
        "A table that was scanned previously is no longer present.",
        "critical",
    ),
}

DRIFT_HANDLER_KEYS = frozenset(DRIFT_RULES.keys())


def _ensure_drift_definition(handler_key: str) -> Any:
    name, description, severity = DRIFT_RULES[handler_key]
    return storage.ensure_definition(
        handler_key=handler_key,
        name=name,
        description=description,
        category="schema",
        severity=severity,
        allowed_scopes=["table"],
    )


def _ensure_per_table_drift_instance(
    handler_key: str,
    database_name: str,
    schema_name: str,
    table_name: str,
) -> Any:
    """Get or create the per-table drift instance for this handler.

    Unlike normal python_handler rules (which require RuleIntelligence to
    propose and a human to approve), drift is always-on structural
    monitoring — we auto-provision an instance the first time a table is
    scanned. Keyed by (definition_id, database, schema, table)."""
    definition = _ensure_drift_definition(handler_key)
    from app.services.snowflake_session import session as sf
    rows = sf.query(
        """
        SELECT * FROM RULE_INSTANCES
        WHERE DEFINITION_ID = %(def_id)s
          AND DATABASE_NAME = %(db)s
          AND SCHEMA_NAME   = %(sch)s
          AND TABLE_NAME    = %(tbl)s
        """,
        {"def_id": definition.id, "db": database_name,
         "sch": schema_name, "tbl": table_name},
    )
    if rows:
        # storage._instance_from_row is private; use get_instance instead
        return storage.get_instance(rows[0]["ID"])

    fingerprint = storage._sha256(
        f"{definition.id}|{database_name}.{schema_name}.{table_name}|drift"
    )
    return storage.create_instance(
        definition_id=definition.id,
        scope="table",
        database_name=database_name,
        schema_name=schema_name,
        table_name=table_name,
        fingerprint=fingerprint,
        severity=definition.default_severity,
        target_config={"kind": "schema_drift"},
        status="active",
        is_active=True,
        owner="data-governance-team",
        created_by="system",
    )


def _normalise_type(t: Optional[str]) -> str:
    """Reduce noisy type strings (e.g. 'NUMBER(38,0)' vs 'NUMBER') to their
    canonical head so we don't flag precision-only re-declarations."""
    if not t:
        return ""
    return (t.split("(")[0]).strip().upper()


def _normalise_nullable(v: Any) -> Optional[bool]:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    return str(v).upper() in ("YES", "TRUE", "1", "Y")


def snapshot_columns(
    database: str, schema: str, table: str,
) -> Dict[str, Dict[str, Any]]:
    """Snapshot the CURRENT ASSETS row for every column of this table BEFORE
    the scan upserts overwrite them. Keyed by column name (upper)."""
    prior = storage.list_column_assets(database, schema, table)
    return {
        (c.column_name or "").upper(): {
            "data_type":   (c.raw_metadata or {}).get("data_type"),
            "is_nullable": (c.raw_metadata or {}).get("is_nullable"),
            "asset_id":    c.id,
        }
        for c in prior
        if c.column_name
    }


def _finding(
    asset_id: str,
    scan_id: str,
    instance_id: str,
    severity: str,
    handler_key: str,
    title: str,
    description: str,
    context: Dict[str, Any],
    evidence: Dict[str, Any],
) -> Dict[str, Any]:
    ev = dict(evidence or {})
    ev.setdefault("fail_count", 1)
    ev.setdefault("total_count", 1)
    ev.setdefault("sample_rows", [])
    ctx = dict(context or {})
    ctx.setdefault("rule_code", handler_key.upper())
    return {
        "asset_id": asset_id,
        "scan_id": scan_id,
        "instance_id": instance_id,
        "title": title,
        "description": description,
        "severity": severity,
        "status": "open",
        "context": ctx,
        "evidence": ev,
    }


def detect_column_drift(
    scan_id: str,
    table_asset: Any,
    prior_snapshot: Dict[str, Dict[str, Any]],
    live_columns: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Compare snapshot (before scan) vs live_columns (from the data source
    for this scan) and emit drift findings. All findings are anchored to the
    TABLE asset — drift is a table-level phenomenon even when the change is
    to one column.

    live_columns items must have keys: column_name, data_type, is_nullable.
    """
    if not prior_snapshot:
        # First scan for this table — nothing to compare against.
        return []

    findings: List[Dict[str, Any]] = []
    db = table_asset.database_name
    sch = table_asset.schema_name
    tbl = table_asset.table_name
    fqn = table_asset.fqn

    live_by_name: Dict[str, Dict[str, Any]] = {
        (c.get("column_name") or "").upper(): c for c in live_columns
        if c.get("column_name")
    }

    base_ctx = {
        "database_name": db,
        "schema_name": sch,
        "table_name": tbl,
        "fqn": fqn,
    }

    # ── Added ────────────────────────────────────────────────────────────────
    added = [n for n in live_by_name if n not in prior_snapshot]
    for col in added:
        instance = _ensure_per_table_drift_instance("column_added", db, sch, tbl)
        _, _, sev = DRIFT_RULES["column_added"]
        live = live_by_name[col]
        findings.append(_finding(
            asset_id=table_asset.id, scan_id=scan_id,
            instance_id=instance.id, severity=sev,
            handler_key="column_added",
            title=f"Column {col} added to {tbl}",
            description=(
                f"Column '{col}' ({live.get('data_type', 'UNKNOWN')}) is new "
                f"in {fqn} since the previous scan. Review downstream models "
                "and, if PII, apply a masking policy."
            ),
            context={**base_ctx, "column_name": col},
            evidence={"column_name": col,
                      "data_type": live.get("data_type"),
                      "is_nullable": live.get("is_nullable")},
        ))

    # ── Removed ──────────────────────────────────────────────────────────────
    removed = [n for n in prior_snapshot if n not in live_by_name]
    for col in removed:
        instance = _ensure_per_table_drift_instance("column_removed", db, sch, tbl)
        _, _, sev = DRIFT_RULES["column_removed"]
        prior = prior_snapshot[col]
        findings.append(_finding(
            asset_id=table_asset.id, scan_id=scan_id,
            instance_id=instance.id, severity=sev,
            handler_key="column_removed",
            title=f"Column {col} removed from {tbl}",
            description=(
                f"Column '{col}' was present in the previous scan of {fqn} "
                "but is no longer in the live schema. This is a breaking "
                "change for any downstream consumer that reads it."
            ),
            context={**base_ctx, "column_name": col},
            evidence={"column_name": col,
                      "prior_data_type": prior.get("data_type"),
                      "prior_is_nullable": prior.get("is_nullable")},
        ))

    # ── Changed (type + nullability) ─────────────────────────────────────────
    common = set(prior_snapshot) & set(live_by_name)
    for col in common:
        prior = prior_snapshot[col]
        live = live_by_name[col]
        prior_type = _normalise_type(prior.get("data_type"))
        live_type = _normalise_type(live.get("data_type"))
        if prior_type and live_type and prior_type != live_type:
            instance = _ensure_per_table_drift_instance("column_type_changed", db, sch, tbl)
            _, _, sev = DRIFT_RULES["column_type_changed"]
            findings.append(_finding(
                asset_id=table_asset.id, scan_id=scan_id,
                instance_id=instance.id, severity=sev,
                handler_key="column_type_changed",
                title=f"Column {col} type changed: {prior_type} → {live_type}",
                description=(
                    f"Column '{col}' in {fqn} changed data type from "
                    f"{prior.get('data_type')} to {live.get('data_type')} "
                    "since the previous scan. Verify downstream casts, joins, "
                    "and stored aggregates."
                ),
                context={**base_ctx, "column_name": col},
                evidence={"column_name": col,
                          "prior_data_type": prior.get("data_type"),
                          "new_data_type": live.get("data_type")},
            ))

        prior_null = _normalise_nullable(prior.get("is_nullable"))
        live_null = _normalise_nullable(live.get("is_nullable"))
        if prior_null is not None and live_null is not None and prior_null != live_null:
            instance = _ensure_per_table_drift_instance(
                "column_nullability_changed", db, sch, tbl)
            _, _, sev = DRIFT_RULES["column_nullability_changed"]
            findings.append(_finding(
                asset_id=table_asset.id, scan_id=scan_id,
                instance_id=instance.id, severity=sev,
                handler_key="column_nullability_changed",
                title=(f"Column {col} nullability changed: "
                       f"{'NULLABLE' if prior_null else 'NOT NULL'} → "
                       f"{'NULLABLE' if live_null else 'NOT NULL'}"),
                description=(
                    f"Column '{col}' in {fqn} changed nullability since the "
                    "previous scan. NOT NULL → NULLABLE can introduce nulls "
                    "into consumers that assume the column is always present."
                ),
                context={**base_ctx, "column_name": col},
                evidence={"column_name": col,
                          "prior_is_nullable": prior_null,
                          "new_is_nullable": live_null},
            ))

    if findings:
        logger.info(
            f"[SchemaDrift] {fqn}: {len(findings)} drift finding(s) — "
            f"added={len(added)} removed={len(removed)}"
        )
    return findings
