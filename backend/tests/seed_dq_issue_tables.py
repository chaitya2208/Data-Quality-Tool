"""
Seed 8 dedicated DQ-testing tables in PLAYGROUND_DB.TEST_DQ_ISSUES.

Every planted violation is documented in `PLANTED_ISSUES` at the bottom of
this file so integration tests can assert "this specific issue was caught."

Tables (all self-contained — no cross-table joins required to plant issues):

  1. DQTEST_NULL_HEAVY          — a NOT-NULL-shaped column with 10% NULLs
  2. DQTEST_DUPLICATE_KEY       — a "key" column with 5 exact-duplicate rows
  3. DQTEST_OUT_OF_RANGE        — numeric column with values outside a natural bound
  4. DQTEST_BAD_FORMAT          — email column with malformed addresses
  5. DQTEST_STALE_TIMESTAMPS    — a "last_updated" column with values from 2020
  6. DQTEST_ENUM_VIOLATIONS     — a status column with values outside a known set
  7. DQTEST_ORPHAN_FK           — a child_id column referencing missing parent
  8. DQTEST_NEGATIVE_AMOUNTS    — an amount column with negative values (currency)

Run with: `python -m tests.seed_dq_issue_tables` from backend/.
Requires an SSO-authenticated Snowflake session (backend/.env).
"""
from __future__ import annotations

import logging
import sys
from typing import Dict, List

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

SCHEMA = "PLAYGROUND_DB.TEST_DQ"


DDL = [
    # SCHEMA already exists (reuse PLAYGROUND_DB.TEST_DQ); role lacks CREATE SCHEMA
    f"DROP TABLE IF EXISTS {SCHEMA}.DQTEST_NULL_HEAVY",
    f"""CREATE TABLE {SCHEMA}.DQTEST_NULL_HEAVY (
        ID          NUMBER(10,0) NOT NULL,
        NAME        VARCHAR(100),
        CUSTOMER_ID NUMBER(10,0),
        CREATED_AT  TIMESTAMP_NTZ
    ) COMMENT = 'CUSTOMER_ID has ~10% NULLs to trip not_null / nullable_id checks'""",

    f"DROP TABLE IF EXISTS {SCHEMA}.DQTEST_DUPLICATE_KEY",
    f"""CREATE TABLE {SCHEMA}.DQTEST_DUPLICATE_KEY (
        RECORD_ID   NUMBER(10,0) NOT NULL,
        USER_ID     NUMBER(10,0) NOT NULL,
        VALUE       VARCHAR(50)
    ) COMMENT = 'USER_ID looks like a natural key but has 5 duplicate rows'""",

    f"DROP TABLE IF EXISTS {SCHEMA}.DQTEST_OUT_OF_RANGE",
    f"""CREATE TABLE {SCHEMA}.DQTEST_OUT_OF_RANGE (
        ID          NUMBER(10,0) NOT NULL,
        AGE         NUMBER(5,0),
        SCORE       NUMBER(5,2)
    ) COMMENT = 'AGE has 250 (impossible), SCORE has -20 and 150 (outside 0-100)'""",

    f"DROP TABLE IF EXISTS {SCHEMA}.DQTEST_BAD_FORMAT",
    f"""CREATE TABLE {SCHEMA}.DQTEST_BAD_FORMAT (
        ID          NUMBER(10,0) NOT NULL,
        EMAIL       VARCHAR(200),
        PHONE       VARCHAR(30)
    ) COMMENT = 'EMAIL and PHONE have malformed values ("not-an-email", "abc")'""",

    f"DROP TABLE IF EXISTS {SCHEMA}.DQTEST_STALE_TIMESTAMPS",
    f"""CREATE TABLE {SCHEMA}.DQTEST_STALE_TIMESTAMPS (
        ID              NUMBER(10,0) NOT NULL,
        LAST_UPDATED    TIMESTAMP_NTZ,
        EVENT_DATE      DATE
    ) COMMENT = 'LAST_UPDATED and EVENT_DATE contain 2020-era stale timestamps'""",

    f"DROP TABLE IF EXISTS {SCHEMA}.DQTEST_ENUM_VIOLATIONS",
    f"""CREATE TABLE {SCHEMA}.DQTEST_ENUM_VIOLATIONS (
        ID          NUMBER(10,0) NOT NULL,
        STATUS      VARCHAR(20),
        PRIORITY    VARCHAR(10)
    ) COMMENT = 'STATUS has UNKNOWN_STATE (outside active/pending/closed set)'""",

    f"DROP TABLE IF EXISTS {SCHEMA}.DQTEST_PARENT_TABLE",
    f"""CREATE TABLE {SCHEMA}.DQTEST_PARENT_TABLE (
        PARENT_ID   NUMBER(10,0) NOT NULL,
        NAME        VARCHAR(100)
    ) COMMENT = 'Parent for DQTEST_ORPHAN_FK test — has IDs 1..5'""",

    f"DROP TABLE IF EXISTS {SCHEMA}.DQTEST_ORPHAN_FK",
    f"""CREATE TABLE {SCHEMA}.DQTEST_ORPHAN_FK (
        CHILD_ID    NUMBER(10,0) NOT NULL,
        PARENT_ID   NUMBER(10,0),
        DETAIL      VARCHAR(100)
    ) COMMENT = 'PARENT_ID has orphan value 999 (not in DQTEST_PARENT_TABLE)'""",

    f"DROP TABLE IF EXISTS {SCHEMA}.DQTEST_NEGATIVE_AMOUNTS",
    f"""CREATE TABLE {SCHEMA}.DQTEST_NEGATIVE_AMOUNTS (
        TXN_ID      NUMBER(10,0) NOT NULL,
        AMOUNT_USD  NUMBER(12,2),
        FEE_USD     NUMBER(12,2)
    ) COMMENT = 'AMOUNT_USD has negative values (should be >= 0 for a revenue table)'""",
]


