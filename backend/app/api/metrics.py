"""Metric-detail API — powers the anomaly-monitoring UI.

Reads the same METRIC_SNAPSHOTS / METRIC_BASELINES that AnomalyProposalAgent
uses. Nothing here computes new metrics; it just projects the substrate the
scan pipeline already populates so the frontend can chart it.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services import storage
from app.services.snowflake_session import session as sf

logger = logging.getLogger(__name__)

router = APIRouter()


def _serialize(v: Any) -> Any:
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


def _fetch_instance_for_metric(
    asset: Any, metric_name: str, column_name: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Find the RULE_INSTANCE (if any) implementing anomaly detection for this
    (asset, metric, column). Returned instance drives the threshold-editor UI.
    Prefers the active instance; falls back to the newest.

    Fetches all instances on the table and filters target_config in Python —
    matches the pattern anomaly_proposal_agent.py uses to avoid depending on
    Snowflake VARIANT-path syntax."""
    rows = sf.query(
        """
        SELECT ID, DEFINITION_ID, THRESHOLD_CONFIG, TARGET_CONFIG, IS_ACTIVE, STATUS,
               SEVERITY, CREATED_AT
        FROM RULE_INSTANCES
        WHERE DATABASE_NAME = %(db)s
          AND SCHEMA_NAME   = %(sch)s
          AND TABLE_NAME    = %(tbl)s
        """,
        {"db": asset.database_name, "sch": asset.schema_name, "tbl": asset.table_name},
    )
    matches: List[Dict[str, Any]] = []
    for r in rows:
        tc = r.get("TARGET_CONFIG")
        if isinstance(tc, str):
            try:
                tc = json.loads(tc)
            except Exception:
                tc = {}
        if not isinstance(tc, dict):
            continue
        if tc.get("asset_id") != asset.id:
            continue
        if (tc.get("metric_name") or "") != metric_name:
            continue
        if (tc.get("column") or None) != (column_name or None):
            continue
        thc = r.get("THRESHOLD_CONFIG")
        if isinstance(thc, str):
            try:
                thc = json.loads(thc)
            except Exception:
                thc = {}
        matches.append({
            "id": r["ID"],
            "definition_id": r["DEFINITION_ID"],
            "threshold_config": thc or {},
            "is_active": bool(r.get("IS_ACTIVE")),
            "status": r.get("STATUS"),
            "severity": r.get("SEVERITY"),
            "created_at": r.get("CREATED_AT"),
        })
    if not matches:
        return None
    matches.sort(key=lambda m: (0 if m["is_active"] else 1, -(m["created_at"].timestamp() if m["created_at"] else 0)))
    winner = matches[0]
    winner.pop("created_at", None)
    return winner


@router.get("/history")
def metric_history(
    asset_id: str,
    metric_name: str,
    column_name: Optional[str] = None,
    limit: int = 90,
):
    """Time-series for one (asset, metric, column). Returns snapshots + baseline
    + any findings that fired against the same target so the chart can annotate
    breach points."""
    asset = storage.get_asset(asset_id)
    if asset is None:
        raise HTTPException(404, f"asset {asset_id} not found")

    col_pred = "COLUMN_NAME = %(col)s" if column_name else "COLUMN_NAME IS NULL"
    params: Dict[str, Any] = {"asset": asset_id, "metric": metric_name, "limit": max(1, min(limit, 500))}
    if column_name:
        params["col"] = column_name

    snap_rows = sf.query(
        f"""
        SELECT SCAN_ID, METRIC_VALUE, METRIC_META, CAPTURED_AT
        FROM METRIC_SNAPSHOTS
        WHERE ASSET_ID = %(asset)s
          AND METRIC_NAME = %(metric)s
          AND {col_pred}
        ORDER BY CAPTURED_AT DESC
        LIMIT %(limit)s
        """,
        params,
    )
    # Return in chronological order (oldest → newest) so the chart plots left-to-right.
    snapshots = [
        {
            "scan_id": r["SCAN_ID"],
            "value": r.get("METRIC_VALUE"),
            "captured_at": _serialize(r["CAPTURED_AT"]),
        }
        for r in reversed(snap_rows)
    ]

    baseline_rows = sf.query(
        f"""
        SELECT MEDIAN_VALUE, MAD_VALUE, SAMPLE_COUNT, OBSERVED_SET,
               WINDOW_START, WINDOW_END, UPDATED_AT
        FROM METRIC_BASELINES
        WHERE ASSET_ID = %(asset)s
          AND METRIC_NAME = %(metric)s
          AND {col_pred}
        ORDER BY UPDATED_AT DESC LIMIT 1
        """,
        {k: v for k, v in params.items() if k != "limit"},
    )
    baseline = None
    if baseline_rows:
        b = baseline_rows[0]
        observed = b.get("OBSERVED_SET")
        if isinstance(observed, str):
            try:
                observed = json.loads(observed)
            except Exception:
                observed = None
        baseline = {
            "median": b.get("MEDIAN_VALUE"),
            "mad": b.get("MAD_VALUE"),
            "sample_count": int(b.get("SAMPLE_COUNT") or 0),
            "observed_set": observed,
            "window_start": _serialize(b.get("WINDOW_START")),
            "window_end": _serialize(b.get("WINDOW_END")),
            "updated_at": _serialize(b.get("UPDATED_AT")),
        }

    instance = _fetch_instance_for_metric(asset, metric_name, column_name)

    # Findings for the same instance — annotate the chart with breach markers.
    findings: List[Dict[str, Any]] = []
    if instance:
        rows = sf.query(
            """
            SELECT ID, SCAN_ID, TITLE, SEVERITY, STATUS, DETECTED_AT, LAST_SEEN_AT
            FROM FINDINGS
            WHERE INSTANCE_ID = %(iid)s
            ORDER BY DETECTED_AT DESC
            LIMIT 100
            """,
            {"iid": instance["id"]},
        )
        for r in rows:
            findings.append({
                "id": r["ID"],
                "scan_id": r.get("SCAN_ID"),
                "title": r.get("TITLE"),
                "severity": r.get("SEVERITY"),
                "status": r.get("STATUS"),
                "detected_at": _serialize(r.get("DETECTED_AT")),
                "last_seen_at": _serialize(r.get("LAST_SEEN_AT")),
            })

    return {
        "asset": {
            "id": asset.id,
            "database_name": asset.database_name,
            "schema_name": asset.schema_name,
            "table_name": asset.table_name,
        },
        "metric_name": metric_name,
        "column_name": column_name,
        "snapshots": snapshots,
        "baseline": baseline,
        "instance": instance,
        "findings": findings,
    }


