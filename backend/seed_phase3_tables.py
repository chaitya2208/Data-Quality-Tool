"""
Phase 3 test tables — extending the seed to stress-test different scenarios.

Tables:
  CLEAN_CUSTOMERS   — no deliberate violations. Verify Claude proposes
                      preventive checks without any violation firing.
  COUNTRIES         — reference/lookup dimension (ISO codes). Verify Claude
                      classifies as `reference` and does NOT propose freshness
                      (static tables shouldn't be checked for staleness).
  WIDE_TRANSACTIONS — 25 columns of mixed types (numeric, text, dates,
                      booleans, ids). Verify large-context handling and
                      per-column-type appropriate proposals.
"""
import logging
import sys

from app.services.snowflake_session import session as sf_session

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


_DDL = [
    # ── CLEAN_CUSTOMERS — deliberately no violations ─────────────────────
    "DROP TABLE IF EXISTS PLAYGROUND_DB.TEST_DQ.CLEAN_CUSTOMERS",
    """
    CREATE TABLE PLAYGROUND_DB.TEST_DQ.CLEAN_CUSTOMERS (
        CUSTOMER_ID     NUMBER(10,0) NOT NULL,
        CUSTOMER_NAME   VARCHAR(200) NOT NULL,
        CUSTOMER_TIER   VARCHAR(20)  NOT NULL,
        SIGNUP_DATE     DATE         NOT NULL,
        LIFETIME_VALUE  NUMBER(12,2),
        IS_ACTIVE       BOOLEAN      NOT NULL,
        CREATED_AT      TIMESTAMP_NTZ NOT NULL
    ) COMMENT = 'Clean customer master data — deliberately no DQ violations'
    """,

    # ── COUNTRIES — reference/lookup dimension ───────────────────────────
    "DROP TABLE IF EXISTS PLAYGROUND_DB.TEST_DQ.COUNTRIES",
    """
    CREATE TABLE PLAYGROUND_DB.TEST_DQ.COUNTRIES (
        COUNTRY_CODE     VARCHAR(2)    NOT NULL,
        COUNTRY_NAME     VARCHAR(100)  NOT NULL,
        CURRENCY_CODE    VARCHAR(3)    NOT NULL,
        CONTINENT        VARCHAR(20)   NOT NULL
    ) COMMENT = 'ISO 3166-1 alpha-2 country reference lookup — static reference data'
    """,

    # ── WIDE_TRANSACTIONS — 25 columns for large-context testing ─────────
    "DROP TABLE IF EXISTS PLAYGROUND_DB.TEST_DQ.WIDE_TRANSACTIONS",
    """
    CREATE TABLE PLAYGROUND_DB.TEST_DQ.WIDE_TRANSACTIONS (
        TXN_ID              NUMBER(12,0) NOT NULL,
        ACCOUNT_ID          NUMBER(10,0),
        MERCHANT_ID         NUMBER(10,0),
        TXN_TYPE            VARCHAR(20),
        TXN_STATUS          VARCHAR(20),
        AMOUNT              NUMBER(14,4),
        FEE                 NUMBER(10,4),
        TAX                 NUMBER(10,4),
        NET_AMOUNT          NUMBER(14,4),
        CURRENCY_CODE       VARCHAR(3),
        EXCHANGE_RATE       NUMBER(10,6),
        AMOUNT_USD          NUMBER(14,4),
        TXN_TS              TIMESTAMP_NTZ,
        SETTLEMENT_DATE     DATE,
        AUTHORIZATION_CODE  VARCHAR(30),
        CARD_LAST4          VARCHAR(4),
        CARD_BRAND          VARCHAR(20),
        COUNTRY_CODE        VARCHAR(2),
        IP_ADDRESS          VARCHAR(45),
        USER_AGENT          VARCHAR(500),
        RISK_SCORE          NUMBER(5,2),
        IS_DISPUTED         BOOLEAN,
        IS_REFUNDED         BOOLEAN,
        NOTES               VARCHAR(1000),
        CREATED_AT          TIMESTAMP_NTZ
    ) COMMENT = 'Payment transactions — wide fact table (25 columns, mixed types) for testing large-context handling'
    """,
]