DATA_SQL: List[str] = [
    # DQTEST_NULL_HEAVY — 100 rows, 10 with NULL CUSTOMER_ID
    f"""INSERT INTO {SCHEMA}.DQTEST_NULL_HEAVY
        SELECT
            SEQ8() + 1 AS ID,
            'name_' || SEQ8() AS NAME,
            CASE WHEN MOD(SEQ8(), 10) = 0 THEN NULL ELSE SEQ8() + 100 END AS CUSTOMER_ID,
            CURRENT_TIMESTAMP() AS CREATED_AT
        FROM TABLE(GENERATOR(ROWCOUNT => 100))""",

    # DQTEST_DUPLICATE_KEY — 50 rows normally, plus 5 duplicates of USER_ID=42
    f"""INSERT INTO {SCHEMA}.DQTEST_DUPLICATE_KEY
        SELECT SEQ8()+1, SEQ8()+1, 'v_'||SEQ8()
        FROM TABLE(GENERATOR(ROWCOUNT => 50))""",
    f"""INSERT INTO {SCHEMA}.DQTEST_DUPLICATE_KEY VALUES
        (101, 42, 'dup1'), (102, 42, 'dup2'), (103, 42, 'dup3'),
        (104, 42, 'dup4'), (105, 42, 'dup5')""",

    # DQTEST_OUT_OF_RANGE — 50 valid + 3 wildly out-of-range
    f"""INSERT INTO {SCHEMA}.DQTEST_OUT_OF_RANGE
        SELECT SEQ8()+1, MOD(SEQ8(), 90) + 10, MOD(SEQ8(), 100)
        FROM TABLE(GENERATOR(ROWCOUNT => 50))""",
    f"""INSERT INTO {SCHEMA}.DQTEST_OUT_OF_RANGE VALUES
        (101, 250, 50), (102, 30, -20), (103, 42, 150)""",

    # DQTEST_BAD_FORMAT — 50 valid emails + 4 malformed
    f"""INSERT INTO {SCHEMA}.DQTEST_BAD_FORMAT
        SELECT SEQ8()+1, 'user'||SEQ8()||'@example.com', '+1-555-01' || LPAD(SEQ8()::VARCHAR, 2, '0')
        FROM TABLE(GENERATOR(ROWCOUNT => 50))""",
    f"""INSERT INTO {SCHEMA}.DQTEST_BAD_FORMAT VALUES
        (101, 'not-an-email', 'abc'),
        (102, 'no@dot',       '123'),
        (103, '@missinguser.com', '   '),
        (104, 'foo bar@x.com', '+1-555')""",

    # DQTEST_STALE_TIMESTAMPS — 20 fresh + 5 with old dates
    f"""INSERT INTO {SCHEMA}.DQTEST_STALE_TIMESTAMPS
        SELECT SEQ8()+1, CURRENT_TIMESTAMP(), CURRENT_DATE()
        FROM TABLE(GENERATOR(ROWCOUNT => 20))""",
    f"""INSERT INTO {SCHEMA}.DQTEST_STALE_TIMESTAMPS VALUES
        (101, '2020-01-01 00:00:00'::TIMESTAMP_NTZ, '2020-01-01'::DATE),
        (102, '2020-06-15 00:00:00'::TIMESTAMP_NTZ, '2020-06-15'::DATE),
        (103, '2019-12-31 00:00:00'::TIMESTAMP_NTZ, '2019-12-31'::DATE),
        (104, '2021-03-10 00:00:00'::TIMESTAMP_NTZ, '2021-03-10'::DATE),
        (105, '2018-08-08 00:00:00'::TIMESTAMP_NTZ, '2018-08-08'::DATE)""",

    # DQTEST_ENUM_VIOLATIONS — 30 valid + 3 unknown states
    f"""INSERT INTO {SCHEMA}.DQTEST_ENUM_VIOLATIONS
        SELECT SEQ8()+1,
               CASE MOD(SEQ8(), 3) WHEN 0 THEN 'active' WHEN 1 THEN 'pending' ELSE 'closed' END,
               CASE MOD(SEQ8(), 2) WHEN 0 THEN 'high' ELSE 'low' END
        FROM TABLE(GENERATOR(ROWCOUNT => 30))""",
    f"""INSERT INTO {SCHEMA}.DQTEST_ENUM_VIOLATIONS VALUES
        (101, 'UNKNOWN_STATE', 'high'),
        (102, 'unknown_state', 'bogus_pri'),
        (103, 'wat',           'low')""",

    # DQTEST_PARENT_TABLE — 5 parents
    f"""INSERT INTO {SCHEMA}.DQTEST_PARENT_TABLE VALUES
        (1, 'alpha'), (2, 'beta'), (3, 'gamma'), (4, 'delta'), (5, 'epsilon')""",

    # DQTEST_ORPHAN_FK — 20 valid children + 3 orphans (parent_id=999)
    f"""INSERT INTO {SCHEMA}.DQTEST_ORPHAN_FK
        SELECT SEQ8()+1, MOD(SEQ8(), 5) + 1, 'child_'||SEQ8()
        FROM TABLE(GENERATOR(ROWCOUNT => 20))""",
    f"""INSERT INTO {SCHEMA}.DQTEST_ORPHAN_FK VALUES
        (101, 999, 'orphan_a'),
        (102, 999, 'orphan_b'),
        (103, 888, 'orphan_c')""",

    # DQTEST_NEGATIVE_AMOUNTS — 40 valid revenue + 4 negatives
    f"""INSERT INTO {SCHEMA}.DQTEST_NEGATIVE_AMOUNTS
        SELECT SEQ8()+1, (100 + MOD(SEQ8()*37, 9900))::NUMBER(12,2),
               (1 + MOD(SEQ8()*13, 50))::NUMBER(12,2)
        FROM TABLE(GENERATOR(ROWCOUNT => 40))""",
    f"""INSERT INTO {SCHEMA}.DQTEST_NEGATIVE_AMOUNTS VALUES
        (101, -50.00, 5.00),
        (102, -100.00, 2.50),
        (103, -1.00, 0.10),
        (104, 500.00, -3.00)""",
]


