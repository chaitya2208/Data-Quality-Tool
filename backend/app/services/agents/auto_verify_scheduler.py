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
from datetime import datetime, timedelta
from typing import Dict

logger = logging.getLogger(__name__)

AUTO_VERIFY_INTERVAL_SECONDS = 5 * 60  # 5 minutes

_lock: threading.Lock = threading.Lock()
_active_timers: Dict[str, threading.Timer] = {}  # run_id → Timer


def schedule(run_id: str, delay: float = AUTO_VERIFY_INTERVAL_SECONDS) -> None:
    """
    Schedule an auto-verify for run_id after `delay` seconds.
    Cancels any existing timer for this run first.
    """
    cancel(run_id)
    timer = threading.Timer(delay, _fire, args=[run_id])
    timer.daemon = True
    with _lock:
        _active_timers[run_id] = timer
    timer.start()
    logger.info(f"[AutoVerify] Scheduled for run {run_id} in {delay:.0f}s")


def cancel(run_id: str) -> None:
    """Cancel the pending auto-verify for a run (called on manual verify or completion)."""
    with _lock:
        timer = _active_timers.pop(run_id, None)
    if timer:
        timer.cancel()
        logger.debug(f"[AutoVerify] Cancelled for run {run_id}")


def _fire(run_id: str) -> None:
    """Run verification, then re-schedule if run is still awaiting_fixes."""
    from app.core.database import SessionLocal
    from app.models.agent_run import AgentRun, AgentTask, AgentRunStatus, AgentTaskStatus

    with _lock:
        _active_timers.pop(run_id, None)  # remove expired entry

    db = SessionLocal()
    try:
        run = db.query(AgentRun).filter(AgentRun.id == run_id).first()
        if not run or run.status != AgentRunStatus.AWAITING_FIXES:
            logger.debug(f"[AutoVerify] Run {run_id} no longer awaiting_fixes, stopping")
            return

        task = db.query(AgentTask).filter(
            AgentTask.run_id == run_id,
            AgentTask.agent_name == "verification_agent",
        ).first()
        if not task:
            return

        logger.info(f"[AutoVerify] Running auto-verification for run {run_id}")
        task.status = AgentTaskStatus.RUNNING
        task.started_at = datetime.utcnow()
        db.commit()

        from app.services.agents.verification_agent import VerificationAgent
        result = VerificationAgent(db).run(run, task)

        task.status = AgentTaskStatus.COMPLETED
        task.completed_at = datetime.utcnow()
        task.output = {**result, "auto_verified": True}
        db.commit()

        if result.get("fully_resolved"):
            run.status = AgentRunStatus.COMPLETED
            run.completed_at = datetime.utcnow()
            db.commit()
            logger.info(f"[AutoVerify] Run {run_id} fully resolved — marked COMPLETED")
        else:
            logger.info(
                f"[AutoVerify] Run {run_id}: {result['resolved']}/{result['total_findings']} resolved, "
                f"{result['remaining']} remaining — rescheduling"
            )
            # Re-schedule for next cycle
            schedule(run_id, AUTO_VERIFY_INTERVAL_SECONDS)

    except Exception as e:
        logger.error(f"[AutoVerify] Failed for run {run_id}: {e}")
        db2 = SessionLocal()
        try:
            t = db2.query(AgentTask).filter(
                AgentTask.run_id == run_id,
                AgentTask.agent_name == "verification_agent",
            ).first()
            if t and t.status.value == "running":
                t.status = AgentTaskStatus.FAILED
                t.error_message = str(e)[:1024]
                db2.commit()
        finally:
            db2.close()
        # Still re-schedule even on failure so it keeps trying
        schedule(run_id, AUTO_VERIFY_INTERVAL_SECONDS)
    finally:
        db.close()
