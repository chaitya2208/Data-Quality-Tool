"""Pending anomaly-rule proposals API.

Proposals originate from AnomalyProposalAgent on scheduled runs and land
in PENDING_PROPOSALS with STATUS='pending'. This router exposes list /
approve / reject actions. Approval materialises a RULE_INSTANCES row and
flips the proposal to 'approved'; rejection captures a reason (used later
by the memo path to suppress re-proposal).
"""
from __future__ import annotations

import json
import logging
from typing import Optional, Dict, Any, List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services import storage
from app.services.snowflake_session import session as sf

logger = logging.getLogger(__name__)

router = APIRouter()


class ProposalOut(BaseModel):
    id: str
    kind: str
    asset_id: Optional[str] = None
    database_name: Optional[str] = None
    schema_name: Optional[str] = None
    table_name: Optional[str] = None
    column_name: Optional[str] = None
    template_shape: Optional[str] = None
    metric_name: Optional[str] = None
    target_config: Optional[Dict[str, Any]] = None
    threshold_config: Optional[Dict[str, Any]] = None
    severity: Optional[str] = None
    rationale: Optional[str] = None
    status: str
    source_run_id: Optional[str] = None
    source_scan_id: Optional[str] = None
    schedule_id: Optional[str] = None
    decision_reason: Optional[str] = None
    decided_by: Optional[str] = None
    decided_at: Optional[str] = None
    created_at: Optional[str] = None
    instance_id: Optional[str] = None