# Ground-truth catalog: what SHOULD be caught. Integration tests iterate
# through this to verify Rule Intelligence surfaces each concept.
PLANTED_ISSUES: List[Dict] = [
    {
        "table": "DQTEST_NULL_HEAVY", "column": "CUSTOMER_ID",
        "issue": "null_values",
        "expected_rule_shape": "not_null",
        "expected_fail_count_range": (5, 15),
    },
    {
        "table": "DQTEST_DUPLICATE_KEY", "column": "USER_ID",
        "issue": "duplicate_key",
        "expected_rule_shape": "uniqueness",
        "expected_fail_count_range": (2, 20),
    },
    {
        "table": "DQTEST_OUT_OF_RANGE", "column": "AGE",
        "issue": "out_of_range",
        "expected_rule_shape": "range",
        "expected_fail_count_range": (1, 5),
    },
    {
        "table": "DQTEST_OUT_OF_RANGE", "column": "SCORE",
        "issue": "out_of_range",
        "expected_rule_shape": "range",
        "expected_fail_count_range": (1, 5),
    },
    {
        "table": "DQTEST_BAD_FORMAT", "column": "EMAIL",
        "issue": "invalid_format",
        "expected_rule_shape": "regex_match",
        "expected_fail_count_range": (3, 8),
    },
    {
        "table": "DQTEST_STALE_TIMESTAMPS", "column": "LAST_UPDATED",
        "issue": "staleness",
        "expected_rule_shape": "freshness",
        "expected_fail_count_range": (1, 10),
    },
    {
        "table": "DQTEST_ENUM_VIOLATIONS", "column": "STATUS",
        "issue": "accepted_values_violation",
        "expected_rule_shape": "accepted_values",
        "expected_fail_count_range": (2, 10),
    },
    {
        "table": "DQTEST_ORPHAN_FK", "column": "PARENT_ID",
        "issue": "referential_integrity",
        "expected_rule_shape": "referential_integrity",
        "expected_fail_count_range": (2, 10),
    },
    {
        "table": "DQTEST_NEGATIVE_AMOUNTS", "column": "AMOUNT_USD",
        "issue": "negative_values",
        # No single canonical template shape — may be draft_sql or range
        "expected_rule_shape": None,
        "expected_fail_count_range": (2, 10),
    },
]


