"""Notifications inbox API.

Dashboard-level bell icon reads /notifications for unread count +
recent items. Anomaly Tier A only emits kind='anomaly_proposals'
today — the same inbox will absorb future event types (drift alerts,
incident summaries) once wired.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.snowflake_session import session as sf

logger = logging.getLogger(__name__)

router = APIRouter()


class NotificationOut(BaseModel):
    id: str
    kind: str
    title: str
    body: Optional[str] = None
    ref_table: Optional[str] = None
    ref_id: Optional[str] = None
    severity: Optional[str] = None
    read_at: Optional[str] = None
    created_at: Optional[str] = None


def _row_to_out(r: dict) -> NotificationOut:
    return NotificationOut(
        id=r["ID"],
        kind=r["KIND"],
        title=r["TITLE"],
        body=r.get("BODY"),
        ref_table=r.get("REF_TABLE"),
        ref_id=r.get("REF_ID"),
        severity=r.get("SEVERITY"),
        read_at=str(r["READ_AT"]) if r.get("READ_AT") else None,
        created_at=str(r["CREATED_AT"]) if r.get("CREATED_AT") else None,
    )


@router.get("")
def list_notifications(unread_only: bool = False, limit: int = 50):
    where = "WHERE READ_AT IS NULL" if unread_only else ""
    rows = sf.query(
        f"""
        SELECT * FROM NOTIFICATIONS {where}
        ORDER BY CREATED_AT DESC
        LIMIT %(limit)s
        """,
        {"limit": max(1, min(limit, 200))},
    )
    return {"items": [_row_to_out(r).model_dump() for r in rows]}


@router.get("/unread-count")
def unread_count():
    rows = sf.query(
        "SELECT COUNT(*) AS N FROM NOTIFICATIONS WHERE READ_AT IS NULL",
        {},
    )
    n = int((rows[0].get("N") if rows else 0) or 0)
    return {"unread": n}


@router.post("/{notification_id}/read")
def mark_read(notification_id: str):
    sf.execute(
        "UPDATE NOTIFICATIONS SET READ_AT = CURRENT_TIMESTAMP() WHERE ID = %(id)s AND READ_AT IS NULL",
        {"id": notification_id},
    )
    return {"ok": True}


@router.post("/read-all")
def mark_all_read():
    sf.execute(
        "UPDATE NOTIFICATIONS SET READ_AT = CURRENT_TIMESTAMP() WHERE READ_AT IS NULL",
        {},
    )
    return {"ok": True}


# ── Diagnostics for anomaly Tier A ─────────────────────────────────────
# Temporary read-only endpoints exposing the metric substrate so end-to-end
# validation doesn't need Snowflake console access. Keep read-only.

@router.get("/_diag/metric-snapshots")
def diag_snapshot_counts(asset_id: Optional[str] = None, limit: int = 50):
    if asset_id:
        rows = sf.query(
            """
            SELECT METRIC_NAME, COLUMN_NAME, COUNT(*) AS N,
                   MIN(CAPTURED_AT) AS FIRST, MAX(CAPTURED_AT) AS LAST
            FROM METRIC_SNAPSHOTS
            WHERE ASSET_ID = %(a)s
            GROUP BY METRIC_NAME, COLUMN_NAME
            ORDER BY METRIC_NAME, COLUMN_NAME
            """,
            {"a": asset_id},
        )
    else:
        rows = sf.query(
            """
            SELECT ASSET_ID, DATABASE_NAME, SCHEMA_NAME, TABLE_NAME,
                   COUNT(*) AS N
            FROM METRIC_SNAPSHOTS
            GROUP BY ASSET_ID, DATABASE_NAME, SCHEMA_NAME, TABLE_NAME
            ORDER BY N DESC
            LIMIT %(l)s
            """,
            {"l": max(1, min(limit, 500))},
        )
    return {"rows": [{k: (str(v) if hasattr(v, 'isoformat') else v) for k, v in r.items()} for r in rows]}


@router.post("/_diag/run-anomaly-agent")
def diag_run_anomaly_agent(asset_id: str, simulate_scheduled: bool = False):
    """DIAG-ONLY: manually invoke AnomalyProposalAgent against a table
    asset without waiting for a full scan cycle. simulate_scheduled=True
    routes proposals into PENDING_PROPOSALS + notifications; False creates
    inline pending RULE_INSTANCES."""
    from app.services import storage
    from app.services.agents.anomaly_proposal_agent import run_for_scan
    from types import SimpleNamespace
    asset = storage.get_asset(asset_id)
    if not asset:
        return {"error": f"asset {asset_id} not found"}
    fake_run = SimpleNamespace(
        id="diag-run", schedule_id=("diag-sched" if simulate_scheduled else None),
    )
    # Add trace: baseline set + dedup results
    from app.services import metric_snapshots
    from app.services.agents import anomaly_proposal_agent as apa
    from app.services import storage as _st
    baselines = metric_snapshots.list_ready_baselines(asset_id)
    trace = []
    for b in baselines:
        m = b["metric_name"]
        col = b.get("column_name")
        mapping = apa._METRIC_TO_SHAPE.get(m)
        if not mapping:
            trace.append({"metric": m, "col": col, "skip": "no_mapping"})
            continue
        shape = mapping["shape"]
        definition = _st.get_definition_by_template_shape(shape)
        if not definition:
            trace.append({"metric": m, "col": col, "skip": "no_definition", "shape": shape})
            continue
        if shape == "category_disappeared":
            tc = {"asset_id": asset.id, "column": col}
        else:
            tc = {"asset_id": asset.id, "metric_name": m}
            if col: tc["column"] = col
        active = apa._existing_active_instance(
            definition.id, asset.database_name, asset.schema_name, asset.table_name, tc,
        )
        pending = apa._existing_pending_or_rejected(asset.id, shape, m, col)
        trace.append({"metric": m, "col": col, "shape": shape,
                      "existing_active": active, "existing_pending_or_rejected": pending})
    summary = run_for_scan(fake_run, asset, "diag-scan")
    return {"summary": summary, "baselines": len(baselines), "trace": trace}


@router.post("/_diag/seed-snapshots")
def diag_seed_snapshots(asset_id: str, metric_name: str, column_name: Optional[str] = None,
                        n: int = 14, value: float = 100.0):
    """DIAG-ONLY: fast-forward baseline maturity by inserting N synthetic
    historical snapshots for one (asset, column, metric). Refreshes the
    baseline afterwards. Returns the refreshed baseline row."""
    import uuid
    from app.services import metric_snapshots as _ms
    inserted = 0
    for i in range(max(1, min(n, 60))):
        sf.execute(
            """
            INSERT INTO METRIC_SNAPSHOTS
                (ID, SCAN_ID, ASSET_ID, DATABASE_NAME, SCHEMA_NAME, TABLE_NAME,
                 COLUMN_NAME, METRIC_NAME, METRIC_VALUE, CAPTURED_AT)
            SELECT %(id)s, 'diag-seed', %(a)s, NULL, NULL, NULL,
                   %(col)s, %(m)s, %(v)s,
                   DATEADD(day, -%(offset)s, CURRENT_TIMESTAMP())
            """,
            {"id": str(uuid.uuid4()), "a": asset_id, "col": column_name,
             "m": metric_name, "v": value + (i % 3), "offset": i},
        )
        inserted += 1
    _ms.refresh_baseline(asset_id, column_name, metric_name)
    baseline = _ms.get_baseline(asset_id, column_name, metric_name)
    return {"inserted": inserted, "baseline": baseline}


@router.get("/_diag/metric-baselines")
def diag_baseline_counts(asset_id: Optional[str] = None, limit: int = 100):
    where = "WHERE ASSET_ID = %(a)s" if asset_id else ""
    params: dict = {}
    if asset_id:
        params["a"] = asset_id
    params["l"] = max(1, min(limit, 500))
    rows = sf.query(
        f"""
        SELECT ASSET_ID, COLUMN_NAME, METRIC_NAME, MEDIAN_VALUE, MAD_VALUE,
               SAMPLE_COUNT, UPDATED_AT
        FROM METRIC_BASELINES
        {where}
        ORDER BY UPDATED_AT DESC
        LIMIT %(l)s
        """,
        params,
    )
    return {"rows": [{k: (str(v) if hasattr(v, 'isoformat') else v) for k, v in r.items()} for r in rows]}