def _parse_json(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return None


def _row_to_out(r: dict) -> ProposalOut:
    return ProposalOut(
        id=r["ID"], kind=r["KIND"],
        asset_id=r.get("ASSET_ID"),
        database_name=r.get("DATABASE_NAME"),
        schema_name=r.get("SCHEMA_NAME"),
        table_name=r.get("TABLE_NAME"),
        column_name=r.get("COLUMN_NAME"),
        template_shape=r.get("TEMPLATE_SHAPE"),
        metric_name=r.get("METRIC_NAME"),
        target_config=_parse_json(r.get("TARGET_CONFIG")),
        threshold_config=_parse_json(r.get("THRESHOLD_CONFIG")),
        severity=r.get("SEVERITY"),
        rationale=r.get("RATIONALE"),
        status=r["STATUS"],
        source_run_id=r.get("SOURCE_RUN_ID"),
        source_scan_id=r.get("SOURCE_SCAN_ID"),
        schedule_id=r.get("SCHEDULE_ID"),
        decision_reason=r.get("DECISION_REASON"),
        decided_by=r.get("DECIDED_BY"),
        decided_at=str(r["DECIDED_AT"]) if r.get("DECIDED_AT") else None,
        created_at=str(r["CREATED_AT"]) if r.get("CREATED_AT") else None,
        instance_id=r.get("INSTANCE_ID"),
    )


def _get_proposal(proposal_id: str) -> Optional[dict]:
    rows = sf.query(
        "SELECT * FROM PENDING_PROPOSALS WHERE ID = %(id)s",
        {"id": proposal_id},
    )
    return rows[0] if rows else None


@router.get("/pending")
def list_pending(limit: int = 100):
    rows = sf.query(
        """
        SELECT * FROM PENDING_PROPOSALS
        WHERE STATUS = 'pending'
        ORDER BY CREATED_AT DESC
        LIMIT %(limit)s
        """,
        {"limit": max(1, min(limit, 500))},
    )
    return {"items": [_row_to_out(r).model_dump() for r in rows]}


@router.get("/{proposal_id}")
def get_proposal(proposal_id: str):
    row = _get_proposal(proposal_id)
    if not row:
        raise HTTPException(404, f"Proposal {proposal_id} not found")
    return _row_to_out(row).model_dump()


class ApproveIn(BaseModel):
    decided_by: Optional[str] = None


@router.post("/{proposal_id}/approve")
def approve_proposal(proposal_id: str, body: ApproveIn):
    row = _get_proposal(proposal_id)
    if not row:
        raise HTTPException(404, f"Proposal {proposal_id} not found")
    if row["STATUS"] != "pending":
        raise HTTPException(409, f"Proposal already {row['STATUS']}")

    target_config = _parse_json(row.get("TARGET_CONFIG")) or {}
    threshold_config = _parse_json(row.get("THRESHOLD_CONFIG")) or {}
    template_shape = row.get("TEMPLATE_SHAPE")
    if not template_shape:
        raise HTTPException(400, "Proposal is missing template_shape")

    definition = storage.get_definition_by_template_shape(template_shape)
    if not definition:
        raise HTTPException(400, f"No rule definition for shape {template_shape}")

    table_asset = None
    if row.get("ASSET_ID"):
        table_asset = storage.get_asset(row["ASSET_ID"])
    if table_asset is None:
        raise HTTPException(400, "Asset referenced by proposal not found")

    try:
        from app.services import rule_sql_templates
        rule_sql = rule_sql_templates.render_template(
            template_shape,
            table_asset.database_name, table_asset.schema_name, table_asset.table_name,
            target_config, threshold_config,
        )
    except Exception as e:
        raise HTTPException(400, f"Could not render rule SQL: {e}")

    scope = "column" if row.get("COLUMN_NAME") else "table"
    fingerprint = storage._sha256(
        f"anomaly|{definition.id}|{table_asset.id}|{json.dumps(target_config, sort_keys=True)}"
    )
    existing = storage.get_instance_by_fingerprint(fingerprint)
    if existing:
        instance = existing
    else:
        instance = storage.create_instance(
            definition_id=definition.id,
            scope=scope,
            database_name=table_asset.database_name,
            schema_name=table_asset.schema_name,
            table_name=table_asset.table_name,
            fingerprint=fingerprint,
            severity=row.get("SEVERITY") or definition.default_severity or "medium",
            target_config=target_config,
            threshold_config=threshold_config,
            rule_sql=rule_sql,
            rationale=row.get("RATIONALE"),
            status="active",
            is_active=True,
            owner="anomaly_proposal_agent",
            created_by=body.decided_by or "user",
            source_run_id=row.get("SOURCE_RUN_ID"),
        )
        storage.approve_instance(instance.id, approved_by=body.decided_by or "user")

    sf.execute(
        """
        UPDATE PENDING_PROPOSALS
        SET STATUS = 'approved',
            DECIDED_BY = %(by)s,
            DECIDED_AT = CURRENT_TIMESTAMP(),
            INSTANCE_ID = %(iid)s
        WHERE ID = %(id)s
        """,
        {"id": proposal_id, "by": body.decided_by or "user", "iid": instance.id},
    )
    return {"ok": True, "instance_id": instance.id}


class RejectIn(BaseModel):
    reason: Optional[str] = None
    decided_by: Optional[str] = None


@router.post("/{proposal_id}/reject")
def reject_proposal(proposal_id: str, body: RejectIn):
    row = _get_proposal(proposal_id)
    if not row:
        raise HTTPException(404, f"Proposal {proposal_id} not found")
    if row["STATUS"] != "pending":
        raise HTTPException(409, f"Proposal already {row['STATUS']}")
    sf.execute(
        """
        UPDATE PENDING_PROPOSALS
        SET STATUS = 'rejected',
            DECIDED_BY = %(by)s,
            DECIDED_AT = CURRENT_TIMESTAMP(),
            DECISION_REASON = %(reason)s
        WHERE ID = %(id)s
        """,
        {"id": proposal_id,
         "by": body.decided_by or "user",
         "reason": (body.reason or "")[:2000]},
    )
    return {"ok": True}


# ─────────────────────────────────────────────────────────────────────────
# Batch endpoints — one HTTP request drives many proposals through the
# approve/reject path. Same per-proposal logic as the singular versions,
# but with a few Snowflake round-trip cuts per item:
#   * skip storage.approve_instance (we already created active + is_active
#     directly, so the follow-up UPDATE is a no-op for this flow);
#   * skip the get_instance readback (we already have the id);
#   * PARTIAL SUCCESS is fine — the response reports per-id outcome so
#     the UI can decide what to retry.
# For 13 anomaly proposals this cuts wall-clock roughly in half vs 13
# separate calls to /approve.
# ─────────────────────────────────────────────────────────────────────────


class BatchApproveIn(BaseModel):
    proposal_ids: List[str]
    decided_by: Optional[str] = None


def _approve_one_fast(proposal_id: str, decided_by: str) -> Dict[str, Any]:
    """Approve one proposal with the trimmed Snowflake path used by the
    batch endpoint. Returns {ok, instance_id?, error?} — never raises so a
    single bad row doesn't sink the whole batch."""
    try:
        row = _get_proposal(proposal_id)
        if not row:
            return {"id": proposal_id, "ok": False, "error": "not found"}
        if row["STATUS"] != "pending":
            return {"id": proposal_id, "ok": False,
                    "error": f"already {row['STATUS']}"}

        target_config = _parse_json(row.get("TARGET_CONFIG")) or {}
        threshold_config = _parse_json(row.get("THRESHOLD_CONFIG")) or {}
        template_shape = row.get("TEMPLATE_SHAPE")
        if not template_shape:
            return {"id": proposal_id, "ok": False, "error": "missing template_shape"}

        definition = storage.get_definition_by_template_shape(template_shape)
        if not definition:
            return {"id": proposal_id, "ok": False,
                    "error": f"no definition for shape {template_shape}"}

        table_asset = storage.get_asset(row["ASSET_ID"]) if row.get("ASSET_ID") else None
        if table_asset is None:
            return {"id": proposal_id, "ok": False, "error": "asset not found"}

        from app.services import rule_sql_templates
        try:
            rule_sql = rule_sql_templates.render_template(
                template_shape,
                table_asset.database_name, table_asset.schema_name, table_asset.table_name,
                target_config, threshold_config,
            )
        except Exception as e:
            return {"id": proposal_id, "ok": False, "error": f"render failed: {e}"}

        scope = "column" if row.get("COLUMN_NAME") else "table"
        fingerprint = storage._sha256(
            f"anomaly|{definition.id}|{table_asset.id}|{json.dumps(target_config, sort_keys=True)}"
        )
        existing = storage.get_instance_by_fingerprint(fingerprint)
        if existing:
            instance_id = existing.id
        else:
            # create_instance already inserts with status='active', is_active=True,
            # so we skip the follow-up storage.approve_instance UPDATE (which would
            # just set them to the same values plus approved_at) — saves 2 more
            # Snowflake calls per proposal.
            instance = storage.create_instance(
                definition_id=definition.id,
                scope=scope,
                database_name=table_asset.database_name,
                schema_name=table_asset.schema_name,
                table_name=table_asset.table_name,
                fingerprint=fingerprint,
                severity=row.get("SEVERITY") or definition.default_severity or "medium",
                target_config=target_config,
                threshold_config=threshold_config,
                rule_sql=rule_sql,
                rationale=row.get("RATIONALE"),
                status="active",
                is_active=True,
                owner="anomaly_proposal_agent",
                created_by=decided_by,
                source_run_id=row.get("SOURCE_RUN_ID"),
            )
            instance_id = instance.id

        sf.execute(
            """
            UPDATE PENDING_PROPOSALS
            SET STATUS = 'approved',
                DECIDED_BY = %(by)s,
                DECIDED_AT = CURRENT_TIMESTAMP(),
                INSTANCE_ID = %(iid)s
            WHERE ID = %(id)s
            """,
            {"id": proposal_id, "by": decided_by, "iid": instance_id},
        )
        return {"id": proposal_id, "ok": True, "instance_id": instance_id}
    except Exception as e:
        logger.exception(f"approve batch item {proposal_id} failed")
        return {"id": proposal_id, "ok": False, "error": str(e)[:500]}


@router.post("/approve-batch")
def approve_proposals_batch(body: BatchApproveIn):
    if not body.proposal_ids:
        return {"results": [], "approved": 0, "failed": 0}
    decided_by = body.decided_by or "user"
    results = [_approve_one_fast(pid, decided_by) for pid in body.proposal_ids]
    approved = sum(1 for r in results if r.get("ok"))
    failed = len(results) - approved
    return {"results": results, "approved": approved, "failed": failed}


class BatchRejectIn(BaseModel):
    proposal_ids: List[str]
    reason: Optional[str] = None
    decided_by: Optional[str] = None


@router.post("/reject-batch")
def reject_proposals_batch(body: BatchRejectIn):
    if not body.proposal_ids:
        return {"results": [], "rejected": 0, "failed": 0}
    decided_by = body.decided_by or "user"
    reason = (body.reason or "")[:2000]
    results: List[Dict[str, Any]] = []
    for pid in body.proposal_ids:
        try:
            row = _get_proposal(pid)
            if not row:
                results.append({"id": pid, "ok": False, "error": "not found"})
                continue
            if row["STATUS"] != "pending":
                results.append({"id": pid, "ok": False,
                                "error": f"already {row['STATUS']}"})
                continue
            sf.execute(
                """
                UPDATE PENDING_PROPOSALS
                SET STATUS = 'rejected',
                    DECIDED_BY = %(by)s,
                    DECIDED_AT = CURRENT_TIMESTAMP(),
                    DECISION_REASON = %(reason)s
                WHERE ID = %(id)s
                """,
                {"id": pid, "by": decided_by, "reason": reason},
            )
            results.append({"id": pid, "ok": True})
        except Exception as e:
            logger.exception(f"reject batch item {pid} failed")
            results.append({"id": pid, "ok": False, "error": str(e)[:500]})
    rejected = sum(1 for r in results if r.get("ok"))
    return {"results": results, "rejected": rejected, "failed": len(results) - rejected}
