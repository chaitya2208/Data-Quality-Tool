"""
Database setup script.
Creates tables and initializes default rules.
"""
from app.core.database import engine, Base, SessionLocal
from app.models import Asset, Scan, Finding, Rule
from app.services.rule_engine import initialize_default_rules
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def setup_database():
    """Create all tables and initialize default data"""
    logger.info("Creating database tables...")
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables created successfully")

    # Initialize default rules
    logger.info("Initializing default rules...")
    db = SessionLocal()
    try:
        initialize_default_rules(db)
        logger.info("Default rules initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize rules: {str(e)}")
        raise
    finally:
        db.close()

    logger.info("Database setup completed!")


if __name__ == "__main__":
    setup_database()
