"""
Schedule runner — a background tick loop that fires due schedules.

Modeled on auto_verify_scheduler.py: a single daemon threading.Timer that
re-arms itself every TICK_SECONDS, no external dependency. Each tick:
  1. reads due schedules (ENABLED, NEXT_RUN_AT <= now)
  2. atomically claims each one (advancing NEXT_RUN_AT — the double-fire guard)
  3. starts the batch via the shared run_batch() service
  4. records LAST_STATUS / LAST_BATCH_ID / LAST_ERROR

One failing schedule (e.g. an expired Snowflake SSO token) is caught per-item
so it never kills the loop — the schedule is marked LAST_STATUS='error' and the
loop keeps ticking. NEXT_RUN_AT has already advanced, so a broken schedule
retries on its next cadence rather than hot-looping.
"""
import threading
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

TICK_SECONDS = 60

_lock = threading.Lock()
_timer: threading.Timer | None = None
_running = False


def start() -> None:
    """Begin the tick loop. Idempotent — a second call is a no-op."""
    global _running
    with _lock:
        if _running:
            return
        _running = True
    logger.info(f"[Scheduler] Started — ticking every {TICK_SECONDS}s")
    _arm()


def stop() -> None:
    """Cancel the pending tick (called on shutdown)."""
    global _running, _timer
    with _lock:
        _running = False
        if _timer:
            _timer.cancel()
            _timer = None
    logger.info("[Scheduler] Stopped")


def _arm() -> None:
    global _timer
    with _lock:
        if not _running:
            return
        _timer = threading.Timer(TICK_SECONDS, _tick)
        _timer.daemon = True
        _timer.start()


def _tick() -> None:
    try:
        _process_due()
    except Exception as e:
        # A failure reading the schedule list (e.g. Snowflake unreachable) must
        # not stop the loop — log and re-arm for the next tick.
        logger.warning(f"[Scheduler] Tick failed: {e}")
    finally:
        _arm()


def _process_due() -> None:
    from app.services import storage
    from app.services.schedule_calc import compute_next_run
    from app.services.batch_runner import run_batch

    now = datetime.now()
    due = storage.list_due_schedules(now)
    if not due:
        return

    logger.info(f"[Scheduler] {len(due)} schedule(s) due")
    for sched in due:
        try:
            new_next = compute_next_run(sched, now)
            # Atomic claim — only the winner runs the batch. Guards against a
            # slow batch overlapping the next tick, or multiple workers.
            if not storage.claim_schedule(sched.id, sched.next_run_at, new_next):
                logger.debug(f"[Scheduler] Schedule {sched.id} already claimed — skipping")
                continue

            batch_id, runs = run_batch(
                connection_id=sched.connection_id,
                scope=sched.scope,
                database=sched.database_name,
                schema_name=sched.schema_name,
                table=sched.table_name,
                workflow_template_id=sched.workflow_template_id,
                schedule_id=sched.id,
            )
            storage.update_schedule(
                sched.id,
                last_batch_id=batch_id,
                last_status="ok",
                last_error=None,
            )
            logger.info(
                f"[Scheduler] Fired schedule '{sched.name}' ({sched.id}) — "
                f"batch {batch_id}, {len(runs)} table(s); next run {new_next.isoformat()}"
            )
        except Exception as e:
            logger.error(f"[Scheduler] Schedule '{sched.name}' ({sched.id}) failed: {e}")
            try:
                storage.update_schedule(
                    sched.id, last_status="error", last_error=str(e)[:1024]
                )
            except Exception:
                pass
