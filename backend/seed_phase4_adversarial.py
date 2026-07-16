"""
Phase 4 — adversarial edge-case test tables. Targets known gaps from
TEST_FINDINGS.md and shapes not covered by the phase 1-3 tables:

  EDGE_CASE_VALUES  — unicode/whitespace-padded strings, an all-NULL column,
                      a boolean-as-VARCHAR column WITHOUT the _FL/_FLAG/_YN/
                      IS_/_IND suffix (gap #2 in TEST_FINDINGS.md — the
                      boolean_stored_as_varchar handler heuristic requires
                      that suffix and should miss this one), and a
                      numeric-looking VARCHAR column with a few garbage rows.
  SINGLE_ROW_CONFIG — exactly 1 row. Verify profiler/RuleIntelligence don't
                      choke on distinct=1-for-everything and don't propose
                      nonsense (e.g. uniqueness "violations" from n=1).
  ORG_HIERARCHY     — self-referential FK (MANAGER_ID -> EMPLOYEE_ID within
                      the same table), 20 rows so RelationshipDiscovery has
                      a real shot at it. Re-tests gap #4 (self-ref FK miss
                      seen on EMPLOYEE_ATTENDANCE last session) with more
                      rows and a dedicated table instead of a side column.
"""
import logging
import sys

from app.services.snowflake_session import session as sf_session

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


_DDL = [
    # ── EDGE_CASE_VALUES ──────────────────────────────────────────────────
    "DROP TABLE IF EXISTS PLAYGROUND_DB.TEST_DQ.EDGE_CASE_VALUES",
    """
    CREATE TABLE PLAYGROUND_DB.TEST_DQ.EDGE_CASE_VALUES (
        ID                 NUMBER(10,0) NOT NULL,
        DESCRIPTION        VARCHAR(500),
        ALWAYS_NULL_COL    VARCHAR(100),
        NOTIFICATIONS_ON   VARCHAR(10),
        MOSTLY_NUMERIC_STR VARCHAR(50),
        CREATED_AT         TIMESTAMP_NTZ
    ) COMMENT = 'Adversarial edge cases: unicode/whitespace, all-null column, suffix-less boolean-as-varchar, mostly-numeric string column'
    """,

    # ── SINGLE_ROW_CONFIG ─────────────────────────────────────────────────
    "DROP TABLE IF EXISTS PLAYGROUND_DB.TEST_DQ.SINGLE_ROW_CONFIG",
    """
    CREATE TABLE PLAYGROUND_DB.TEST_DQ.SINGLE_ROW_CONFIG (
        CONFIG_ID      NUMBER(10,0) NOT NULL,
        MAX_RETRIES    NUMBER(5,0),
        FEATURE_FLAG   VARCHAR(20),
        UPDATED_AT     TIMESTAMP_NTZ
    ) COMMENT = 'Singleton app config row — exactly 1 row, always'
    """,

    # ── ORG_HIERARCHY ─────────────────────────────────────────────────────
    "DROP TABLE IF EXISTS PLAYGROUND_DB.TEST_DQ.ORG_HIERARCHY",
    """
    CREATE TABLE PLAYGROUND_DB.TEST_DQ.ORG_HIERARCHY (
        EMPLOYEE_ID    NUMBER(10,0) NOT NULL,
        MANAGER_ID     NUMBER(10,0),
        EMPLOYEE_NAME  VARCHAR(100),
        DEPT_CODE      VARCHAR(10),
        CREATED_AT     TIMESTAMP_NTZ
    ) COMMENT = 'Org chart — MANAGER_ID self-referential FK to EMPLOYEE_ID in this same table'
    """,
]