@router.get("/asset/{asset_id}")
def list_asset_metrics(asset_id: str, sparkline_points: int = 20):
    """Every (column, metric) pair we have a baseline for on this asset — plus
    the latest snapshot value AND a short sparkline history so the caller can
    render mini-charts in one round-trip."""
    asset = storage.get_asset(asset_id)
    if asset is None:
        raise HTTPException(404, f"asset {asset_id} not found")

    base_rows = sf.query(
        """
        SELECT COLUMN_NAME, METRIC_NAME, MEDIAN_VALUE, MAD_VALUE, SAMPLE_COUNT,
               UPDATED_AT
        FROM METRIC_BASELINES
        WHERE ASSET_ID = %(a)s
        ORDER BY METRIC_NAME, COLUMN_NAME
        """,
        {"a": asset_id},
    )
    if not base_rows:
        return {"metrics": []}

    # Pull the last N snapshots per (metric, column) in one query using QUALIFY.
    n_points = max(2, min(sparkline_points, 60))
    hist_rows = sf.query(
        """
        SELECT COLUMN_NAME, METRIC_NAME, METRIC_VALUE, CAPTURED_AT
        FROM METRIC_SNAPSHOTS
        WHERE ASSET_ID = %(a)s
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY METRIC_NAME, COLUMN_NAME ORDER BY CAPTURED_AT DESC
        ) <= %(n)s
        ORDER BY METRIC_NAME, COLUMN_NAME, CAPTURED_AT ASC
        """,
        {"a": asset_id, "n": n_points},
    )
    hist_by_key: Dict[tuple, List[Dict[str, Any]]] = {}
    for r in hist_rows:
        key = (r.get("COLUMN_NAME"), r["METRIC_NAME"])
        hist_by_key.setdefault(key, []).append({
            "value": r.get("METRIC_VALUE"),
            "captured_at": _serialize(r.get("CAPTURED_AT")),
        })

    out = []
    for b in base_rows:
        col = b.get("COLUMN_NAME")
        metric = b["METRIC_NAME"]
        history = hist_by_key.get((col, metric), [])
        latest = history[-1] if history else {}
        # Compute deviation-from-median for the color/status pill.
        median = b.get("MEDIAN_VALUE")
        mad = b.get("MAD_VALUE")
        latest_v = latest.get("value") if latest else None
        deviations_from_median = None
        if latest_v is not None and median is not None and mad and mad > 0:
            deviations_from_median = abs(float(latest_v) - float(median)) / float(mad)
        out.append({
            "column_name": col,
            "metric_name": metric,
            "median": median,
            "mad": mad,
            "sample_count": int(b.get("SAMPLE_COUNT") or 0),
            "latest_value": latest_v,
            "latest_captured_at": latest.get("captured_at") if latest else None,
            "deviations_from_median": deviations_from_median,
            "history": history,
            "baseline_updated_at": _serialize(b.get("UPDATED_AT")),
        })
    return {"metrics": out}


class ThresholdIn(BaseModel):
    deviations: Optional[float] = None
    max_pct_change: Optional[float] = None


@router.patch("/instance/{instance_id}/threshold")
def update_threshold(instance_id: str, body: ThresholdIn):
    """Merge new threshold values into RULE_INSTANCES.threshold_config. Only
    the two anomaly knobs are exposed — anything else stays untouched."""
    inst = storage.get_instance(instance_id)
    if inst is None:
        raise HTTPException(404, f"instance {instance_id} not found")

    current = inst.threshold_config or {}
    updates: Dict[str, Any] = {}
    if body.deviations is not None:
        if body.deviations <= 0 or body.deviations > 10:
            raise HTTPException(400, "deviations must be between 0 and 10")
        updates["deviations"] = float(body.deviations)
    if body.max_pct_change is not None:
        if body.max_pct_change <= 0 or body.max_pct_change > 1000:
            raise HTTPException(400, "max_pct_change must be between 0 and 1000")
        updates["max_pct_change"] = float(body.max_pct_change)
    if not updates:
        raise HTTPException(400, "no threshold fields supplied")

    merged = {**current, **updates}

    # Re-render the rule SQL so the change takes effect on the next scan.
    definition = storage.get_definition(inst.definition_id)
    new_sql = inst.rule_sql
    if definition and getattr(definition, "template_shape", None):
        try:
            from app.services import rule_sql_templates
            new_sql = rule_sql_templates.render_template(
                definition.template_shape,
                inst.database_name, inst.schema_name, inst.table_name,
                inst.target_config or {}, merged,
            )
        except Exception as e:
            logger.warning(f"[metrics] re-render failed for instance {instance_id}: {e}")

    storage.update_instance(
        instance_id,
        threshold_config=merged,
        rule_sql=new_sql,
    )
    return {"ok": True, "threshold_config": merged}
