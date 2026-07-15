"""
Seed 3 domain-diverse dirty test tables in PLAYGROUND_DB.TEST_DQ.

Each table has deliberate, documented violations so we can validate that
Claude proposes the right checks and finds the right violations end-to-end.
Drops any existing versions first so re-runs are idempotent.

Tables:
  ORDERS               — e-commerce fact-shaped
                         (dup PK, nullable FK, negative total, stale date, bad status/currency)
  EMPLOYEE_ATTENDANCE  — HR self-referential + cross-field constraint
                         (CHECK_OUT < CHECK_IN, HOURS > 24, nullable EMPLOYEE_ID)
  SUBSCRIPTIONS        — SaaS date-range + enum
                         (END < START, bad PLAN_TIER, zero price, bad EMAIL, bool-as-VARCHAR)
"""
import logging
import sys

from app.services.snowflake_session import session as sf_session

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_DDL = [
    # ── ORDERS ─────────────────────────────────────────────────────────────
    "DROP TABLE IF EXISTS PLAYGROUND_DB.TEST_DQ.ORDERS",
    """
    CREATE TABLE PLAYGROUND_DB.TEST_DQ.ORDERS (
        ORDER_ID       NUMBER(10,0)  NOT NULL,
        CUSTOMER_ID    NUMBER(10,0),
        ORDER_DATE     DATE,
        STATUS         VARCHAR(20),
        TOTAL_AMOUNT   NUMBER(12,2),
        CURRENCY_CODE  VARCHAR(3),
        CREATED_AT     TIMESTAMP_NTZ
    ) COMMENT = 'E-commerce order transactions (test dataset with deliberate DQ violations)'
    """,

    # ── EMPLOYEE_ATTENDANCE ────────────────────────────────────────────────
    "DROP TABLE IF EXISTS PLAYGROUND_DB.TEST_DQ.EMPLOYEE_ATTENDANCE",
    """
    CREATE TABLE PLAYGROUND_DB.TEST_DQ.EMPLOYEE_ATTENDANCE (
        RECORD_ID       NUMBER(10,0)   NOT NULL,
        EMPLOYEE_ID     NUMBER(10,0),
        CHECK_IN_TS     TIMESTAMP_NTZ,
        CHECK_OUT_TS    TIMESTAMP_NTZ,
        HOURS_WORKED    NUMBER(5,2),
        MANAGER_ID      NUMBER(10,0),
        DEPT_CODE       VARCHAR(10),
        CREATED_AT      TIMESTAMP_NTZ
    ) COMMENT = 'Employee daily attendance log (test dataset with deliberate DQ violations)'
    """,

    # ── SUBSCRIPTIONS ──────────────────────────────────────────────────────
    "DROP TABLE IF EXISTS PLAYGROUND_DB.TEST_DQ.SUBSCRIPTIONS",
    """
    CREATE TABLE PLAYGROUND_DB.TEST_DQ.SUBSCRIPTIONS (
        SUB_ID         NUMBER(10,0)  NOT NULL,
        USER_EMAIL     VARCHAR(200),
        PLAN_TIER      VARCHAR(20),
        START_DATE     DATE,
        END_DATE       DATE,
        MONTHLY_PRICE  NUMBER(8,2),
        AUTO_RENEW     VARCHAR(5),
        CREATED_AT     TIMESTAMP_NTZ
    ) COMMENT = 'SaaS subscription records (test dataset with deliberate DQ violations)'
    """,
]

