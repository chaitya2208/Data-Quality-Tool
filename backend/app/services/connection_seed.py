"""
Backward-compat seed: turn the existing SNOWFLAKE_* .env config into one saved
Connection row so the current setup keeps working with zero reconfiguration
after the multi-source migration.

Idempotent — only seeds when no Snowflake connection already exists.
"""
import logging
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.connection import Connection, ConnectionType

logger = logging.getLogger(__name__)


def seed_default_connection(db: Session) -> None:
    account = getattr(settings, "SNOWFLAKE_ACCOUNT", None)
    if not account:
        logger.info("[Seed] No SNOWFLAKE_ACCOUNT in settings — skipping default connection seed")
        return

    existing = db.query(Connection).filter(Connection.type == ConnectionType.SNOWFLAKE).first()
    if existing:
        logger.info(f"[Seed] Snowflake connection already exists ({existing.id}) — skipping")
        return

    conn = Connection(
        name="Default Snowflake",
        type=ConnectionType.SNOWFLAKE,
        host=account,  # account identifier
        database=getattr(settings, "SNOWFLAKE_DATABASE", None),
        schema_=getattr(settings, "SNOWFLAKE_SCHEMA", None),
        username=getattr(settings, "SNOWFLAKE_USER", None),
        secret=getattr(settings, "SNOWFLAKE_PASSWORD", None),
        auth_method=getattr(settings, "SNOWFLAKE_AUTH_METHOD", "externalbrowser"),
        extra={
            "warehouse": getattr(settings, "SNOWFLAKE_WAREHOUSE", None),
            "role":      getattr(settings, "SNOWFLAKE_ROLE", None),
        },
        is_active=True,
    )
    db.add(conn)
    db.commit()
    logger.info(f"[Seed] Seeded default Snowflake connection from .env ({conn.id})")
