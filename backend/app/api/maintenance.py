"""Maintenance proposals API.

MaintenanceAgent generates proposals (pause / flag flapping / retire
superseded) that land in MAINTENANCE_PROPOSALS. This router exposes
list / approve / dismiss / run-sweep actions.

Approval maps action → concrete instance mutation:
  - retire_candidate → instance status='paused', is_active=False
  - flapping         → instance status='paused', is_active=False
  - superseded       → instance status='retired', is_active=False
  - obsolete_target  → instance status='retired', is_active=False
"""
from __future__ import annotations

import logging
from typing import Optional, Any, Dict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services import storage
from app.services.agents import maintenance_agent

logger = logging.getLogger(__name__)

router = APIRouter()


class MaintenanceProposalOut(BaseModel):
    id: str
    instance_id: str
    action: str
    reason: Optional[str] = None
    evidence: Optional[Dict[str, Any]] = None
    status: str
    decision_reason: Optional[str] = None
    decided_by: Optional[str] = None
    decided_at: Optional[str] = None
    created_at: Optional[str] = None
    # Denormalised for UI convenience
    instance_summary: Optional[Dict[str, Any]] = None


def _instance_summary(instance_id: str) -> Optional[Dict[str, Any]]:
    inst = storage.get_instance(instance_id)
    if inst is None:
        return None
    defn = storage.get_definition(inst.definition_id)
    return {
        "id": inst.id,
        "database_name": inst.database_name,
        "schema_name": inst.schema_name,
        "table_name": inst.table_name,
        "severity": inst.severity,
        "status": inst.status,
        "definition_name": getattr(defn, "name", None),
        "definition_id": inst.definition_id,
    }


def _to_out(
    p: Any,
    instances: Optional[Dict[str, Any]] = None,
    definitions: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if instances is not None:
        inst = instances.get(p.instance_id)
        if inst is not None:
            defn = (definitions or {}).get(inst.definition_id)
            summary: Optional[Dict[str, Any]] = {
                "id": inst.id,
                "database_name": inst.database_name,
                "schema_name": inst.schema_name,
                "table_name": inst.table_name,
                "severity": inst.severity,
                "status": inst.status,
                "definition_name": getattr(defn, "name", None),
                "definition_id": inst.definition_id,
            }
        else:
            summary = None
    else:
        summary = _instance_summary(p.instance_id)
    return MaintenanceProposalOut(
        id=p.id, instance_id=p.instance_id, action=p.action,
        reason=p.reason, evidence=p.evidence, status=p.status,
        decision_reason=p.decision_reason, decided_by=p.decided_by,
        decided_at=str(p.decided_at) if p.decided_at else None,
        created_at=str(p.created_at) if p.created_at else None,
        instance_summary=summary,
    ).model_dump()


@router.get("/pending")
def list_pending(limit: int = 200):
    proposals = storage.list_maintenance_proposals(status="pending", limit=limit)
    if not proposals:
        return {"items": []}
    instance_ids = list({p.instance_id for p in proposals})
    instances = storage.get_instances_by_ids(instance_ids)
    definition_ids = list({inst.definition_id for inst in instances.values() if inst.definition_id})
    definitions = storage.get_definitions_by_ids(definition_ids) if definition_ids else {}
    return {"items": [_to_out(p, instances, definitions) for p in proposals]}


@router.get("/{proposal_id}")
def get_proposal(proposal_id: str):
    p = storage.get_maintenance_proposal(proposal_id)
    if p is None:
        raise HTTPException(404, f"Proposal {proposal_id} not found")
    return _to_out(p)


class ApproveIn(BaseModel):
    decided_by: Optional[str] = None


_ACTION_TO_INSTANCE_STATUS = {
    "retire_candidate": ("paused", False),
    "flapping":         ("paused", False),
    "superseded":       ("retired", False),
    "obsolete_target":  ("retired", False),
}


@router.post("/{proposal_id}/approve")
def approve(proposal_id: str, body: ApproveIn):
    p = storage.get_maintenance_proposal(proposal_id)
    if p is None:
        raise HTTPException(404, f"Proposal {proposal_id} not found")
    if p.status != "pending":
        raise HTTPException(409, f"Proposal already {p.status}")

    mapping = _ACTION_TO_INSTANCE_STATUS.get(p.action)
    if mapping is None:
        raise HTTPException(400, f"Unknown maintenance action: {p.action}")
    new_status, new_active = mapping

    inst = storage.get_instance(p.instance_id)
    if inst is not None:
        storage.update_instance(
            p.instance_id, status=new_status, is_active=new_active,
        )
        # Retiring (not pausing) an anomaly instance should un-enroll the
        # underlying metric so we stop capturing snapshots for it. Pausing
        # is temporary — keep the enrollment so history keeps flowing.
        if new_status == "retired":
            _maybe_unenroll_instance_metric(inst)
    storage.decide_maintenance_proposal(
        proposal_id, status="approved", decided_by=body.decided_by,
    )
    return {"ok": True, "instance_id": p.instance_id, "new_status": new_status}


def _maybe_unenroll_instance_metric(inst: Any) -> None:
    """If this rule instance is anomaly-shaped (target_config carries asset_id
    + metric_name), un-enroll the corresponding MONITORED_METRICS row unless a
    user enrolled it manually."""
    try:
        tc = inst.target_config or {}
        asset_id = tc.get("asset_id")
        metric = tc.get("metric_name")
        col = tc.get("column")
        if not (asset_id and metric):
            return
        from app.services.snowflake_session import session as sf
        rows = sf.query(
            """
            SELECT ENROLLMENT_SOURCE FROM MONITORED_METRICS
            WHERE ASSET_ID = %(a)s AND METRIC_NAME = %(m)s
              AND (
                (COLUMN_NAME IS NULL AND %(c)s IS NULL)
                OR COLUMN_NAME = %(c)s
              )
            """,
            {"a": asset_id, "m": metric, "c": col},
        )
        if not rows:
            return
        source = rows[0].get("ENROLLMENT_SOURCE")
        if source in ("user", "auto_table"):
            return
        storage.unenroll_metric(asset_id, col, metric)
    except Exception as e:
        logger.debug(f"[maintenance] unenroll-on-retire failed: {e}")


class DismissIn(BaseModel):
    reason: Optional[str] = None
    decided_by: Optional[str] = None


@router.post("/{proposal_id}/dismiss")
def dismiss(proposal_id: str, body: DismissIn):
    p = storage.get_maintenance_proposal(proposal_id)
    if p is None:
        raise HTTPException(404, f"Proposal {proposal_id} not found")
    if p.status != "pending":
        raise HTTPException(409, f"Proposal already {p.status}")
    storage.decide_maintenance_proposal(
        proposal_id, status="dismissed",
        decided_by=body.decided_by, reason=body.reason,
    )
    return {"ok": True}


@router.post("/run")
def run_sweep():
    """Kick off a MaintenanceAgent sweep. Synchronous; fine for weekly
    cadence and a few thousand instances."""
    try:
        result = maintenance_agent.run()
    except Exception as e:
        logger.exception("MaintenanceAgent run failed")
        raise HTTPException(500, f"Maintenance sweep failed: {e}")
    return result