_SEED = [
    # ── EDGE_CASE_VALUES: 18 rows ──────────────────────────────────────────
    # Violations seeded:
    #   - DESCRIPTION: leading/trailing whitespace (rows 3,7), unicode (café,
    #     naïve, 日本語, emoji) (rows 5,9,12), empty string vs NULL (row 15)
    #   - ALWAYS_NULL_COL: 100% NULL across all 18 rows (deliberate)
    #   - NOTIFICATIONS_ON: boolean-shaped values ('true'/'false'/'Y'/'N'/
    #     mixed case) but column name has NONE of the recognized suffixes
    #     (_FL, _FLAG, _YN, IS_, _IND) — should NOT be caught by the
    #     existing boolean_stored_as_varchar heuristic
    #   - MOSTLY_NUMERIC_STR: numeric-looking strings with 2 garbage rows
    #     ('N/A' row 8, '12.5.3' malformed-decimal row 14)
    """
    INSERT INTO PLAYGROUND_DB.TEST_DQ.EDGE_CASE_VALUES
      (ID, DESCRIPTION, ALWAYS_NULL_COL, NOTIFICATIONS_ON, MOSTLY_NUMERIC_STR, CREATED_AT)
    VALUES
      (1,  'Standard widget',             NULL, 'true',  '100',    CURRENT_TIMESTAMP()),
      (2,  'Blue gadget',                 NULL, 'false', '250',    CURRENT_TIMESTAMP()),
      (3,  '  padded both sides  ',       NULL, 'Y',     '75',     CURRENT_TIMESTAMP()),
      (4,  'Regular item',                NULL, 'N',     '300',    CURRENT_TIMESTAMP()),
      (5,  'Café special',                NULL, 'true',  '150',    CURRENT_TIMESTAMP()),
      (6,  'Basic component',             NULL, 'False', '80',     CURRENT_TIMESTAMP()),
      (7,  'trailing space   ',           NULL, 'TRUE',  '220',    CURRENT_TIMESTAMP()),
      (8,  'Widget assembly',             NULL, 'true',  'N/A',    CURRENT_TIMESTAMP()),
      (9,  'naïve approach kit',          NULL, 'false', '95',     CURRENT_TIMESTAMP()),
      (10, 'Standard part',               NULL, 'Y',     '410',    CURRENT_TIMESTAMP()),
      (11, 'Compact unit',                NULL, 'N',     '60',     CURRENT_TIMESTAMP()),
      (12, '日本語のラベル',                 NULL, 'true',  '175',    CURRENT_TIMESTAMP()),
      (13, 'Deluxe model',                NULL, 'false', '500',    CURRENT_TIMESTAMP()),
      (14, 'Malformed number test',       NULL, 'true',  '12.5.3', CURRENT_TIMESTAMP()),
      (15, '',                            NULL, 'false', '30',     CURRENT_TIMESTAMP()),
      (16, 'Premium kit ✅',               NULL, 'Y',     '640',    CURRENT_TIMESTAMP()),
      (17, 'Economy pack',                NULL, 'N',     '45',     CURRENT_TIMESTAMP()),
      (18, 'Final test row',              NULL, 'true',  '200',    CURRENT_TIMESTAMP())
    """,

    # ── SINGLE_ROW_CONFIG: exactly 1 row ────────────────────────────────────
    """
    INSERT INTO PLAYGROUND_DB.TEST_DQ.SINGLE_ROW_CONFIG
      (CONFIG_ID, MAX_RETRIES, FEATURE_FLAG, UPDATED_AT)
    VALUES
      (1, 3, 'ENABLED', CURRENT_TIMESTAMP())
    """,

    # ── ORG_HIERARCHY: 20 rows, one CEO (NULL manager), rest form a tree ───
    # Violation seeded: row 20 has MANAGER_ID=999 which does not exist in
    # EMPLOYEE_ID — an orphan self-referential FK, deliberately.
    """
    INSERT INTO PLAYGROUND_DB.TEST_DQ.ORG_HIERARCHY
      (EMPLOYEE_ID, MANAGER_ID, EMPLOYEE_NAME, DEPT_CODE, CREATED_AT)
    VALUES
      (1,  NULL, 'Alice CEO',        'EXEC', CURRENT_TIMESTAMP()),
      (2,  1,    'Bob VP Eng',       'ENG',  CURRENT_TIMESTAMP()),
      (3,  1,    'Carol VP Sales',   'SALES',CURRENT_TIMESTAMP()),
      (4,  2,    'Dave Mgr',         'ENG',  CURRENT_TIMESTAMP()),
      (5,  2,    'Eve Mgr',          'ENG',  CURRENT_TIMESTAMP()),
      (6,  3,    'Frank Mgr',        'SALES',CURRENT_TIMESTAMP()),
      (7,  4,    'Grace Eng',        'ENG',  CURRENT_TIMESTAMP()),
      (8,  4,    'Heidi Eng',        'ENG',  CURRENT_TIMESTAMP()),
      (9,  5,    'Ivan Eng',         'ENG',  CURRENT_TIMESTAMP()),
      (10, 5,    'Judy Eng',         'ENG',  CURRENT_TIMESTAMP()),
      (11, 6,    'Karl Sales',       'SALES',CURRENT_TIMESTAMP()),
      (12, 6,    'Liam Sales',       'SALES',CURRENT_TIMESTAMP()),
      (13, 7,    'Mona Eng',         'ENG',  CURRENT_TIMESTAMP()),
      (14, 7,    'Nate Eng',         'ENG',  CURRENT_TIMESTAMP()),
      (15, 8,    'Olga Eng',         'ENG',  CURRENT_TIMESTAMP()),
      (16, 9,    'Paul Eng',         'ENG',  CURRENT_TIMESTAMP()),
      (17, 10,   'Quinn Eng',        'ENG',  CURRENT_TIMESTAMP()),
      (18, 11,   'Rita Sales',       'SALES',CURRENT_TIMESTAMP()),
      (19, 12,   'Sam Sales',        'SALES',CURRENT_TIMESTAMP()),
      (20, 999,  'Orphan Employee',  'ENG',  CURRENT_TIMESTAMP())
    """,
]


def main() -> int:
    logger.info("Creating phase 4 adversarial test tables in PLAYGROUND_DB.TEST_DQ")
    for stmt in _DDL:
        stripped = " ".join(stmt.strip().split()[:6])
        logger.info(f"  {stripped}...")
        sf_session.execute(stmt)

    logger.info("Seeding data")
    for stmt in _SEED:
        which = stmt.strip().split("\n")[1].strip().split(".")[-1].strip()
        logger.info(f"  seeding {which}...")
        sf_session.execute(stmt)

    logger.info("Verifying row counts")
    for tbl in ("EDGE_CASE_VALUES", "SINGLE_ROW_CONFIG", "ORG_HIERARCHY"):
        rows = sf_session.query(f"SELECT COUNT(*) AS N FROM PLAYGROUND_DB.TEST_DQ.{tbl}")
        logger.info(f"  PLAYGROUND_DB.TEST_DQ.{tbl}: {rows[0].get('N')} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
