"""Scheduler -- deferred-and-future-work.md §10's "orchestrator on a timer"
(mvp-scope.md MVP2: "Scheduled scans and scheduled rule execution"). Two
job types, both reading CORE.SCAN_SCHEDULES (infra/snowflake/
11_create_scan_schedules_table.sql):

    RULE_EXECUTION -- re-runs every approved rule (main.py's
                      run_all_approved_rules(), the same loop the manual
                      "Run Rules" button calls -- no duplicated logic).
                      This is what activates architecture.md §8's
                      auto-resolve-on-passing-rerun, which had existed as
                      a documented intent on update_alert_status() with no
                      real caller until now.
    RESCAN          -- re-runs the recommend-rules workflow
                       (main.py's _recommend_rules_for_table()/
                       _recommend_rules_for_tables()) over a schedule's
                       target database/schema(/table).

Runs as a BackgroundScheduler (thread-based, not asyncio) inside the same
FastAPI process, started/stopped via main.py's lifespan handler --
apscheduler's BackgroundScheduler manages its own thread and shuts down
cleanly on .shutdown(), so this doesn't need FastAPI's own event loop the
way an AsyncIOScheduler would, and this codebase has no other
asyncio-heavy code to integrate with.

Auth/connection reality, flagged explicitly (see docs/deferred-and-
future-work.md and context.md for the full writeup): this reuses the same
cached, interactive-SSO source connection every other route in this app
uses (tools/snowflake_connection.py's module-level _source_conn). That
connection only exists once a human has completed one browser login in
this process. This means:
  - A scheduled job firing before any human has ever logged in this
    process will trigger the SAME interactive browser popup a manual
    click would -- there is no user at the keyboard to click through it,
    so that job effectively hangs/fails silently until someone does log
    in via any other route.
  - This is NOT true unattended/headless scheduling. Real unattended
    operation needs key-pair auth (RSA key registered on the Snowflake
    user via `ALTER USER ... SET RSA_PUBLIC_KEY=...`, then swapping
    _connect_source()'s `authenticator=`/`client_store_temporary_credential=`
    for `private_key=<der bytes>` -- the exact swap point is already
    noted in that function's own docstring). Deliberately not built this
    round -- the user needs to obtain that key through their organization
    first. Once available, only tools/snowflake_connection.py needs to
    change; this scheduler's logic is auth-method-agnostic.
"""

from __future__ import annotations

import datetime
import logging
import os
import threading
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler

logger = logging.getLogger(__name__)

# How often the scheduler wakes up to check which SCAN_SCHEDULES rows are
# due -- separate from each individual schedule's own INTERVAL_MINUTES. A
# short poll interval (default 1 min) means a schedule fires close to its
# configured interval without needing a separate per-schedule APScheduler
# job for each row (simpler: one poll job, N schedule rows, rather than
# dynamically adding/removing APScheduler jobs as schedules are created).
_POLL_INTERVAL_MINUTES = int(os.getenv("SCHEDULER_POLL_INTERVAL_MINUTES", "1"))

_scheduler: BackgroundScheduler | None = None


def _is_due(schedule: dict[str, Any]) -> bool:
    """A schedule with no LAST_RUN_AT yet (never run) is always due --
    otherwise due when INTERVAL_MINUTES have elapsed since LAST_RUN_AT.
    """
    if schedule["last_run_at"] is None:
        return True
    now_naive_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    elapsed = now_naive_utc - schedule["last_run_at"]
    return elapsed.total_seconds() >= schedule["interval_minutes"] * 60


def _run_one_schedule(schedule: dict[str, Any]) -> None:
    """Execute one due schedule in its own thread."""
    from scan_operations import recommend_rules_for_table, recommend_rules_for_tables, run_all_approved_rules
    from tools.snowflake_metadata_tools import list_tables

    try:
        if schedule["schedule_type"] == "RULE_EXECUTION":
            results = run_all_approved_rules()
            logger.info(
                "scheduler: ran %d approved rule(s) (schedule_id=%s)",
                len(results),
                schedule["schedule_id"],
            )
        elif schedule["schedule_type"] == "RESCAN":
            db, schema, table = (
                schedule["target_database"],
                schedule["target_schema"],
                schedule["target_table"],
            )
            if table:
                recommend_rules_for_table(db, schema, table)
            else:
                tables = list_tables(db, schema)
                recommend_rules_for_tables(db, schema, [t["name"] for t in tables])
            logger.info(
                "scheduler: re-scanned %s.%s%s (schedule_id=%s)",
                db,
                schema,
                f".{table}" if table else " (all tables)",
                schedule["schedule_id"],
            )
        else:
            logger.warning(
                "scheduler: unknown schedule_type %r (schedule_id=%s), skipping",
                schedule["schedule_type"],
                schedule["schedule_id"],
            )
    except Exception:  # noqa: BLE001
        logger.exception("scheduler: schedule %s failed", schedule["schedule_id"])


def _run_due_schedules() -> None:
    """One poll tick: find every active, due schedule and fire it. A
    failure in one schedule (or in listing schedules at all) is logged and
    swallowed -- a scheduler tick failing must never crash the background
    thread it runs on (APScheduler would otherwise just silently drop
    future runs of a job whose callback raised).
    """
    from tools.storage_tools import list_scan_schedules, update_scan_schedule_last_run

    try:
        schedules = list_scan_schedules(is_active=True)
    except Exception:  # noqa: BLE001
        logger.exception("scheduler: failed to list schedules")
        return

    for schedule in schedules:
        if not _is_due(schedule):
            continue
        # Mark last_run_at immediately so the next poll doesn't re-fire this
        # schedule while its job thread is still running.
        try:
            update_scan_schedule_last_run(schedule["schedule_id"])
        except Exception:  # noqa: BLE001
            logger.exception("scheduler: failed to update last_run_at for %s", schedule["schedule_id"])
            continue

        t = threading.Thread(
            target=_run_one_schedule,
            args=(schedule,),
            daemon=True,
            name=f"sched-{schedule['schedule_id'][:8]}",
        )
        t.start()


def start_scheduler() -> None:
    """Start the background poll loop. Called once from main.py's lifespan
    handler on app startup. Idempotent -- calling twice is a no-op rather
    than starting a second thread (guards against a hot-reload restarting
    the lifespan handler without a clean shutdown first).
    """
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = BackgroundScheduler()
    _scheduler.add_job(
        _run_due_schedules,
        "interval",
        minutes=_POLL_INTERVAL_MINUTES,
        id="poll_scan_schedules",
    )
    _scheduler.start()
    logger.info("scheduler: started, polling every %d minute(s)", _POLL_INTERVAL_MINUTES)


def shutdown_scheduler() -> None:
    """Stop the background poll loop. Called from main.py's lifespan
    handler on app shutdown so the scheduler's thread doesn't outlive (or
    block) the FastAPI process exiting.
    """
    global _scheduler
    if _scheduler is None:
        return
    _scheduler.shutdown(wait=False)
    _scheduler = None
    logger.info("scheduler: stopped")
