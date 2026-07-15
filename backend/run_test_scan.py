"""
Fires a single scan against PLAYGROUND_DB.TEST_DQ.PRODUCT_CATALOG using the
Snowflake default connection. Runs the coordinator INLINE (not in a background
thread) so logs come out in the current process and we can wait for the
awaiting_fixes state before exiting.
"""
import logging
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
# Dampen the noisiest sub-loggers
for noisy in ("snowflake.connector", "urllib3", "botocore", "boto3"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

from app.services import storage
from app.services.agents.coordinator import WorkflowCoordinator, DB_AGENT_ORDER


TARGET = ("PLAYGROUND_DB", "TEST_DQ", "SUBSCRIPTIONS")
CONNECTION_ID = "02237403-7382-4901-ac94-87735b9d8cff"


def main():
    db, sc, tb = TARGET
    print(f"\n=== Starting scan of {db}.{sc}.{tb} ===\n", flush=True)
    run = storage.create_agent_run(
        connection_id=CONNECTION_ID,
        database=db,
        schema_name=sc,
        table=tb,
        status="pending",
    )
    storage.create_agent_tasks(run.id, DB_AGENT_ORDER)
    print(f"[run] id={run.id}", flush=True)
    coord = WorkflowCoordinator(run_id=run.id)
    t0 = time.time()
    coord.run()  # blocks until awaiting_fixes / completed / failed
    elapsed = time.time() - t0

    run = storage.get_agent_run(run.id)
    print(f"\n=== Run {run.id} status={run.status} in {elapsed:.1f}s ===", flush=True)
    return run.id


if __name__ == "__main__":
    run_id = main()
    print(f"\nrun_id={run_id}")
    sys.exit(0)
