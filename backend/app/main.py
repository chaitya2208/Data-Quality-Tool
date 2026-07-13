from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import settings
from app.api import assets, scans, findings, rules, health, ai_recommendations, agent_runs
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
    except Exception as e:
        logger.error(f"Snowflake startup failed: {e}")
    yield
    # ── Shutdown ──
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
