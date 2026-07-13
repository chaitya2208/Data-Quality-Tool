"""
Idempotent schema migrations — run once at startup after SSO login.
Each migration is a plain SQL string executed against the app schema.
Failures are logged and swallowed so a missing Cortex Search privilege
never blocks the backend from starting.
"""
import logging
from app.services.snowflake_session import session as sf_session

logger = logging.getLogger(__name__)

_MIGRATIONS = [
    (
        "add_workflow_template_id_to_agent_runs",
        "ALTER TABLE AGENT_RUNS ADD COLUMN IF NOT EXISTS WORKFLOW_TEMPLATE_ID VARCHAR(36)",
    ),
    (
        "create_workflow_templates",
        """
        CREATE TABLE IF NOT EXISTS WORKFLOW_TEMPLATES (
            ID           VARCHAR(36)    NOT NULL PRIMARY KEY,
            LABEL        VARCHAR(200)   NOT NULL,
            DESCRIPTION  VARCHAR(1000),
            RULE_PATTERNS VARIANT       NOT NULL,
            CREATED_BY   VARCHAR(200),
            CREATED_AT   TIMESTAMP_NTZ  DEFAULT CURRENT_TIMESTAMP(),
            UPDATED_AT   TIMESTAMP_NTZ  DEFAULT CURRENT_TIMESTAMP()
        )
        """,
    ),
    (
        "create_rule_intelligence_logs",
        """
        CREATE TABLE IF NOT EXISTS RULE_INTELLIGENCE_LOGS (
            ID                    VARCHAR(36)    NOT NULL PRIMARY KEY,
            RUN_ID                VARCHAR(36)    NOT NULL,
            TABLE_FQN             VARCHAR(500)   NOT NULL,
            TABLE_TYPE            VARCHAR(50),
            TABLE_TYPE_CONFIDENCE INTEGER,
            THINKING              TEXT,
            SIGNALS_USED          VARIANT,
            PROPOSALS_COUNT       INTEGER        DEFAULT 0,
            SUPPRESSED_COUNT      INTEGER        DEFAULT 0,
            APPROVED_COUNT        INTEGER        DEFAULT 0,
            REJECTED_COUNT        INTEGER        DEFAULT 0,
            MODEL_USED            VARCHAR(100)   DEFAULT 'us.anthropic.claude-opus-4-8',
            CREATED_AT            TIMESTAMP_NTZ  DEFAULT CURRENT_TIMESTAMP()
        )
        """,
    ),
    (
        "create_rule_intelligence_search",
        # Cortex Search — requires CORTEX_USER privilege.
        # Wrapped in a try inside run_migrations; if Cortex Search is
        # unavailable the app still works, search_similar_intelligence
        # just returns [] until the service exists.
        """
        CREATE OR REPLACE CORTEX SEARCH SERVICE RULE_INTELLIGENCE_SEARCH
            ON THINKING
            ATTRIBUTES TABLE_FQN, TABLE_TYPE, APPROVED_COUNT, REJECTED_COUNT
            WAREHOUSE = COMPUTE_WH
            TARGET_LAG = '1 hour'
            AS (
                SELECT
                    ID,
                    TABLE_FQN,
                    TABLE_TYPE,
                    APPROVED_COUNT,
                    REJECTED_COUNT,
                    TABLE_FQN || ' ' || COALESCE(THINKING, '') AS THINKING
                FROM RULE_INTELLIGENCE_LOGS
                WHERE THINKING IS NOT NULL
                  AND LENGTH(THINKING) > 0
            )
        """,
    ),
]


def run_migrations() -> None:
    for name, sql in _MIGRATIONS:
        try:
            sf_session.execute(sql.strip())
            logger.info(f"[Migrations] {name}: ok")
        except Exception as e:
            logger.warning(f"[Migrations] {name}: skipped — {e}")
