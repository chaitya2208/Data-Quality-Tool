"""
Maintenance Agent — evaluates whether existing rule instances are still
useful and proposes cleanup actions.

Runs on-demand (weekly cadence recommended). Scans every active
RULE_INSTANCES row and applies four deterministic heuristics:

  - retire_candidate: no failures in RETIRE_QUIET_DAYS AND never reopened
  - flapping:         reopened_count >= FLAPPING_REOPEN_THRESHOLD
  - superseded:       another active instance covers the same
                      (definition_id, asset_fqn, target_config)
  - obsolete_target:  the referenced ASSET row is gone

Emits MAINTENANCE_PROPOSALS rows (action + reason + evidence) which the
user reviews in a queue UI. Deterministic first; LLM narrative can be
layered on later.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from app.services import storage
from app.services.snowflake_session import session as sf

logger = logging.getLogger(__name__)

RETIRE_QUIET_DAYS = 90
RETIRE_MIN_RUNS = 3
FLAPPING_REOPEN_THRESHOLD = 4
_EXECUTION_LOOKBACK = 200


def _asset_fqn(inst: Any) -> str:
    return f"{inst.database_name}.{inst.schema_name}.{inst.table_name}".upper()


def _target_key(inst: Any) -> str:
    import json
    try:
        return json.dumps(inst.target_config or {}, sort_keys=True)
    except Exception:
        return str(inst.target_config)


def _skip(instance_id: str, action: str) -> bool:
    return storage.has_pending_maintenance_proposal(instance_id, action)


def _evaluate_retire_and_flapping(
    inst: Any, executions: List[Any], findings: List[Any],
) -> List[Tuple[str, str, dict]]:
    """Returns a list of (action, reason, evidence) tuples."""
    import datetime
    out: List[Tuple[str, str, dict]] = []

    max_reopens = max((getattr(f, "reopened_count", 0) or 0) for f in findings) if findings else 0
    if max_reopens >= FLAPPING_REOPEN_THRESHOLD:
        out.append((
            "flapping",
            f"Rule has flapped {max_reopens} times — consider loosening "
            f"the threshold or muting the instance.",
            {"max_reopened_count": max_reopens,
             "finding_ids": [f.id for f in findings if (getattr(f, "reopened_count", 0) or 0) >= FLAPPING_REOPEN_THRESHOLD][:5]},
        ))
        # If it's flapping, don't also propose retire — inconsistent.
        return out

    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(days=RETIRE_QUIET_DAYS)
    fails_in_window = 0
    last_fail_at = None
    for e in executions:
        ex_at = getattr(e, "executed_at", None)
        if ex_at is None:
            continue
        # Snowflake returns tz-aware datetimes; guard anyway.
        try:
            aware = ex_at if ex_at.tzinfo else ex_at.replace(tzinfo=datetime.timezone.utc)
        except AttributeError:
            continue
        if getattr(e, "status", None) == "failed":
            if aware >= cutoff:
                fails_in_window += 1
            if last_fail_at is None or aware > last_fail_at:
                last_fail_at = aware

    ever_reopened = any((getattr(f, "reopened_count", 0) or 0) > 0 for f in findings)
    if fails_in_window == 0 and not ever_reopened and executions:
        # Must have SOME executions in the window — else the rule may
        # simply not have run. Require an executed_at within window.
        recent_runs = sum(
            1 for e in executions
            if getattr(e, "executed_at", None) and
            (e.executed_at if e.executed_at.tzinfo else e.executed_at.replace(tzinfo=datetime.timezone.utc)) >= cutoff
        )
        if recent_runs >= RETIRE_MIN_RUNS:
            out.append((
                "retire_candidate",
                f"No failures in the last {RETIRE_QUIET_DAYS} days "
                f"({recent_runs} clean runs); consider pausing.",
                {"quiet_days": RETIRE_QUIET_DAYS,
                 "recent_clean_runs": recent_runs,
                 "last_failure_at": str(last_fail_at) if last_fail_at else None},
            ))
    return out


def _detect_superseded(instances: List[Any]) -> List[Tuple[str, str, str, dict]]:
    """Returns (older_instance_id, action, reason, evidence) tuples.
    An instance is superseded when another ACTIVE instance shares the
    same (definition_id, asset_fqn, target_config) and was created later.
    """
    groups: Dict[Tuple[str, str, str], List[Any]] = {}
    for inst in instances:
        key = (inst.definition_id, _asset_fqn(inst), _target_key(inst))
        groups.setdefault(key, []).append(inst)

    import datetime
    _EPOCH = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
    out: List[Tuple[str, str, str, dict]] = []
    for key, group in groups.items():
        if len(group) < 2:
            continue
        # Newest wins; older ones are superseded. NULL created_at (shouldn't
        # happen in practice) sorts to the bottom.
        ordered = sorted(group, key=lambda i: getattr(i, "created_at", None) or _EPOCH, reverse=True)
        winner = ordered[0]
        for older in ordered[1:]:
            out.append((
                older.id,
                "superseded",
                f"A newer active instance ({winner.id}) covers the same "
                f"target — this one is redundant.",
                {"newer_instance_id": winner.id,
                 "definition_id": older.definition_id,
                 "asset_fqn": key[1]},
            ))
    return out


def _detect_obsolete_target(inst: Any, asset_by_fqn: Dict[str, Any]) -> Optional[Tuple[str, str, dict]]:
    fqn = _asset_fqn(inst)
    if asset_by_fqn.get(fqn) is None:
        return (
            "obsolete_target",
            f"Referenced asset {fqn} no longer exists — rule cannot run.",
            {"asset_fqn": fqn},
        )
    return None


def _prefetch_assets_by_fqn(fqns: List[str]) -> Dict[str, Any]:
    """Batch-load ASSETS for a set of FQNs. Missing FQNs map to None so callers
    can distinguish 'not fetched yet' from 'fetched, does not exist'."""
    result: Dict[str, Any] = {fqn: None for fqn in fqns}
    if not fqns:
        return result
    placeholders = ", ".join(f"%(f{n})s" for n in range(len(fqns)))
    params = {f"f{n}": fqn for n, fqn in enumerate(fqns)}
    rows = sf.query(
        f"SELECT * FROM ASSETS WHERE FQN IN ({placeholders})",
        params,
    )
    for r in rows:
        result[r["FQN"]] = storage._asset_from_row(r)
    return result


def _prefetch_findings_by_asset(asset_ids: List[str]) -> Dict[str, List[Any]]:
    """One query fetching non-superseded findings for a set of assets, bucketed
    by instance_id. Rules use per-instance reopened_count, so we filter to
    findings that carry an INSTANCE_ID."""
    by_instance: Dict[str, List[Any]] = {}
    if not asset_ids:
        return by_instance
    placeholders = ", ".join(f"%(a{n})s" for n in range(len(asset_ids)))
    params = {f"a{n}": aid for n, aid in enumerate(asset_ids)}
    rows = sf.query(
        f"""
        SELECT * FROM FINDINGS
        WHERE ASSET_ID IN ({placeholders})
          AND STATUS <> 'superseded'
          AND INSTANCE_ID IS NOT NULL
        """,
        params,
    )
    for r in rows:
        try:
            f = storage._finding_from_row(r)
        except Exception:
            continue
        by_instance.setdefault(f.instance_id, []).append(f)
    return by_instance


def run() -> Dict[str, Any]:
    """Full sweep. Returns summary counts by action."""
    _total, instances = storage.list_instances(status="active", limit=5000)
    if not instances:
        return {"scanned": 0, "proposals_created": 0, "by_action": {}}

    instance_ids = [i.id for i in instances]
    exec_by_iid = storage.list_executions_for_instances(instance_ids, limit_per_instance=_EXECUTION_LOOKBACK)

    # Batch-fetch: one query for all referenced assets, one for all findings
    # attached to those assets. Buckets by instance in Python instead of
    # firing 2N queries.
    unique_fqns = sorted({_asset_fqn(i) for i in instances})
    asset_by_fqn = _prefetch_assets_by_fqn(unique_fqns)
    live_asset_ids = [a.id for a in asset_by_fqn.values() if a is not None]
    findings_by_iid = _prefetch_findings_by_asset(live_asset_ids)

    created = 0
    by_action: Dict[str, int] = {}

    # Per-instance rules
    for inst in instances:
        executions = exec_by_iid.get(inst.id, [])
        findings = findings_by_iid.get(inst.id, [])

        obsolete = _detect_obsolete_target(inst, asset_by_fqn)
        if obsolete:
            action, reason, evidence = obsolete
            if not _skip(inst.id, action):
                storage.create_maintenance_proposal(inst.id, action, reason, evidence)
                created += 1
                by_action[action] = by_action.get(action, 0) + 1
            # If target is gone, skip the retire/flapping checks — they'll
            # be noisy.
            continue

        for action, reason, evidence in _evaluate_retire_and_flapping(inst, executions, findings):
            if _skip(inst.id, action):
                continue
            storage.create_maintenance_proposal(inst.id, action, reason, evidence)
            created += 1
            by_action[action] = by_action.get(action, 0) + 1

    # Cross-instance rule
    for older_id, action, reason, evidence in _detect_superseded(instances):
        if _skip(older_id, action):
            continue
        storage.create_maintenance_proposal(older_id, action, reason, evidence)
        created += 1
        by_action[action] = by_action.get(action, 0) + 1

    logger.info(f"[MaintenanceAgent] scanned={len(instances)} created={created} by_action={by_action}")
    if created > 0:
        try:
            _emit_summary_notification(created, by_action)
        except Exception as e:
            logger.warning(f"[MaintenanceAgent] notification create failed: {e}")
    return {
        "scanned": len(instances),
        "proposals_created": created,
        "by_action": by_action,
    }


_ACTION_LABEL = {
    "retire_candidate": "retire",
    "flapping": "flapping",
    "superseded": "superseded",
    "obsolete_target": "obsolete",
}


def _emit_summary_notification(created: int, by_action: Dict[str, int]) -> None:
    parts = [f"{n} {_ACTION_LABEL.get(a, a)}" for a, n in sorted(by_action.items()) if n]
    breakdown = ", ".join(parts) if parts else "cleanup"
    title = f"{created} rule cleanup suggestion{'s' if created != 1 else ''}: {breakdown}"[:500]
    body = (
        "MaintenanceAgent found rule instances that look stale, flapping, "
        "redundant, or pointing at dropped tables. Review and apply."
    )
    sf.execute(
        """
        INSERT INTO NOTIFICATIONS
            (ID, KIND, TITLE, BODY, REF_TABLE, REF_ID, SEVERITY)
        VALUES
            (%(id)s, 'maintenance_proposals', %(title)s, %(body)s,
             'MAINTENANCE_PROPOSALS', NULL, 'info')
        """,
        {"id": storage._new_id(), "title": title, "body": body},
    )
