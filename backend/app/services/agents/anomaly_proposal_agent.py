"""
Anomaly Proposal Agent — deterministic sweep at the end of a scan.

For every baseline on the scanned asset that has >= 14 samples, propose
a `metric_anomaly` / `metric_relative_change` / `category_disappeared`
rule IF:
  - No active RULE_INSTANCE already exists for the same (asset, column,
    metric, template_shape).
  - No prior PENDING_PROPOSALS row for the same target is already
    pending or was previously rejected (memo-style suppression).

Routing:
  - Agentic run (run.schedule_id is None): create RULE_INSTANCES rows with
    status='pending' so they surface in the existing inline approval UI.
  - Scheduled run (run.schedule_id set): create PENDING_PROPOSALS rows +
    one NOTIFICATIONS entry so the dashboard bell picks them up.

No Claude calls in Tier A — the rules are deterministic from baseline
stats. LLM-authored rationale can be layered on later.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.services import storage
from app.services import metric_snapshots
from app.services.snowflake_session import session as sf

logger = logging.getLogger(__name__)

# Default anomaly thresholds when proposing rules. These match the
# defaults hard-coded in the template SQL — carrying them on the instance
# lets a user tune per-rule without editing template code.
_DEFAULT_DEVIATIONS = 3.0
_DEFAULT_PCT_CHANGE = 25.0

# Cap per-scan proposal fan-out so a fresh table doesn't dump 60 unread
# notifications in one go on its 14th scan. First scan proposes the top
# N; the rest arrive on future scans as no new proposals stabilise.
_MAX_PROPOSALS_PER_SCAN = 20

# Which template shape / severity to propose for each metric name.
_METRIC_TO_SHAPE: Dict[str, Dict[str, Any]] = {
    "row_count":            {"shape": "metric_anomaly", "severity": "high"},
    "freshness_lag_hours":  {"shape": "metric_anomaly", "severity": "high"},
    "null_pct":             {"shape": "metric_anomaly", "severity": "medium"},
    "distinct_count":       {"shape": "metric_anomaly", "severity": "low"},
    "observed_categories":  {"shape": "category_disappeared", "severity": "medium"},
}


def _existing_active_instance(
    definition_id: str,
    database_name: str,
    schema_name: str,
    table_name: str,
    target_config: Dict[str, Any],
) -> bool:
    """True if a RULE_INSTANCES row already exists for this (definition,
    table, target_config) — regardless of status. Prevents re-proposing
    a rule the user already approved, rejected, or has pending review."""
    metric = target_config.get("metric_name") or ""
    column = target_config.get("column") or ""
    rows = sf.query(
        """
        SELECT ID, TARGET_CONFIG FROM RULE_INSTANCES
        WHERE DEFINITION_ID = %(def_id)s
          AND DATABASE_NAME = %(db)s
          AND SCHEMA_NAME   = %(sch)s
          AND TABLE_NAME    = %(tbl)s
        """,
        {"def_id": definition_id, "db": database_name,
         "sch": schema_name, "tbl": table_name},
    )
    for r in rows:
        tc = r.get("TARGET_CONFIG")
        if isinstance(tc, str):
            import json
            try:
                tc = json.loads(tc)
            except Exception:
                tc = {}
        if not isinstance(tc, dict):
            continue
        if (tc.get("metric_name") or "") == metric and (tc.get("column") or "") == column:
            return True
    return False


def _existing_pending_or_rejected(
    asset_id: str,
    template_shape: str,
    metric_name: str,
    column_name: Optional[str],
) -> bool:
    """True if PENDING_PROPOSALS already has a matching row in
    {pending, rejected} state — the memo-suppression path."""
    col_pred = "COLUMN_NAME = %(col)s" if column_name else "COLUMN_NAME IS NULL"
    params = {
        "asset": asset_id, "shape": template_shape, "metric": metric_name,
    }
    if column_name:
        params["col"] = column_name
    rows = sf.query(
        f"""
        SELECT ID, STATUS FROM PENDING_PROPOSALS
        WHERE ASSET_ID = %(asset)s
          AND TEMPLATE_SHAPE = %(shape)s
          AND METRIC_NAME = %(metric)s
          AND {col_pred}
          AND STATUS IN ('pending', 'rejected')
        LIMIT 1
        """,
        params,
    )
    return bool(rows)


def _rationale_for(baseline: Dict[str, Any]) -> str:
    metric = baseline["metric_name"]
    n = baseline["sample_count"]
    col = baseline.get("column_name")
    tag = f"column {col}" if col else "table"
    if metric == "observed_categories":
        return (
            f"{tag} has a stable set of category values across {n} recent "
            "scans — a value disappearing likely means an upstream source "
            "stopped producing rows for that segment."
        )
    median = baseline.get("median")
    mad = baseline.get("mad")
    return (
        f"{tag} '{metric}' has median={median!r} and MAD={mad!r} across the "
        f"last {n} scans — sudden deviations from that baseline are worth "
        "surfacing as findings."
    )


def _candidate_from_baseline(
    baseline: Dict[str, Any],
    table_asset: Any,
) -> Optional[Dict[str, Any]]:
    metric = baseline["metric_name"]
    mapping = _METRIC_TO_SHAPE.get(metric)
    if not mapping:
        return None
    shape = mapping["shape"]
    severity = mapping["severity"]
    column = baseline.get("column_name")

    definition = storage.get_definition_by_template_shape(shape)
    if not definition:
        logger.warning(f"[AnomalyProposal] no definition for shape {shape}")
        return None

    if shape == "category_disappeared":
        target_config = {
            "asset_id": table_asset.id,
            "column": column,
        }
        threshold_config = {}
    else:
        target_config = {
            "asset_id": table_asset.id,
            "metric_name": metric,
        }
        if column:
            target_config["column"] = column
        threshold_config = {"deviations": _DEFAULT_DEVIATIONS}

    # Dedup by (definition, table, target_config) at the RULE_INSTANCES
    # layer AND at the PENDING_PROPOSALS layer.
    if _existing_active_instance(
        definition.id, table_asset.database_name, table_asset.schema_name,
        table_asset.table_name, target_config,
    ):
        return None
    if _existing_pending_or_rejected(
        table_asset.id, shape, metric, column,
    ):
        return None

    return {
        "definition_id": definition.id,
        "template_shape": shape,
        "metric_name": metric,
        "column_name": column,
        "severity": severity,
        "target_config": target_config,
        "threshold_config": threshold_config,
        "rationale": _rationale_for(baseline),
    }


def _fingerprint(definition_id: str, asset_id: str, target_config: Dict[str, Any]) -> str:
    import json
    payload = json.dumps({"d": definition_id, "a": asset_id, "t": target_config},
                         sort_keys=True)
    return storage._sha256(f"anomaly|{payload}")


def _rendered_sql(shape: str, target_config: Dict[str, Any],
                  threshold_config: Dict[str, Any],
                  table_asset: Any) -> Optional[str]:
    try:
        from app.services import rule_sql_templates
        return rule_sql_templates.render_template(
            shape,
            table_asset.database_name, table_asset.schema_name, table_asset.table_name,
            target_config, threshold_config,
        )
    except Exception as e:
        logger.warning(f"[AnomalyProposal] render_template failed: {e}")
        return None


def run_for_scan(run: Any, table_asset: Any, scan_id: str) -> Dict[str, Any]:
    """Entrypoint called by the coordinator after findings finalize.

    Returns a small summary dict for logging.
    """
    if table_asset is None:
        return {"proposed": 0, "skipped": 0, "reason": "no_table_asset"}

    baselines = metric_snapshots.list_ready_baselines(table_asset.id)
    if not baselines:
        return {"proposed": 0, "skipped": 0, "reason": "no_mature_baselines"}

    candidates: List[Dict[str, Any]] = []
    for b in baselines:
        c = _candidate_from_baseline(b, table_asset)
        if c is not None:
            candidates.append(c)
    if not candidates:
        return {"proposed": 0, "skipped": len(baselines), "reason": "all_covered"}

    # Cap fan-out. Prefer table-level metrics + user-facing ones first.
    def _priority(c):
        pri = 0
        if c["metric_name"] == "row_count": pri -= 3
        if c["metric_name"] == "freshness_lag_hours": pri -= 2
        if c["template_shape"] == "category_disappeared": pri -= 1
        return pri
    candidates.sort(key=_priority)
    capped = candidates[:_MAX_PROPOSALS_PER_SCAN]

    is_scheduled = bool(getattr(run, "schedule_id", None))
    proposed = 0
    for c in capped:
        try:
            if is_scheduled:
                _emit_pending_proposal(run, table_asset, scan_id, c)
            else:
                _emit_inline_pending_instance(run, table_asset, c)
            proposed += 1
        except Exception as e:
            logger.warning(
                f"[AnomalyProposal] emit failed for {c['template_shape']}/"
                f"{c['metric_name']}/{c['column_name']}: {e}"
            )

    if is_scheduled and proposed > 0:
        try:
            _create_notification(run, table_asset, proposed)
        except Exception as e:
            logger.warning(f"[AnomalyProposal] notification create failed: {e}")

    return {"proposed": proposed, "candidates": len(candidates),
            "scheduled": is_scheduled}


def _emit_inline_pending_instance(run: Any, table_asset: Any, c: Dict[str, Any]) -> None:
    """Agentic run — create a RULE_INSTANCES row with status='pending' so
    the standard inline approval UI picks it up alongside RuleIntelligence
    proposals."""
    rule_sql = _rendered_sql(
        c["template_shape"], c["target_config"], c["threshold_config"], table_asset,
    )
    fingerprint = _fingerprint(c["definition_id"], table_asset.id, c["target_config"])
    scope = "column" if c["column_name"] else "table"
    storage.create_instance(
        definition_id=c["definition_id"],
        scope=scope,
        database_name=table_asset.database_name,
        schema_name=table_asset.schema_name,
        table_name=table_asset.table_name,
        fingerprint=fingerprint,
        severity=c["severity"],
        target_config=c["target_config"],
        threshold_config=c["threshold_config"],
        rule_sql=rule_sql,
        rationale=c["rationale"],
        status="pending",
        is_active=False,
        created_by="anomaly_proposal_agent",
        source_run_id=run.id,
    )


def _emit_pending_proposal(run: Any, table_asset: Any, scan_id: str,
                            c: Dict[str, Any]) -> None:
    """Scheduled run — write to PENDING_PROPOSALS. Approval later
    materialises the RULE_INSTANCE."""
    import json
    proposal_id = storage._new_id()
    sf.execute(
        """
        INSERT INTO PENDING_PROPOSALS
            (ID, KIND, ASSET_ID, DATABASE_NAME, SCHEMA_NAME, TABLE_NAME,
             COLUMN_NAME, TEMPLATE_SHAPE, METRIC_NAME,
             TARGET_CONFIG, THRESHOLD_CONFIG, SEVERITY, RATIONALE,
             STATUS, SOURCE_RUN_ID, SOURCE_SCAN_ID, SCHEDULE_ID)
        SELECT
            %(id)s, %(kind)s, %(asset)s, %(db)s, %(sch)s, %(tbl)s,
            %(col)s, %(shape)s, %(metric)s,
            PARSE_JSON(%(tc)s), PARSE_JSON(%(thc)s), %(sev)s, %(rat)s,
            'pending', %(run)s, %(scan)s, %(sched)s
        """,
        {
            "id": proposal_id,
            "kind": "anomaly_rule",
            "asset": table_asset.id,
            "db": table_asset.database_name,
            "sch": table_asset.schema_name,
            "tbl": table_asset.table_name,
            "col": c["column_name"],
            "shape": c["template_shape"],
            "metric": c["metric_name"],
            "tc": json.dumps(c["target_config"]),
            "thc": json.dumps(c["threshold_config"]),
            "sev": c["severity"],
            "rat": c["rationale"],
            "run": run.id,
            "scan": scan_id,
            "sched": getattr(run, "schedule_id", None),
        },
    )


def _create_notification(run: Any, table_asset: Any, count: int) -> None:
    notification_id = storage._new_id()
    fqn = f"{table_asset.database_name}.{table_asset.schema_name}.{table_asset.table_name}"
    title = f"{count} new anomaly rule{'s' if count > 1 else ''} proposed for {fqn}"
    body = (
        "Scheduled scan detected mature baselines and proposed anomaly-detection "
        "rules. Review and approve to start monitoring."
    )
    sf.execute(
        """
        INSERT INTO NOTIFICATIONS
            (ID, KIND, TITLE, BODY, REF_TABLE, REF_ID, SEVERITY)
        VALUES
            (%(id)s, 'anomaly_proposals', %(title)s, %(body)s,
             'PENDING_PROPOSALS', %(ref_id)s, 'info')
        """,
        {
            "id": notification_id,
            "title": title,
            "body": body,
            "ref_id": run.id,
        },
    )