_SEED = [
    # ── ORDERS ─────────────────────────────────────────────────────────────
    #  Clean rows                                                Bad rows to catch
    #  1001, cust 501, 2026-07-10, PROCESSING, 129.99, USD       —
    #  1002, cust 502, 2026-07-11, SHIPPED,    89.50,  USD       —
    #  1003, cust 503, 2026-07-12, DELIVERED,  245.00, EUR       —
    #  1004, cust 504, 2026-07-13, PROCESSING, 12.99,  GBP       —
    #  1005, cust 505, 2026-07-14, DELIVERED,  1499.99,USD       —
    #  1005 (DUP PK)      cust 506, 2026-07-01, SHIPPED, 55.00,  USD    ← DUP order_id
    #  1006, NULL   ,     2026-07-08, PROCESSING, 78.25,  USD          ← nullable FK
    #  1007, cust 507,    2024-01-15, DELIVERED,  99.00,  USD          ← stale ORDER_DATE
    #  1008, cust 508,    2026-07-09, PENDING2, 65.00,  USD            ← invalid STATUS
    #  1009, cust 509,    2026-07-10, DELIVERED, -50.00, USD           ← negative TOTAL
    #  1010, cust 510,    2026-07-11, PROCESSING, 199.00, XX9          ← invalid CURRENCY
    """
    INSERT INTO PLAYGROUND_DB.TEST_DQ.ORDERS
      (ORDER_ID, CUSTOMER_ID, ORDER_DATE, STATUS, TOTAL_AMOUNT, CURRENCY_CODE, CREATED_AT)
    VALUES
      (1001, 501, '2026-07-10'::DATE, 'PROCESSING', 129.99,  'USD', CURRENT_TIMESTAMP()),
      (1002, 502, '2026-07-11'::DATE, 'SHIPPED',     89.50,  'USD', CURRENT_TIMESTAMP()),
      (1003, 503, '2026-07-12'::DATE, 'DELIVERED',  245.00,  'EUR', CURRENT_TIMESTAMP()),
      (1004, 504, '2026-07-13'::DATE, 'PROCESSING',  12.99,  'GBP', CURRENT_TIMESTAMP()),
      (1005, 505, '2026-07-14'::DATE, 'DELIVERED', 1499.99,  'USD', CURRENT_TIMESTAMP()),
      (1005, 506, '2026-07-01'::DATE, 'SHIPPED',     55.00,  'USD', CURRENT_TIMESTAMP()),
      (1006, NULL,'2026-07-08'::DATE, 'PROCESSING',  78.25,  'USD', CURRENT_TIMESTAMP()),
      (1007, 507, '2024-01-15'::DATE, 'DELIVERED',   99.00,  'USD', CURRENT_TIMESTAMP()),
      (1008, 508, '2026-07-09'::DATE, 'PENDING2',    65.00,  'USD', CURRENT_TIMESTAMP()),
      (1009, 509, '2026-07-10'::DATE, 'DELIVERED',  -50.00,  'USD', CURRENT_TIMESTAMP()),
      (1010, 510, '2026-07-11'::DATE, 'PROCESSING', 199.00,  'XX9', CURRENT_TIMESTAMP()),
      (1011, 511, '2026-07-12'::DATE, 'PROCESSING',  22.50,  'USD', CURRENT_TIMESTAMP()),
      (1012, 512, '2026-07-13'::DATE, 'SHIPPED',    340.00,  'USD', CURRENT_TIMESTAMP()),
      (1013, 513, '2026-07-14'::DATE, 'DELIVERED',   17.99,  'EUR', CURRENT_TIMESTAMP()),
      (1014, 514, '2026-07-14'::DATE, 'PROCESSING', 550.00,  'GBP', CURRENT_TIMESTAMP())
    """,

    # ── EMPLOYEE_ATTENDANCE ────────────────────────────────────────────────
    #  Bad rows:
    #    RECORD_ID=5:  CHECK_OUT < CHECK_IN (impossible)
    #    RECORD_ID=6:  HOURS_WORKED = 27  (impossible >24)
    #    RECORD_ID=7:  EMPLOYEE_ID = NULL (broken PK-like column)
    #    RECORD_ID=8:  MANAGER_ID = 9999 (orphan — no employee with that ID exists in this small set)
    #    RECORD_ID=9:  CHECK_OUT_TS = CHECK_IN_TS (0 hours worked but recorded 8)
    """
    INSERT INTO PLAYGROUND_DB.TEST_DQ.EMPLOYEE_ATTENDANCE
      (RECORD_ID, EMPLOYEE_ID, CHECK_IN_TS, CHECK_OUT_TS, HOURS_WORKED, MANAGER_ID, DEPT_CODE, CREATED_AT)
    VALUES
      (1, 101, '2026-07-14 08:00:00'::TIMESTAMP_NTZ, '2026-07-14 17:00:00'::TIMESTAMP_NTZ,  8.5, 501, 'ENG', CURRENT_TIMESTAMP()),
      (2, 102, '2026-07-14 09:00:00'::TIMESTAMP_NTZ, '2026-07-14 18:00:00'::TIMESTAMP_NTZ,  8.0, 501, 'ENG', CURRENT_TIMESTAMP()),
      (3, 103, '2026-07-14 08:30:00'::TIMESTAMP_NTZ, '2026-07-14 16:30:00'::TIMESTAMP_NTZ,  7.5, 502, 'SALES', CURRENT_TIMESTAMP()),
      (4, 104, '2026-07-14 07:45:00'::TIMESTAMP_NTZ, '2026-07-14 16:15:00'::TIMESTAMP_NTZ,  8.0, 502, 'SALES', CURRENT_TIMESTAMP()),
      (5, 105, '2026-07-14 09:00:00'::TIMESTAMP_NTZ, '2026-07-14 07:00:00'::TIMESTAMP_NTZ,  8.0, 503, 'OPS',   CURRENT_TIMESTAMP()),
      (6, 106, '2026-07-14 06:00:00'::TIMESTAMP_NTZ, '2026-07-15 09:00:00'::TIMESTAMP_NTZ, 27.0, 503, 'OPS',   CURRENT_TIMESTAMP()),
      (7, NULL,'2026-07-14 08:00:00'::TIMESTAMP_NTZ, '2026-07-14 16:00:00'::TIMESTAMP_NTZ,  8.0, 504, 'ENG',   CURRENT_TIMESTAMP()),
      (8, 108, '2026-07-14 09:00:00'::TIMESTAMP_NTZ, '2026-07-14 17:00:00'::TIMESTAMP_NTZ,  8.0, 9999,'ENG',  CURRENT_TIMESTAMP()),
      (9, 109, '2026-07-14 08:00:00'::TIMESTAMP_NTZ, '2026-07-14 08:00:00'::TIMESTAMP_NTZ,  8.0, 501, 'ENG',   CURRENT_TIMESTAMP()),
      (10,110, '2026-07-13 08:00:00'::TIMESTAMP_NTZ, '2026-07-13 16:30:00'::TIMESTAMP_NTZ,  8.5, 501, 'ENG',   CURRENT_TIMESTAMP()),
      (11,111, '2026-07-13 09:00:00'::TIMESTAMP_NTZ, '2026-07-13 18:00:00'::TIMESTAMP_NTZ,  8.0, 502, 'SALES', CURRENT_TIMESTAMP()),
      (12,112, '2026-07-13 08:30:00'::TIMESTAMP_NTZ, '2026-07-13 16:30:00'::TIMESTAMP_NTZ,  7.5, 503, 'OPS',   CURRENT_TIMESTAMP())
    """,

    # ── SUBSCRIPTIONS ──────────────────────────────────────────────────────
    # Bad rows:
    #   SUB_ID=105: END_DATE < START_DATE
    #   SUB_ID=106: PLAN_TIER = 'PLATINUM' (not in {FREE, BASIC, PRO, ENTERPRISE})
    #   SUB_ID=107: MONTHLY_PRICE = 0 (invalid for non-FREE tier)
    #   SUB_ID=108: USER_EMAIL = 'bob at company' (malformed — no @ or .)
    #   AUTO_RENEW is VARCHAR — every row has 'yes' or 'no' (should be BOOLEAN)
    """
    INSERT INTO PLAYGROUND_DB.TEST_DQ.SUBSCRIPTIONS
      (SUB_ID, USER_EMAIL, PLAN_TIER, START_DATE, END_DATE, MONTHLY_PRICE, AUTO_RENEW, CREATED_AT)
    VALUES
      (101, 'alice@example.com',  'PRO',        '2026-01-01'::DATE, '2027-01-01'::DATE,  49.99, 'yes', CURRENT_TIMESTAMP()),
      (102, 'bob@company.io',     'BASIC',      '2026-02-01'::DATE, '2027-02-01'::DATE,  19.99, 'yes', CURRENT_TIMESTAMP()),
      (103, 'carol@dept.gov',     'ENTERPRISE', '2026-03-01'::DATE, '2027-03-01'::DATE, 299.00, 'no',  CURRENT_TIMESTAMP()),
      (104, 'dave@example.com',   'FREE',       '2026-04-01'::DATE, '2027-04-01'::DATE,   0.00, 'yes', CURRENT_TIMESTAMP()),
      (105, 'eve@example.com',    'PRO',        '2026-05-01'::DATE, '2026-04-01'::DATE,  49.99, 'no',  CURRENT_TIMESTAMP()),
      (106, 'frank@example.com',  'PLATINUM',   '2026-06-01'::DATE, '2027-06-01'::DATE, 499.00, 'yes', CURRENT_TIMESTAMP()),
      (107, 'grace@example.com',  'PRO',        '2026-07-01'::DATE, '2027-07-01'::DATE,   0.00, 'yes', CURRENT_TIMESTAMP()),
      (108, 'bob at company',     'BASIC',      '2026-01-15'::DATE, '2027-01-15'::DATE,  19.99, 'no',  CURRENT_TIMESTAMP()),
      (109, 'ivy@example.com',    'BASIC',      '2026-02-15'::DATE, '2027-02-15'::DATE,  19.99, 'yes', CURRENT_TIMESTAMP()),
      (110, 'julia@example.com',  'PRO',        '2026-03-15'::DATE, '2027-03-15'::DATE,  49.99, 'no',  CURRENT_TIMESTAMP()),
      (111, 'kim@example.com',    'FREE',       '2026-04-15'::DATE, '2027-04-15'::DATE,   0.00, 'yes', CURRENT_TIMESTAMP()),
      (112, 'liam@example.com',   'PRO',        '2026-05-15'::DATE, '2027-05-15'::DATE,  49.99, 'yes', CURRENT_TIMESTAMP())
    """,
]


def main() -> int:
    logger.info("Creating test tables in PLAYGROUND_DB.TEST_DQ")
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
    for tbl in ("ORDERS", "EMPLOYEE_ATTENDANCE", "SUBSCRIPTIONS"):
        rows = sf_session.query(f"SELECT COUNT(*) AS N FROM PLAYGROUND_DB.TEST_DQ.{tbl}")
        logger.info(f"  PLAYGROUND_DB.TEST_DQ.{tbl}: {rows[0].get('N')} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
