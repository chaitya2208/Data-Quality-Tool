from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import settings
from app.api import assets, scans, findings, rules, health, ai_recommendations, agent_runs, profiling, connections, schedules, settings as settings_api, table_health, mutes, validate, notifications, proposals, maintenance, lineage
from app.services.snowflake_session import session as sf_session
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
# Third-party libraries log verbosely at INFO by default (connector internals,
# OCSP cert checks, HTTP retries, telemetry) — they'd otherwise drown out the
# app's own log lines since basicConfig sets the level for every logger.
for _noisy_logger in ("snowflake.connector", "boto3", "botocore", "urllib3"):
    logging.getLogger(_noisy_logger).setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup: one SSO login, warm up cache ──
    logger.info("Starting up — connecting to Snowflake (SSO)…")
    try:
        sf_session.connect()   # opens browser once
        sf_session.warm_up()   # fetches roles, warehouses, databases
        logger.info("Snowflake session ready.")
        # Seed the default Snowflake connection from .env (idempotent) so the
        # multi-source connections UI works out of the box.
        try:
            from app.services.connection_seed import seed_default_connection
            seed_default_connection()
        except Exception as seed_err:
            logger.warning(f"Default connection seed skipped: {seed_err}")
        try:
            from app.services.migrations import run_migrations
            run_migrations()
        except Exception as mig_err:
            logger.warning(f"Migrations skipped: {mig_err}")
        # Start the schedule runner AFTER migrations so the SCHEDULES table
        # exists. Best-effort — a scheduler failure must not block startup.
        try:
            from app.services import schedule_runner
            schedule_runner.start()
        except Exception as sched_err:
            logger.warning(f"Scheduler start skipped: {sched_err}")
        # Recover any runs that were left 'running' by a prior server crash/restart.
        try:
            from app.services.storage import recover_orphaned_runs
            n = recover_orphaned_runs()
            if n:
                logger.info(f"Recovered {n} orphaned run(s) from prior server restart.")
        except Exception as rec_err:
            logger.warning(f"Orphaned-run recovery skipped: {rec_err}")
    except Exception as e:
        logger.error(f"Snowflake startup failed: {e}")
    yield
    # ── Shutdown ──
    try:
        from app.services import schedule_runner
        schedule_runner.stop()
    except Exception:
        pass
    logger.info("Shutting down.")


app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    lifespan=lifespan,
)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],  # React dev server
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(health.router, tags=["health"])
app.include_router(assets.router, prefix=f"{settings.API_V1_STR}/assets", tags=["assets"])
app.include_router(scans.router, prefix=f"{settings.API_V1_STR}/scans", tags=["scans"])
app.include_router(findings.router, prefix=f"{settings.API_V1_STR}/findings", tags=["findings"])
app.include_router(rules.router, prefix=f"{settings.API_V1_STR}/rules", tags=["rules"])
app.include_router(ai_recommendations.router, prefix=f"{settings.API_V1_STR}/ai", tags=["ai"])
app.include_router(agent_runs.router, prefix=f"{settings.API_V1_STR}/agent", tags=["agent"])
app.include_router(profiling.router, prefix=f"{settings.API_V1_STR}/profiling", tags=["profiling"])
app.include_router(connections.router, prefix=f"{settings.API_V1_STR}/connections", tags=["connections"])
app.include_router(schedules.router, prefix=f"{settings.API_V1_STR}/schedules", tags=["schedules"])
app.include_router(settings_api.router, prefix=f"{settings.API_V1_STR}/settings", tags=["settings"])
app.include_router(table_health.router, prefix=f"{settings.API_V1_STR}/table-health", tags=["table-health"])
app.include_router(mutes.router, prefix=f"{settings.API_V1_STR}/mutes", tags=["mutes"])
app.include_router(lineage.router, prefix=f"{settings.API_V1_STR}/lineage", tags=["lineage"])
app.include_router(notifications.router, prefix=f"{settings.API_V1_STR}/notifications", tags=["notifications"])
app.include_router(proposals.router, prefix=f"{settings.API_V1_STR}/proposals", tags=["proposals"])
app.include_router(validate.router, prefix=f"{settings.API_V1_STR}/validate", tags=["validate"])
app.include_router(maintenance.router, prefix=f"{settings.API_V1_STR}/maintenance", tags=["maintenance"])


@app.get("/")
async def root():
    return {
        "message": "Data Quality Platform API",
        "version": "0.1.0",
        "docs": f"{settings.API_V1_STR}/docs"
    }


@app.get("/health")
async def health_check():
    return {"status": "healthy"}
