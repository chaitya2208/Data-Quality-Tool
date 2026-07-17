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
        # Links a run back to the schedule that fired it (null for manual/ad-hoc
        # runs) so Run History can badge and filter scheduled runs reliably.
        "add_schedule_id_to_agent_runs",
        "ALTER TABLE AGENT_RUNS ADD COLUMN IF NOT EXISTS SCHEDULE_ID VARCHAR(36)",
    ),
    (
        # Who approved/rejected a rule instance (Snowflake session user) so the
        # Rule Library review queue can show the approver, not just a timestamp.
        "add_approved_by_to_rule_instances",
        "ALTER TABLE RULE_INSTANCES ADD COLUMN IF NOT EXISTS APPROVED_BY VARCHAR(255)",
    ),
    (
        "add_rejected_by_to_rule_instances",
        "ALTER TABLE RULE_INSTANCES ADD COLUMN IF NOT EXISTS REJECTED_BY VARCHAR(255)",
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
    # Origin of a saved workflow (the table it was created from) so the Schedule
    # modal can auto-fill scope/database/schema/table. Nullable — workflows saved
    # before this migration have no origin and fall back to manual entry. One
    # column per statement (Snowflake does not accept repeated ADD COLUMN clauses
    # in a single ALTER), matching the proven single-column pattern above.
    (
        "add_workflow_origin_scope",
        "ALTER TABLE WORKFLOW_TEMPLATES ADD COLUMN IF NOT EXISTS ORIGIN_SCOPE VARCHAR(20)",
    ),
    (
        "add_workflow_origin_database",
        "ALTER TABLE WORKFLOW_TEMPLATES ADD COLUMN IF NOT EXISTS ORIGIN_DATABASE VARCHAR(255)",
    ),
    (
        "add_workflow_origin_schema",
        "ALTER TABLE WORKFLOW_TEMPLATES ADD COLUMN IF NOT EXISTS ORIGIN_SCHEMA VARCHAR(255)",
    ),
    (
        "add_workflow_origin_table",
        "ALTER TABLE WORKFLOW_TEMPLATES ADD COLUMN IF NOT EXISTS ORIGIN_TABLE VARCHAR(255)",
    ),
    (
        "create_schedules",
        """
        CREATE TABLE IF NOT EXISTS SCHEDULES (
            ID                   VARCHAR(36)   NOT NULL PRIMARY KEY,
            NAME                 VARCHAR(200)  NOT NULL,
            ENABLED              BOOLEAN       DEFAULT TRUE,
            CONNECTION_ID        VARCHAR(36),
            SCOPE                VARCHAR(20)   NOT NULL,   -- database | schema | table
            DATABASE_NAME        VARCHAR(255),
            SCHEMA_NAME          VARCHAR(255),
            TABLE_NAME           VARCHAR(255),
            WORKFLOW_TEMPLATE_ID VARCHAR(36),              -- null = AI pipeline, set = saved workflow
            CADENCE              VARCHAR(20)   NOT NULL,    -- daily|weekly|monthly|yearly|custom
            TIME_OF_DAY          VARCHAR(5),               -- 'HH:MM' 24h, server-local
            DAY_OF_WEEK          INTEGER,                  -- weekly: 0=Mon..6=Sun
            DAY_OF_MONTH         INTEGER,                  -- monthly/yearly: 1..31
            MONTH_OF_YEAR        INTEGER,                  -- yearly: 1..12
            INTERVAL_VALUE       INTEGER,                  -- custom: N
            INTERVAL_UNIT        VARCHAR(10),              -- custom: 'hours'|'days'
            NEXT_RUN_AT          TIMESTAMP_NTZ,
            LAST_RUN_AT          TIMESTAMP_NTZ,
            LAST_BATCH_ID        VARCHAR(36),
            LAST_STATUS          VARCHAR(20),              -- 'ok'|'error'
            LAST_ERROR           VARCHAR(1024),
            CREATED_AT           TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
            CREATED_BY           VARCHAR(200)
        )
        """,
    ),
    (
        "create_rule_review_lessons",
        """
        CREATE TABLE IF NOT EXISTS DQ_APP.RULE_REVIEW_LESSONS (
            ID            VARCHAR(36)    NOT NULL PRIMARY KEY,
            RUN_ID        VARCHAR(36)    NOT NULL,
            TABLE_FQN     VARCHAR(500)   NOT NULL,
            VERDICT       VARCHAR(20)    NOT NULL,
            CHECK_CONCEPT VARCHAR(200),
            COLUMN_NAME   VARCHAR(200),
            SEVERITY      VARCHAR(50),
            REASON        VARCHAR(1000),
            CREATED_AT    TIMESTAMP_NTZ  DEFAULT CURRENT_TIMESTAMP()
        )
        """,
    ),
    (
        "create_rule_feedback_memos",
        """
        CREATE TABLE IF NOT EXISTS DQ_APP.RULE_FEEDBACK_MEMOS (
            ID                 VARCHAR(36)    NOT NULL PRIMARY KEY,
            BARE_TABLE_NAME    VARCHAR(200)   NOT NULL,
            TABLE_TYPE         VARCHAR(50)    NOT NULL,
            MEMO               VARIANT        NOT NULL,
            LESSON_COUNT       INTEGER        DEFAULT 0,
            CREATED_AT         TIMESTAMP_NTZ  DEFAULT CURRENT_TIMESTAMP(),
            UPDATED_AT         TIMESTAMP_NTZ  DEFAULT CURRENT_TIMESTAMP(),
            UNIQUE (BARE_TABLE_NAME, TABLE_TYPE)
        )
        """,
    ),
    # ── Incident lifecycle on FINDINGS ────────────────────────────────────
    # One FINDINGS row per (INSTANCE_ID, ASSET_ID) persists across scans;
    # each scan UPDATEs / RESOLVEs / REOPENs / CREATEs. Columns added below.
    ("add_findings_first_detected_at",
     "ALTER TABLE FINDINGS ADD COLUMN IF NOT EXISTS FIRST_DETECTED_AT TIMESTAMP_TZ(9)"),
    ("add_findings_last_seen_at",
     "ALTER TABLE FINDINGS ADD COLUMN IF NOT EXISTS LAST_SEEN_AT TIMESTAMP_TZ(9)"),
    ("add_findings_last_scan_id",
     "ALTER TABLE FINDINGS ADD COLUMN IF NOT EXISTS LAST_SCAN_ID VARCHAR(36)"),
    ("add_findings_reopened_count",
     "ALTER TABLE FINDINGS ADD COLUMN IF NOT EXISTS REOPENED_COUNT NUMBER(38,0) DEFAULT 0"),
    ("add_findings_current_fail_count",
     "ALTER TABLE FINDINGS ADD COLUMN IF NOT EXISTS CURRENT_FAIL_COUNT NUMBER(38,0)"),
    ("add_findings_current_total_count",
     "ALTER TABLE FINDINGS ADD COLUMN IF NOT EXISTS CURRENT_TOTAL_COUNT NUMBER(38,0)"),
    ("add_findings_fail_history",
     "ALTER TABLE FINDINGS ADD COLUMN IF NOT EXISTS FAIL_HISTORY VARIANT"),
    ("backfill_findings_first_detected_at",
     "UPDATE FINDINGS SET FIRST_DETECTED_AT = DETECTED_AT WHERE FIRST_DETECTED_AT IS NULL"),
    ("backfill_findings_last_seen_at",
     "UPDATE FINDINGS SET LAST_SEEN_AT = COALESCE(UPDATED_AT, DETECTED_AT) WHERE LAST_SEEN_AT IS NULL"),
    ("backfill_findings_last_scan_id",
     "UPDATE FINDINGS SET LAST_SCAN_ID = SCAN_ID WHERE LAST_SCAN_ID IS NULL"),
    ("create_mutes",
     """
     CREATE TABLE IF NOT EXISTS MUTES (
         ID          VARCHAR(36) NOT NULL PRIMARY KEY,
         INSTANCE_ID VARCHAR(36) NOT NULL,
         ASSET_ID    VARCHAR(36) NOT NULL,
         MUTED_UNTIL TIMESTAMP_TZ(9) NOT NULL,
         REASON      VARCHAR(16777216),
         MUTED_BY    VARCHAR(255),
         CREATED_AT  TIMESTAMP_TZ(9) NOT NULL DEFAULT CURRENT_TIMESTAMP()
     )
     """),
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