TABLES = [
    "DQTEST_NULL_HEAVY", "DQTEST_DUPLICATE_KEY", "DQTEST_OUT_OF_RANGE", "DQTEST_BAD_FORMAT",
    "DQTEST_STALE_TIMESTAMPS", "DQTEST_ENUM_VIOLATIONS", "DQTEST_ORPHAN_FK", "DQTEST_NEGATIVE_AMOUNTS",
    "DQTEST_PARENT_TABLE",
]


def seed():
    """Execute DDL + data insertion in order. Idempotent (drops first)."""
    from app.services.snowflake_session import session as sf
    for stmt in DDL:
        log.info("DDL: %s", stmt.strip().split("\n")[0][:80])
        sf.execute(stmt)
    for stmt in DATA_SQL:
        log.info("INSERT: %s", stmt.strip().split("\n")[0][:80])
        sf.execute(stmt)
    log.info("Seed complete — %d tables in %s", len(TABLES), SCHEMA)
    # Sanity: count rows
    for t in TABLES:
        rows = sf.query(f"SELECT COUNT(*) AS CNT FROM {SCHEMA}.{t}")
        log.info("  %s: %s rows", t, rows[0]["CNT"] if rows else "?")


if __name__ == "__main__":
    seed()
    sys.exit(0)
