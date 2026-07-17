"""
Backward-compat seed: turn the existing SNOWFLAKE_* .env config into one saved
Connection row so the current setup keeps working with zero reconfiguration
after the multi-source migration.

Idempotent — only seeds when no Snowflake connection already exists.
Connections now persist in the Snowflake DQ_APP.CONNECTIONS table via
app.services.storage (the old SQLAlchemy ORM is gone).
"""
import logging

from app.core.config import settings
from app.services import storage
from app.services.connection_types import ConnectionType

logger = logging.getLogger(__name__)


def seed_default_connection(db=None) -> None:
    """`db` is accepted and ignored (kept for call-site compatibility)."""
    account = getattr(settings, "SNOWFLAKE_ACCOUNT", None)
    if not account:
        logger.info("[Seed] No SNOWFLAKE_ACCOUNT in settings — skipping default connection seed")
        return

    existing = storage.get_first_connection(prefer_type=ConnectionType.SNOWFLAKE.value)
    if existing and existing.type == ConnectionType.SNOWFLAKE.value:
        logger.info(f"[Seed] Snowflake connection already exists ({existing.id}) — skipping")
        return

    conn = storage.create_connection(
        name="Default Snowflake",
        type=ConnectionType.SNOWFLAKE.value,
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
    logger.info(f"[Seed] Seeded default Snowflake connection from .env ({conn.id})")