_SEED = [
    # ── CLEAN_CUSTOMERS: 15 clean rows, no violations ────────────────────
    """
    INSERT INTO PLAYGROUND_DB.TEST_DQ.CLEAN_CUSTOMERS
      (CUSTOMER_ID, CUSTOMER_NAME, CUSTOMER_TIER, SIGNUP_DATE, LIFETIME_VALUE, IS_ACTIVE, CREATED_AT)
    VALUES
      (1, 'Acme Corp',            'ENTERPRISE', '2024-01-15'::DATE,  125000.00, TRUE,  CURRENT_TIMESTAMP()),
      (2, 'Globex LLC',           'ENTERPRISE', '2024-02-20'::DATE,   89500.50, TRUE,  CURRENT_TIMESTAMP()),
      (3, 'Wayne Enterprises',    'PRO',        '2024-03-10'::DATE,   34200.00, TRUE,  CURRENT_TIMESTAMP()),
      (4, 'Stark Industries',     'ENTERPRISE', '2024-04-01'::DATE,  210500.75, TRUE,  CURRENT_TIMESTAMP()),
      (5, 'Umbrella Corp',        'PRO',        '2024-05-15'::DATE,   45300.00, TRUE,  CURRENT_TIMESTAMP()),
      (6, 'Cyberdyne Systems',    'BASIC',      '2024-06-20'::DATE,   12100.00, TRUE,  CURRENT_TIMESTAMP()),
      (7, 'Initech',              'BASIC',      '2024-07-01'::DATE,    8750.00, FALSE, CURRENT_TIMESTAMP()),
      (8, 'Aperture Science',     'PRO',        '2024-08-11'::DATE,   67200.00, TRUE,  CURRENT_TIMESTAMP()),
      (9, 'Massive Dynamic',      'ENTERPRISE', '2024-09-05'::DATE,  156800.00, TRUE,  CURRENT_TIMESTAMP()),
      (10,'Rekall Inc',           'FREE',       '2024-10-15'::DATE,       0.00, TRUE,  CURRENT_TIMESTAMP()),
      (11,'Weyland-Yutani',       'ENTERPRISE', '2024-11-22'::DATE,  198000.00, TRUE,  CURRENT_TIMESTAMP()),
      (12,'Tyrell Corp',          'PRO',        '2024-12-01'::DATE,   52400.00, TRUE,  CURRENT_TIMESTAMP()),
      (13,'Oscorp',               'PRO',        '2025-01-05'::DATE,   41200.00, TRUE,  CURRENT_TIMESTAMP()),
      (14,'Buy n Large',          'ENTERPRISE', '2025-02-14'::DATE,  145600.00, TRUE,  CURRENT_TIMESTAMP()),
      (15,'Virtucon',             'BASIC',      '2025-03-01'::DATE,   19500.00, FALSE, CURRENT_TIMESTAMP())
    """,

    # ── COUNTRIES: 20 rows of real ISO codes ─────────────────────────────
    """
    INSERT INTO PLAYGROUND_DB.TEST_DQ.COUNTRIES
      (COUNTRY_CODE, COUNTRY_NAME, CURRENCY_CODE, CONTINENT)
    VALUES
      ('US', 'United States',   'USD', 'North America'),
      ('CA', 'Canada',           'CAD', 'North America'),
      ('MX', 'Mexico',           'MXN', 'North America'),
      ('GB', 'United Kingdom',   'GBP', 'Europe'),
      ('FR', 'France',           'EUR', 'Europe'),
      ('DE', 'Germany',          'EUR', 'Europe'),
      ('IT', 'Italy',            'EUR', 'Europe'),
      ('ES', 'Spain',            'EUR', 'Europe'),
      ('JP', 'Japan',            'JPY', 'Asia'),
      ('CN', 'China',            'CNY', 'Asia'),
      ('IN', 'India',            'INR', 'Asia'),
      ('KR', 'South Korea',      'KRW', 'Asia'),
      ('SG', 'Singapore',        'SGD', 'Asia'),
      ('AU', 'Australia',        'AUD', 'Oceania'),
      ('NZ', 'New Zealand',      'NZD', 'Oceania'),
      ('BR', 'Brazil',           'BRL', 'South America'),
      ('AR', 'Argentina',        'ARS', 'South America'),
      ('ZA', 'South Africa',     'ZAR', 'Africa'),
      ('EG', 'Egypt',            'EGP', 'Africa'),
      ('NG', 'Nigeria',          'NGN', 'Africa')
    """,

    # ── WIDE_TRANSACTIONS: 30 rows with a few deliberate violations ──────
    #   Bad row TXN 105:  AMOUNT != FEE+TAX+NET_AMOUNT sum consistency (subtle)
    #   Bad row TXN 108:  EXCHANGE_RATE=0 (impossible)
    #   Bad row TXN 112:  SETTLEMENT_DATE before TXN_TS (impossible)
    #   Bad row TXN 115:  RISK_SCORE=150 (out of range 0-100)
    #   Bad row TXN 118:  CARD_LAST4='ABCD' (should be 4 digits)
    #   Bad row TXN 121:  IS_DISPUTED=TRUE AND IS_REFUNDED=FALSE + big AMOUNT (business logic)
    """
    INSERT INTO PLAYGROUND_DB.TEST_DQ.WIDE_TRANSACTIONS
      (TXN_ID, ACCOUNT_ID, MERCHANT_ID, TXN_TYPE, TXN_STATUS, AMOUNT, FEE, TAX, NET_AMOUNT,
       CURRENCY_CODE, EXCHANGE_RATE, AMOUNT_USD, TXN_TS, SETTLEMENT_DATE,
       AUTHORIZATION_CODE, CARD_LAST4, CARD_BRAND, COUNTRY_CODE, IP_ADDRESS, USER_AGENT,
       RISK_SCORE, IS_DISPUTED, IS_REFUNDED, NOTES, CREATED_AT)
    VALUES
      (100, 5001, 9001, 'PURCHASE', 'SETTLED', 100.00, 2.90, 8.00, 89.10, 'USD', 1.0,  100.00,  '2026-07-01 10:15:00'::TIMESTAMP_NTZ, '2026-07-03'::DATE, 'AUTH1000', '4242', 'VISA', 'US', '10.0.0.1', 'Mozilla/5.0', 12.5, FALSE, FALSE, NULL, CURRENT_TIMESTAMP()),
      (101, 5002, 9002, 'PURCHASE', 'SETTLED', 250.50, 7.27, 20.00, 223.23, 'USD', 1.0,  250.50, '2026-07-02 11:20:00'::TIMESTAMP_NTZ, '2026-07-04'::DATE, 'AUTH1001', '5555', 'MC',   'CA', '10.0.0.2', 'Mozilla/5.0', 8.0,  FALSE, FALSE, NULL, CURRENT_TIMESTAMP()),
      (102, 5003, 9003, 'REFUND',   'SETTLED', 45.00,  1.30, 3.60, 40.10,  'EUR', 1.07, 48.15,  '2026-07-03 14:00:00'::TIMESTAMP_NTZ, '2026-07-05'::DATE, 'AUTH1002', '3782', 'AMEX', 'FR', '10.0.0.3', 'Chrome/120', 5.5,  FALSE, TRUE,  'refund per request', CURRENT_TIMESTAMP()),
      (103, 5004, 9004, 'PURCHASE', 'SETTLED', 89.99,  2.60, 7.20, 80.19,  'GBP', 1.27, 114.29, '2026-07-04 09:45:00'::TIMESTAMP_NTZ, '2026-07-06'::DATE, 'AUTH1003', '6011', 'DISC', 'GB', '10.0.0.4', 'Firefox/122','15.0', FALSE, FALSE, NULL, CURRENT_TIMESTAMP()),
      (104, 5005, 9005, 'PURCHASE', 'SETTLED', 1200.00, 34.80, 96.00, 1069.20, 'USD', 1.0, 1200.00, '2026-07-05 16:30:00'::TIMESTAMP_NTZ, '2026-07-07'::DATE, 'AUTH1004', '4242', 'VISA', 'US', '10.0.0.5', 'Mozilla/5.0', 45.0, FALSE, FALSE, NULL, CURRENT_TIMESTAMP()),
      (105, 5006, 9006, 'PURCHASE', 'SETTLED', 500.00, 14.50, 40.00, 999.99,  'USD', 1.0, 500.00,  '2026-07-06 12:00:00'::TIMESTAMP_NTZ, '2026-07-08'::DATE, 'AUTH1005', '5555', 'MC',   'US', '10.0.0.6', 'Safari/17', 18.0, FALSE, FALSE, 'net does not match sum', CURRENT_TIMESTAMP()),
      (106, 5007, 9007, 'PURCHASE', 'SETTLED', 75.25,  2.18, 6.02, 67.05,  'USD', 1.0, 75.25,  '2026-07-07 08:00:00'::TIMESTAMP_NTZ, '2026-07-09'::DATE, 'AUTH1006', '4242', 'VISA', 'US', '10.0.0.7', 'Chrome/120', 22.0, FALSE, FALSE, NULL, CURRENT_TIMESTAMP()),
      (107, 5008, 9008, 'PURCHASE', 'SETTLED', 320.00, 9.28, 25.60, 285.12, 'JPY', 0.0067, 2.14, '2026-07-08 13:15:00'::TIMESTAMP_NTZ, '2026-07-10'::DATE, 'AUTH1007', '3782', 'AMEX', 'JP', '10.0.0.8', 'Chrome/120', 10.5, FALSE, FALSE, NULL, CURRENT_TIMESTAMP()),
      (108, 5009, 9009, 'PURCHASE', 'SETTLED', 55.00,  1.60, 4.40, 49.00,  'EUR', 0.0,   0.00,  '2026-07-09 15:30:00'::TIMESTAMP_NTZ, '2026-07-11'::DATE, 'AUTH1008', '5555', 'MC',   'DE', '10.0.0.9', 'Mozilla/5.0', 30.0, FALSE, FALSE, 'zero fx rate', CURRENT_TIMESTAMP()),
      (109, 5010, 9010, 'PURCHASE', 'SETTLED', 178.30, 5.17, 14.26, 158.87, 'USD', 1.0, 178.30, '2026-07-10 11:45:00'::TIMESTAMP_NTZ, '2026-07-12'::DATE, 'AUTH1009', '6011', 'DISC', 'US', '10.0.0.10','Firefox/122', 6.5, FALSE, FALSE, NULL, CURRENT_TIMESTAMP()),
      (110, 5011, 9011, 'REFUND',   'PENDING', 200.00, 0.0,  0.00,  200.00, 'USD', 1.0, 200.00, '2026-07-11 09:00:00'::TIMESTAMP_NTZ, '2026-07-13'::DATE, 'AUTH1010', '4242', 'VISA', 'US', '10.0.0.11','Chrome/120', 40.0, TRUE,  TRUE,  'chargeback dispute', CURRENT_TIMESTAMP()),
      (111, 5012, 9012, 'PURCHASE', 'SETTLED', 62.50,  1.81, 5.00,  55.69,  'CAD', 0.73, 45.63, '2026-07-12 10:30:00'::TIMESTAMP_NTZ, '2026-07-14'::DATE, 'AUTH1011', '3782', 'AMEX', 'CA', '10.0.0.12','Safari/17', 11.0, FALSE, FALSE, NULL, CURRENT_TIMESTAMP()),
      (112, 5013, 9013, 'PURCHASE', 'SETTLED', 89.00,  2.58, 7.12,  79.30,  'USD', 1.0, 89.00,  '2026-07-13 14:00:00'::TIMESTAMP_NTZ, '2026-07-10'::DATE, 'AUTH1012', '5555', 'MC',   'US', '10.0.0.13','Chrome/120', 8.5,  FALSE, FALSE, 'settlement date before txn ts', CURRENT_TIMESTAMP()),
      (113, 5014, 9014, 'PURCHASE', 'SETTLED', 445.00, 12.91, 35.60, 396.49, 'USD', 1.0, 445.00, '2026-07-14 12:15:00'::TIMESTAMP_NTZ, '2026-07-16'::DATE, 'AUTH1013', '4242', 'VISA', 'US', '10.0.0.14','Mozilla/5.0', 25.0, FALSE, FALSE, NULL, CURRENT_TIMESTAMP()),
      (114, 5015, 9015, 'PURCHASE', 'SETTLED', 78.90,  2.29, 6.31,  70.30,  'AUD', 0.66, 52.07, '2026-07-15 08:30:00'::TIMESTAMP_NTZ, '2026-07-17'::DATE, 'AUTH1014', '6011', 'DISC', 'AU', '10.0.0.15','Chrome/120', 14.0, FALSE, FALSE, NULL, CURRENT_TIMESTAMP()),
      (115, 5016, 9016, 'PURCHASE', 'SETTLED', 999.00, 28.97, 79.92, 890.11, 'USD', 1.0, 999.00, '2026-07-15 10:00:00'::TIMESTAMP_NTZ, '2026-07-17'::DATE, 'AUTH1015', '3782', 'AMEX', 'US', '10.0.0.16','Firefox/122', 150.0, FALSE, FALSE, 'risk score out of 0-100 range', CURRENT_TIMESTAMP()),
      (116, 5017, 9017, 'PURCHASE', 'SETTLED', 55.00,  1.60, 4.40, 49.00,  'USD', 1.0, 55.00,  '2026-07-15 11:30:00'::TIMESTAMP_NTZ, '2026-07-17'::DATE, 'AUTH1016', '4242', 'VISA', 'US', '10.0.0.17','Chrome/120', 9.0,  FALSE, FALSE, NULL, CURRENT_TIMESTAMP()),
      (117, 5018, 9018, 'PURCHASE', 'SETTLED', 132.75, 3.85, 10.62, 118.28, 'EUR', 1.07, 142.04,'2026-07-15 13:45:00'::TIMESTAMP_NTZ, '2026-07-17'::DATE, 'AUTH1017', '5555', 'MC',   'IT', '10.0.0.18','Safari/17', 16.5, FALSE, FALSE, NULL, CURRENT_TIMESTAMP()),
      (118, 5019, 9019, 'PURCHASE', 'SETTLED', 210.00, 6.09, 16.80, 187.11, 'USD', 1.0, 210.00, '2026-07-15 14:00:00'::TIMESTAMP_NTZ, '2026-07-17'::DATE, 'AUTH1018', 'ABCD', 'VISA', 'US', '10.0.0.19','Mozilla/5.0', 20.0, FALSE, FALSE, 'card_last4 not digits', CURRENT_TIMESTAMP()),
      (119, 5020, 9020, 'PURCHASE', 'SETTLED', 89.99,  2.61, 7.20, 80.18,  'USD', 1.0, 89.99,  '2026-07-15 15:00:00'::TIMESTAMP_NTZ, '2026-07-17'::DATE, 'AUTH1019', '6011', 'DISC', 'US', '10.0.0.20','Chrome/120', 5.0,  FALSE, FALSE, NULL, CURRENT_TIMESTAMP()),
      (120, 5021, 9021, 'PURCHASE', 'SETTLED', 340.00, 9.86, 27.20, 302.94, 'GBP', 1.27, 431.80,'2026-07-15 16:15:00'::TIMESTAMP_NTZ, '2026-07-17'::DATE, 'AUTH1020', '4242', 'VISA', 'GB', '10.0.0.21','Firefox/122', 28.0, FALSE, FALSE, NULL, CURRENT_TIMESTAMP()),
      (121, 5022, 9022, 'PURCHASE', 'SETTLED', 15000.00, 435.00, 1200.00, 13365.00, 'USD', 1.0, 15000.00, '2026-07-15 17:00:00'::TIMESTAMP_NTZ, '2026-07-17'::DATE, 'AUTH1021', '3782', 'AMEX', 'US', '10.0.0.22','Chrome/120', 80.0, TRUE, FALSE, 'high-value disputed but not refunded', CURRENT_TIMESTAMP()),
      (122, 5023, 9023, 'PURCHASE', 'SETTLED', 47.20,  1.37, 3.78, 42.05,  'USD', 1.0, 47.20,  '2026-07-15 18:00:00'::TIMESTAMP_NTZ, '2026-07-17'::DATE, 'AUTH1022', '5555', 'MC',   'US', '10.0.0.23','Mozilla/5.0', 7.5,  FALSE, FALSE, NULL, CURRENT_TIMESTAMP()),
      (123, 5024, 9024, 'PURCHASE', 'SETTLED', 89.00,  2.58, 7.12, 79.30,  'USD', 1.0, 89.00,  '2026-07-15 19:00:00'::TIMESTAMP_NTZ, '2026-07-17'::DATE, 'AUTH1023', '4242', 'VISA', 'US', '10.0.0.24','Chrome/120', 10.0, FALSE, FALSE, NULL, CURRENT_TIMESTAMP()),
      (124, 5025, 9025, 'PURCHASE', 'SETTLED', 156.00, 4.52, 12.48, 138.99, 'CAD', 0.73, 113.88,'2026-07-15 20:00:00'::TIMESTAMP_NTZ, '2026-07-17'::DATE, 'AUTH1024', '6011', 'DISC', 'CA', '10.0.0.25','Safari/17', 12.5, FALSE, FALSE, NULL, CURRENT_TIMESTAMP())
    """,
]


def main() -> int:
    logger.info("Creating phase 3 test tables in PLAYGROUND_DB.TEST_DQ")
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
    for tbl in ("CLEAN_CUSTOMERS", "COUNTRIES", "WIDE_TRANSACTIONS"):
        rows = sf_session.query(f"SELECT COUNT(*) AS N FROM PLAYGROUND_DB.TEST_DQ.{tbl}")
        logger.info(f"  PLAYGROUND_DB.TEST_DQ.{tbl}: {rows[0].get('N')} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
