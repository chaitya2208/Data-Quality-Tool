"""
Auto-verify scheduler — runs VerificationAgent in the background every
AUTO_VERIFY_INTERVAL_SECONDS while a run is in AWAITING_FIXES state.

Design:
- One scheduler instance per run, stored in _active_schedulers dict
- Each schedule fires once, re-schedules itself only if the run is still awaiting_fixes
- Cancelled when: run completes, fails, or manually verified (manual verify resets timer)
- Thread-safe: uses threading.Timer + module-level lock
"""
import threading
import logging
from datetime import datetime
from typing import Dict

from app.services import storage

logger = logging.getLogger(__name__)

AUTO_VERIFY_INTERVAL_SECONDS = 5 * 60  # 5 minutes

MAX_CONSECUTIVE_FAILURES = 5

_lock: threading.Lock = threading.Lock()
_active_timers: Dict[str, threading.Timer] = {}    # run_id → Timer
_failure_counts: Dict[str, int] = {}               # run_id → consecutive failure count
_table_to_run: Dict[str, str] = {}                 # table_fqn → run_id (latest active run per table)


def schedule(run_id: str, delay: float = None, table_fqn: str = None) -> None:
    """
    Schedule an auto-verify for run_id after `delay` seconds. When `delay` is
    omitted, use the configured interval (Settings → auto_verify_interval_min).
    Cancels any existing timer for this run first, and any prior run on the
    same table (table_fqn) so only the newest run's scheduler is alive.
    """
    if delay is None:
        try:
            from app.services import settings_service
            delay = settings_service.get_auto_verify_interval_seconds()
        except Exception:
            delay = AUTO_VERIFY_INTERVAL_SECONDS

    # Cancel any prior run on the same table before registering this one
    if table_fqn:
        with _lock:
            prior_run_id = _table_to_run.get(table_fqn)
        if prior_run_id and prior_run_id != run_id:
            logger.info(
                f"[AutoVerify] Cancelling prior run {prior_run_id} for table {table_fqn} "
                f"— superseded by run {run_id}"
            )
            cancel(prior_run_id)

    cancel(run_id)
    timer = threading.Timer(delay, _fire, args=[run_id])
    timer.daemon = True
    with _lock:
        _active_timers[run_id] = timer
        if table_fqn:
            _table_to_run[table_fqn] = run_id
    timer.start()
    logger.info(f"[AutoVerify] Scheduled for run {run_id} in {delay:.0f}s")


def cancel(run_id: str) -> None:
    """Cancel the pending auto-verify for a run (called on manual verify or completion)."""
    with _lock:
        timer = _active_timers.pop(run_id, None)
        _failure_counts.pop(run_id, None)
        # Remove table → run mapping if it still points at this run
        stale_keys = [k for k, v in _table_to_run.items() if v == run_id]
        for k in stale_keys:
            del _table_to_run[k]
    if timer:
        timer.cancel()
        logger.debug(f"[AutoVerify] Cancelled for run {run_id}")


def _fire(run_id: str) -> None:
    """Run verification, then re-schedule if run is still awaiting_fixes."""
    with _lock:
        _active_timers.pop(run_id, None)  # remove expired entry

    try:
        run = storage.get_agent_run(run_id)
        if not run or run.status != "awaiting_fixes":
            logger.debug(f"[AutoVerify] Run {run_id} no longer awaiting_fixes, stopping")
            return

        task = storage.get_agent_task(run_id, "verification_agent")
        if not task:
            return

        logger.info(f"[AutoVerify] Running auto-verification for run {run_id}")
        storage.update_agent_task(task.id, status="running", started_at=datetime.utcnow())

        from app.services.agents.verification_agent import VerificationAgent
        result = VerificationAgent().run(run, task)

        # Always mark the task completed with the latest result. The UI
        # (AgentWorkflow) derives an amber "Partial" state from
        # output.remaining > 0 — so a completed task with remaining findings
        # renders as partial, not green.
        storage.update_agent_task(
            task.id, status="completed", completed_at=datetime.utcnow(),
            output={**result, "auto_verified": True},
        )

        # Reset failure counter on success
        with _lock:
            _failure_counts.pop(run_id, None)

        if result.get("fully_resolved"):
            storage.update_agent_run(run_id, status="completed", completed_at=datetime.utcnow())
            logger.info(f"[AutoVerify] Run {run_id} fully resolved — marked COMPLETED")
        else:
            logger.info(
                f"[AutoVerify] Run {run_id}: {result['resolved']}/{result['total_findings']} resolved, "
                f"{result['remaining']} remaining — rescheduling"
            )
            schedule(run_id, AUTO_VERIFY_INTERVAL_SECONDS)

    except Exception as e:
        with _lock:
            failures = _failure_counts.get(run_id, 0) + 1
            _failure_counts[run_id] = failures

        logger.error(f"[AutoVerify] Failed for run {run_id} (attempt {failures}/{MAX_CONSECUTIVE_FAILURES}): {e}")
        try:
            t = storage.get_agent_task(run_id, "verification_agent")
            if t and t.status == "running":
                storage.update_agent_task(t.id, status="failed", error_message=str(e)[:1024])
        except Exception:
            pass

        if failures >= MAX_CONSECUTIVE_FAILURES:
            logger.warning(
                f"[AutoVerify] Run {run_id} hit {MAX_CONSECUTIVE_FAILURES} consecutive failures — "
                f"stopping auto-verify. Fix the underlying issue and manually verify to resume."
            )
            with _lock:
                _failure_counts.pop(run_id, None)
        else:
            schedule(run_id, AUTO_VERIFY_INTERVAL_SECONDS)
