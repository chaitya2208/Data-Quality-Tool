"""Run the pipeline (findings + explanation) for the given AgentRun,
approving all proposals in the review state as-is (no filtering)."""
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
for noisy in ("snowflake.connector", "urllib3", "botocore", "boto3"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

from app.services.agents.coordinator import WorkflowCoordinator

if len(sys.argv) < 2:
    print("Usage: run_pipeline.py <run_id>")
    sys.exit(1)

run_id = sys.argv[1]
print(f"\n=== Running pipeline for {run_id} ===\n", flush=True)
WorkflowCoordinator(run_id=run_id).run_pipeline_after_review()
print("\n=== Done ===")
