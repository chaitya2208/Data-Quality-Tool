"""
Scan finalization — incident lifecycle state machine.

Called at the end of every scan (manual, agentic, and scheduled — they all
funnel through FindingsAgent.run, which delegates to finalize_scan below).

For each (rule_instance, asset) evaluated in this scan, produces exactly ONE
of four outcomes:

    UPDATE  — rule failed AND an open finding exists → bump counts + history,
              preserve first_detected_at (the "broken since Tuesday" clock).
    RESOLVE — rule passed AND an open finding exists → auto-close with note.
    REOPEN  — rule failed AND a recently-resolved finding exists (within
              REOPEN_WINDOW_DAYS) → revive it; reopened_count += 1.
    CREATE  — rule failed AND nothing exists (or resolved > window ago)
              → brand-new incident.

MUTES: if (instance, asset) is currently muted, the lifecycle is SKIPPED —
executions still ran and RULE_EXECUTIONS still logged, but no new incident
appears and open incidents are not touched. Passing during a mute still
auto-resolves (mutes silence noise, not fixes).

Evidence contract: every rule-engine result must carry evidence with
{fail_count, total_count, sample_rows}. See rule_engine.py + dynamic_rules.py
where these are populated, and rule_sql_templates.failing_rows_sample_sql for
the SELECTs that fetch the sample rows.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set

from app.services import storage

logger = logging.getLogger(__name__)

REOPEN_WINDOW_DAYS = 7   # resolved-within-N-days findings reopen; older → new incident


def _evidence_counts(evidence: Optional[dict]) -> tuple[int, int]:
    """Read the standard evidence contract, tolerating missing keys."""
    if not evidence:
        return 0, 0
    fc = evidence.get("fail_count",
          evidence.get("failed_count", 0))    # legacy fallback
    tc = evidence.get("total_count", 0)
    try:
        return int(fc or 0), int(tc or 0)
    except (TypeError, ValueError):
        return 0, 0


def finalize_scan(
    scan_id: str,
    asset_id_for_passed: str,
    findings_data: List[Dict[str, Any]],
    executed_instance_ids: Set[str],
) -> Dict[str, Any]:
    """Apply the lifecycle state machine.

    Args:
        scan_id: the scan just completed.
        asset_id_for_passed: the table asset id — used as the ASSET_ID lookup
            key for RESOLVE on rules that PASSED this scan (a passed rule
            produces no finding, so we need the target asset from context).
            For column-level rules the same key works: existing open findings
            were originally created against either the table or column asset,
            and find_open_finding does not require a specific one — the
            resolve loop below matches ALL open findings for this instance.
        findings_data: list of finding dicts produced by RuleEngine —
            everything that FAILED. Each dict has instance_id, asset_id,
            evidence.fail_count/total_count, etc.
        executed_instance_ids: instances that RAN this scan (both pass + fail).
            Used to compute the passed set = executed - failed.

    Returns:
        Counts by branch — `{"updated": n, "resolved": n, "reopened": n,
        "created": n, "muted": n}` — plus `"events"`: a list of per-finding
        lifecycle events for THIS scan, shape
            {finding_id, event: "created"|"updated"|"reopened"|"resolved",
             instance_id, asset_id, prev_fail_count, prev_total_count,
             curr_fail_count, curr_total_count}
        so callers can render "N created / M updated (was → now) / K reopened"
        instead of a static "N findings" that hides what actually changed.
    """
    stats: Dict[str, Any] = {
        "updated": 0, "resolved": 0, "reopened": 0, "created": 0, "muted": 0,
        "events": [],
    }

    failed_by_instance: Dict[str, Dict[str, Any]] = {}
    for fd in findings_data:
        iid = fd.get("instance_id")
        if iid:
            failed_by_instance[iid] = fd

    # ── PASS branch: for every instance that ran but did NOT produce a
    # finding this scan, resolve any still-open findings. ─────────────────
    for instance_id in executed_instance_ids - set(failed_by_instance.keys()):
        # A rule can have open findings on the table asset OR a column asset,
        # depending on where it was originally raised. Rather than guess, we
        # look for any open finding for this instance and resolve it — the
        # asset_id_for_passed is only a hint for the initial lookup; if the
        # rule's open finding lives on a column asset, we still find it via
        # the fallback pass below.
        resolved_any = False
        for asset_id in _candidate_asset_ids_for_pass(asset_id_for_passed, instance_id):
            existing = storage.find_open_finding(instance_id, asset_id)
            if existing:
                if storage.is_muted(instance_id, asset_id):
                    stats["muted"] += 1
                    continue
                storage.auto_resolve_finding(existing.id, scan_id)
                stats["resolved"] += 1
                stats["events"].append({
                    "finding_id": existing.id,
                    "event": "resolved",
                    "instance_id": instance_id,
                    "asset_id": asset_id,
                    "prev_fail_count": getattr(existing, "current_fail_count", None),
                    "prev_total_count": getattr(existing, "current_total_count", None),
                    "curr_fail_count": 0,
                    "curr_total_count": getattr(existing, "current_total_count", None),
                })
                resolved_any = True
        if not resolved_any:
            pass  # rule passed, nothing was open — noop

    # ── FAIL branch: UPDATE / REOPEN / CREATE per finding dict ────────────
    for instance_id, fd in failed_by_instance.items():
        asset_id = fd.get("asset_id")
        if not asset_id:
            logger.warning(f"[finalize_scan] Finding for instance {instance_id} has no asset_id; skipping")
            continue

        # Muted: log the execution (already done upstream) but don't create/
        # update an incident during the mute window.
        if storage.is_muted(instance_id, asset_id):
            stats["muted"] += 1
            continue

        fail_count, total_count = _evidence_counts(fd.get("evidence"))
        severity = fd.get("severity")
        evidence = fd.get("evidence")

        open_f = storage.find_open_finding(instance_id, asset_id)
        if open_f:
            prev_fail = getattr(open_f, "current_fail_count", None)
            prev_total = getattr(open_f, "current_total_count", None)
            storage.apply_finding_update(
                open_f.id, scan_id,
                fail_count=fail_count, total_count=total_count,
                severity=severity, evidence=evidence,
            )
            stats["updated"] += 1
            stats["events"].append({
                "finding_id": open_f.id,
                "event": "updated",
                "instance_id": instance_id,
                "asset_id": asset_id,
                "prev_fail_count": prev_fail,
                "prev_total_count": prev_total,
                "curr_fail_count": fail_count,
                "curr_total_count": total_count,
            })
            continue

        resolved_f = storage.find_recently_resolved_finding(
            instance_id, asset_id, within_days=REOPEN_WINDOW_DAYS,
        )
        if resolved_f:
            prev_fail = getattr(resolved_f, "current_fail_count", None)
            prev_total = getattr(resolved_f, "current_total_count", None)
            storage.reopen_finding(
                resolved_f.id, scan_id,
                fail_count=fail_count, total_count=total_count,
                severity=severity, evidence=evidence,
            )
            stats["reopened"] += 1
            stats["events"].append({
                "finding_id": resolved_f.id,
                "event": "reopened",
                "instance_id": instance_id,
                "asset_id": asset_id,
                # For a reopen, "previous" is the state just before it was
                # resolved — same fields on the row (they aren't zeroed on
                # auto-resolve). Lets the UI say "was 5 failing rows, resolved,
                # now failing again with 7".
                "prev_fail_count": prev_fail,
                "prev_total_count": prev_total,
                "curr_fail_count": fail_count,
                "curr_total_count": total_count,
            })
            continue

        created = storage.create_finding_with_lifecycle(
            asset_id=asset_id, scan_id=scan_id, instance_id=instance_id,
            title=fd["title"], description=fd["description"],
            severity=severity, context=fd.get("context"), evidence=evidence,
            fail_count=fail_count, total_count=total_count,
        )
        stats["created"] += 1
        stats["events"].append({
            "finding_id": created.id,
            "event": "created",
            "instance_id": instance_id,
            "asset_id": asset_id,
            "prev_fail_count": None,
            "prev_total_count": None,
            "curr_fail_count": fail_count,
            "curr_total_count": total_count,
        })

    logger.info(
        f"[finalize_scan] scan={scan_id} updated={stats['updated']} "
        f"resolved={stats['resolved']} reopened={stats['reopened']} "
        f"created={stats['created']} muted={stats['muted']}"
    )
    return stats


def _candidate_asset_ids_for_pass(table_asset_id: str, instance_id: str) -> List[str]:
    """When a rule PASSES, we don't have a finding row to tell us which asset
    its previous incident lived on (table or a specific column). Return the
    table asset id first (covers table-scoped rules and the common case),
    then any column-asset ids that have prior open findings for this
    instance. Small extra query on the pass path, but correct."""
    ids = [table_asset_id] if table_asset_id else []
    try:
        rows = _sf().query(
            """
            SELECT DISTINCT ASSET_ID FROM FINDINGS
            WHERE INSTANCE_ID = %(iid)s
              AND STATUS IN ('open','reopened')
              AND (ASSET_ID <> %(aid)s OR %(aid)s IS NULL)
            """,
            {"iid": instance_id, "aid": table_asset_id},
        )
        ids.extend(r["ASSET_ID"] for r in rows if r.get("ASSET_ID"))
    except Exception as e:
        logger.debug(f"[finalize_scan] candidate_asset_ids lookup failed: {e}")
    return ids


def _sf():
    from app.services.snowflake_session import session
    return session
