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
    # ── Anomaly Detection Tier A ─────────────────────────────────────────
    # Per-scan longitudinal metric captures + rolling MAD baselines. Feeds
    # metric_anomaly / metric_relative_change / category_disappeared rule
    # shapes and RuleIntelligence auto-proposal once sample_count >= 14.
    ("create_metric_snapshots",
     """
     CREATE TABLE IF NOT EXISTS METRIC_SNAPSHOTS (
         ID              VARCHAR(36) NOT NULL PRIMARY KEY,
         SCAN_ID         VARCHAR(36) NOT NULL,
         ASSET_ID        VARCHAR(36) NOT NULL,
         DATABASE_NAME   VARCHAR(255),
         SCHEMA_NAME     VARCHAR(255),
         TABLE_NAME      VARCHAR(255),
         COLUMN_NAME     VARCHAR(255),
         METRIC_NAME     VARCHAR(100) NOT NULL,
         METRIC_VALUE    FLOAT,
         METRIC_META     VARIANT,
         CAPTURED_AT     TIMESTAMP_TZ(9) DEFAULT CURRENT_TIMESTAMP()
     )
     """),
    ("create_metric_baselines",
     """
     CREATE TABLE IF NOT EXISTS METRIC_BASELINES (
         ID              VARCHAR(36) NOT NULL PRIMARY KEY,
         ASSET_ID        VARCHAR(36) NOT NULL,
         COLUMN_NAME     VARCHAR(255),
         METRIC_NAME     VARCHAR(100) NOT NULL,
         MEDIAN_VALUE    FLOAT,
         MAD_VALUE       FLOAT,
         SAMPLE_COUNT    NUMBER(38,0) DEFAULT 0,
         OBSERVED_SET    VARIANT,
         WINDOW_START    TIMESTAMP_TZ(9),
         WINDOW_END      TIMESTAMP_TZ(9),
         UPDATED_AT      TIMESTAMP_TZ(9) DEFAULT CURRENT_TIMESTAMP()
     )
     """),
    # Anomaly proposals surfaced from scheduled runs (agentic runs use the
    # normal inline RULE_INSTANCES flow with status='pending'). Each row is
    # an actionable proposal a user can approve/reject via the notifications
    # inbox. Status: pending | approved | rejected | superseded.
    ("create_pending_proposals",
     """
     CREATE TABLE IF NOT EXISTS PENDING_PROPOSALS (
         ID               VARCHAR(36) NOT NULL PRIMARY KEY,
         KIND             VARCHAR(50) NOT NULL,
         ASSET_ID         VARCHAR(36),
         DATABASE_NAME    VARCHAR(255),
         SCHEMA_NAME      VARCHAR(255),
         TABLE_NAME       VARCHAR(255),
         COLUMN_NAME      VARCHAR(255),
         TEMPLATE_SHAPE   VARCHAR(100),
         METRIC_NAME      VARCHAR(100),
         TARGET_CONFIG    VARIANT,
         THRESHOLD_CONFIG VARIANT,
         SEVERITY         VARCHAR(20),
         RATIONALE        VARCHAR(2000),
         EVIDENCE         VARIANT,
         STATUS           VARCHAR(20) DEFAULT 'pending',
         SOURCE_RUN_ID    VARCHAR(36),
         SOURCE_SCAN_ID   VARCHAR(36),
         SCHEDULE_ID      VARCHAR(36),
         DECISION_REASON  VARCHAR(2000),
         DECIDED_BY       VARCHAR(255),
         DECIDED_AT       TIMESTAMP_TZ(9),
         CREATED_AT       TIMESTAMP_TZ(9) DEFAULT CURRENT_TIMESTAMP(),
         INSTANCE_ID      VARCHAR(36)
     )
     """),
    # Dashboard notifications inbox. Points at a resource (proposal, finding,
    # etc.) via KIND + REF_ID so the same inbox can hold future event types.
    ("create_notifications",
     """
     CREATE TABLE IF NOT EXISTS NOTIFICATIONS (
         ID           VARCHAR(36) NOT NULL PRIMARY KEY,
         KIND         VARCHAR(50) NOT NULL,
         TITLE        VARCHAR(500) NOT NULL,
         BODY         VARCHAR(2000),
         REF_TABLE    VARCHAR(100),
         REF_ID       VARCHAR(36),
         SEVERITY     VARCHAR(20),
         READ_AT      TIMESTAMP_TZ(9),
         CREATED_AT   TIMESTAMP_TZ(9) DEFAULT CURRENT_TIMESTAMP()
     )
     """),
    ("create_maintenance_proposals",
     """
     CREATE TABLE IF NOT EXISTS MAINTENANCE_PROPOSALS (
         ID               VARCHAR(36) NOT NULL PRIMARY KEY,
         INSTANCE_ID      VARCHAR(36) NOT NULL,
         ACTION           VARCHAR(30) NOT NULL,
         REASON           VARCHAR(2000),
         EVIDENCE         VARIANT,
         STATUS           VARCHAR(20) DEFAULT 'pending',
         DECISION_REASON  VARCHAR(2000),
         DECIDED_BY       VARCHAR(255),
         DECIDED_AT       TIMESTAMP_TZ(9),
         CREATED_AT       TIMESTAMP_TZ(9) DEFAULT CURRENT_TIMESTAMP()
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
            WAREHOUSE = {warehouse}
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
    (
        "create_rule_chats",
        """
        CREATE TABLE IF NOT EXISTS RULE_CHATS (
            ID          VARCHAR(36)   NOT NULL,
            TITLE       VARCHAR(255),
            MESSAGES    VARIANT       NOT NULL,
            CREATED_BY  VARCHAR(255),
            CREATED_AT  TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
            UPDATED_AT  TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
            PRIMARY KEY (ID)
        )
        """,
    ),
]


def run_migrations() -> None:
    # Interpolate connection-scoped settings (warehouse) into templated
    # migrations so we don't hardcode a warehouse name that may not exist
    # for the current role.
    from app.core.config import settings
    warehouse = (settings.SNOWFLAKE_WAREHOUSE or "").strip() or "COMPUTE_WH"
    for name, sql in _MIGRATIONS:
        try:
            rendered = sql.strip().format(warehouse=warehouse) if "{warehouse}" in sql else sql.strip()
        except Exception:
            rendered = sql.strip()
        try:
            sf_session.execute(rendered)
            logger.info(f"[Migrations] {name}: ok")
        except Exception as e:
            logger.warning(f"[Migrations] {name}: skipped — {e}")
