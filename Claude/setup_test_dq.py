"""
Create PLAYGROUND_DB.TEST_DQ schema and seed 5 tables with deliberately bad data
so the DQ platform has something interesting to scan.

Tables:
  CUSTOMERS       — nulls in required fields, duplicate IDs (completeness + uniqueness)
  ORDERS          — invalid status/amount values, nulls (validity)
  TRANSACTIONS    — duplicate transaction IDs, orphan customer refs (uniqueness + referential)
  PRODUCT_CATALOG — stale last_updated dates, nulls in critical cols (freshness + completeness)
  DAILY_SALES     — huge row-count drop mid-series, negative revenue (volume + validity)

Run from repo root (env vars loaded from .env):
    python setup_test_dq.py
"""

import json
import os
import sys

import snowflake.connector
from dotenv import load_dotenv

load_dotenv()


def connect():
    return snowflake.connector.connect(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        authenticator=os.environ.get("SNOWFLAKE_AUTHENTICATOR", "externalbrowser"),
        role=os.environ.get("APP_SNOWFLAKE_ROLE") or os.environ.get("SNOWFLAKE_ROLE"),
        warehouse=os.environ["SNOWFLAKE_WAREHOUSE"],
        database="PLAYGROUND_DB",
        client_store_temporary_credential=True,
    )


def run(cur, sql, label=""):
    print(f"  {'> ' + label if label else sql[:80].strip()}")
    cur.execute(sql)


def setup():
    conn = connect()
    cur = conn.cursor()

    # ── Schema ───────────────────────────────────────────────────────────────
    run(cur, "CREATE SCHEMA IF NOT EXISTS PLAYGROUND_DB.TEST_DQ", "create schema TEST_DQ")

    # ── 1. CUSTOMERS — completeness + uniqueness issues ───────────────────────
    print("\n[1/5] CUSTOMERS")
    run(cur, "DROP TABLE IF EXISTS PLAYGROUND_DB.TEST_DQ.CUSTOMERS")
    run(cur, """
        CREATE TABLE PLAYGROUND_DB.TEST_DQ.CUSTOMERS (
            CUSTOMER_ID   INTEGER,
            FIRST_NAME    VARCHAR(100),
            LAST_NAME     VARCHAR(100),
            EMAIL         VARCHAR(200),
            PHONE         VARCHAR(20),
            COUNTRY       VARCHAR(50),
            CREATED_AT    TIMESTAMP_NTZ
        )
    """)
    # 30 rows: duplicated IDs, ~30% null emails, nulls in name fields, blank phone
    customers = [
        # (id, first, last, email, phone, country, created_at)
        (1,    'Alice',  'Nguyen',  'alice@example.com',    '555-0101', 'US',  '2024-01-10 08:00:00'),
        (2,    'Bob',    'Smith',   None,                   '555-0102', 'US',  '2024-01-11 09:00:00'),
        (3,    'Carol',  'Lee',     'carol@example.com',    None,       'CA',  '2024-01-12 10:00:00'),
        (4,    None,     'Patel',   'dpatel@example.com',   '555-0104', 'IN',  '2024-01-13 11:00:00'),
        (5,    'Eva',    None,      None,                   '555-0105', 'UK',  '2024-01-14 12:00:00'),
        (6,    'Frank',  'Garcia',  'frank@example.com',    '',         'MX',  '2024-01-15 08:30:00'),
        (7,    'Grace',  'Kim',     'grace@example.com',    '555-0107', 'KR',  '2024-01-16 09:30:00'),
        (8,    'Henry',  'Brown',   None,                   '555-0108', 'US',  '2024-01-17 10:30:00'),
        (9,    'Iris',   'Wang',    'iris@example.com',     '555-0109', None,  '2024-01-18 11:30:00'),
        (10,   'Jack',   'Jones',   'jack@example.com',     '555-0110', 'AU',  '2024-01-19 12:30:00'),
        # Duplicate IDs
        (1,    'Alice2', 'Nguyen2', 'alice2@example.com',   '555-0201', 'US',  '2024-02-01 08:00:00'),
        (3,    'Carol2', 'Lee2',    None,                   '555-0203', 'CA',  '2024-02-02 09:00:00'),
        (7,    'Grace2', 'Kim2',    'grace2@example.com',   '555-0207', 'KR',  '2024-02-03 10:00:00'),
        # More rows with nulls
        (11,   None,     None,      None,                   None,       'US',  '2024-02-10 08:00:00'),
        (12,   'Liam',   'Taylor',  'liam@example.com',     '555-0112', 'US',  '2024-02-11 09:00:00'),
        (13,   'Mia',    'Anderson',None,                   '555-0113', 'DE',  '2024-02-12 10:00:00'),
        (14,   'Noah',   'Thomas',  'noah@example.com',     None,       'FR',  '2024-02-13 11:00:00'),
        (15,   'Olivia', 'Jackson', None,                   '555-0115', 'IT',  '2024-02-14 12:00:00'),
        (16,   None,     'White',   'w16@example.com',      '555-0116', 'ES',  '2024-02-15 13:00:00'),
        (17,   'Peter',  None,      None,                   '555-0117', 'BR',  '2024-02-16 14:00:00'),
        (18,   'Quinn',  'Harris',  'quinn@example.com',    '',         'AR',  '2024-02-17 15:00:00'),
        (19,   'Rachel', 'Martin',  'rachel@example.com',   '555-0119', 'CL',  '2024-02-18 08:00:00'),
        (20,   'Sam',    'Garcia',  None,                   '555-0120', 'CO',  '2024-02-19 09:00:00'),
        # More duplicates
        (10,   'Jack2',  'Jones2',  'jack2@example.com',    '555-0310', 'AU',  '2024-03-01 10:00:00'),
        (12,   'Liam2',  'Taylor2', None,                   '555-0312', 'US',  '2024-03-02 11:00:00'),
        (21,   'Tina',   'Martinez',None,                   None,       None,  '2024-03-05 08:00:00'),
        (22,   'Uma',    'Robinson','uma@example.com',      '555-0122', 'US',  '2024-03-06 09:00:00'),
        (23,   'Victor', 'Clark',   'victor@example.com',   '555-0123', 'US',  '2024-03-07 10:00:00'),
        (24,   None,     None,      None,                   None,       None,  '2024-03-08 11:00:00'),
        (25,   'Wendy',  'Lewis',   'wendy@example.com',    '555-0125', 'US',  '2024-03-09 12:00:00'),
    ]
    for row in customers:
        vals = ', '.join(['NULL' if v is None else f"''" if v == '' else f"'{v}'" if isinstance(v, str) else str(v) for v in row])
        cur.execute(f"INSERT INTO PLAYGROUND_DB.TEST_DQ.CUSTOMERS VALUES ({vals})")
    print(f"    inserted {len(customers)} rows")

    # ── 2. ORDERS — validity issues ───────────────────────────────────────────
    print("\n[2/5] ORDERS")
    run(cur, "DROP TABLE IF EXISTS PLAYGROUND_DB.TEST_DQ.ORDERS")
    run(cur, """
        CREATE TABLE PLAYGROUND_DB.TEST_DQ.ORDERS (
            ORDER_ID      INTEGER,
            CUSTOMER_ID   INTEGER,
            ORDER_DATE    DATE,
            STATUS        VARCHAR(20),
            AMOUNT        FLOAT,
            CURRENCY      VARCHAR(10),
            DISCOUNT_PCT  FLOAT
        )
    """)
    orders = [
        # Valid
        (1001, 1,  '2024-02-01', 'SHIPPED',   149.99, 'USD', 0.0),
        (1002, 2,  '2024-02-02', 'DELIVERED',  89.50, 'USD', 5.0),
        (1003, 3,  '2024-02-03', 'PENDING',   220.00, 'CAD', 0.0),
        (1004, 4,  '2024-02-04', 'CANCELLED',  55.00, 'INR', 0.0),
        (1005, 5,  '2024-02-05', 'SHIPPED',   310.75, 'GBP', 10.0),
        # Invalid status values
        (1006, 6,  '2024-02-06', 'shipped',   175.00, 'MXN', 0.0),   # lowercase
        (1007, 7,  '2024-02-07', 'REFUNDD',    90.00, 'KRW', 0.0),   # typo
        (1008, 8,  '2024-02-08', 'N/A',        60.00, 'USD', 0.0),   # bad value
        (1009, 9,  '2024-02-09', None,        120.00, 'USD', 0.0),   # null status
        (1010, 10, '2024-02-10', 'PROCESSING', 88.00, 'AUD', 0.0),   # not in enum
        # Negative / zero amounts
        (1011, 1,  '2024-02-11', 'DELIVERED',  -50.00,'USD', 0.0),   # negative amount
        (1012, 2,  '2024-02-12', 'SHIPPED',      0.00,'USD', 0.0),   # zero amount
        (1013, 3,  '2024-02-13', 'PENDING',    -1.00, 'CAD', 0.0),
        # Discount out of range (>100%)
        (1014, 4,  '2024-02-14', 'SHIPPED',   500.00, 'INR', 150.0), # discount > 100
        (1015, 5,  '2024-02-15', 'DELIVERED', 200.00, 'GBP', -10.0), # negative discount
        # Null amounts
        (1016, 6,  '2024-02-16', 'SHIPPED',   None,   'USD', 0.0),
        (1017, 7,  '2024-02-17', 'DELIVERED', None,   'USD', 0.0),
        # Invalid currency codes
        (1018, 8,  '2024-02-18', 'PENDING',   300.00, 'XX',  0.0),
        (1019, 9,  '2024-02-19', 'SHIPPED',   250.00, None,  0.0),
        (1020, 10, '2024-02-20', 'DELIVERED', 180.00, 'USDD',0.0),   # 4-char code
        # Future order dates
        (1021, 1,  '2030-01-01', 'PENDING',   999.00, 'USD', 0.0),
        (1022, 2,  '2099-12-31', 'PENDING',   111.00, 'USD', 0.0),
        # Valid batch to pad
        (1023, 11, '2024-03-01', 'SHIPPED',   75.00,  'USD', 5.0),
        (1024, 12, '2024-03-02', 'DELIVERED', 230.00, 'USD', 0.0),
        (1025, 13, '2024-03-03', 'CANCELLED', 45.00,  'EUR', 0.0),
    ]
    for row in orders:
        vals = ', '.join(['NULL' if v is None else f"'{v}'" if isinstance(v, str) else str(v) for v in row])
        cur.execute(f"INSERT INTO PLAYGROUND_DB.TEST_DQ.ORDERS VALUES ({vals})")
    print(f"    inserted {len(orders)} rows")

    # ── 3. TRANSACTIONS — uniqueness + referential integrity ──────────────────
    print("\n[3/5] TRANSACTIONS")
    run(cur, "DROP TABLE IF EXISTS PLAYGROUND_DB.TEST_DQ.TRANSACTIONS")
    run(cur, """
        CREATE TABLE PLAYGROUND_DB.TEST_DQ.TRANSACTIONS (
            TRANSACTION_ID  VARCHAR(20),
            CUSTOMER_ID     INTEGER,
            ORDER_ID        INTEGER,
            TXN_DATE        TIMESTAMP_NTZ,
            AMOUNT          FLOAT,
            PAYMENT_METHOD  VARCHAR(30),
            STATUS          VARCHAR(20)
        )
    """)
    transactions = [
        # Valid
        ('TXN-001', 1,  1001, '2024-02-01 10:00:00', 149.99, 'CREDIT_CARD', 'SUCCESS'),
        ('TXN-002', 2,  1002, '2024-02-02 11:00:00',  89.50, 'DEBIT_CARD',  'SUCCESS'),
        ('TXN-003', 3,  1003, '2024-02-03 12:00:00', 220.00, 'BANK_TRANSFER','PENDING'),
        ('TXN-004', 4,  1004, '2024-02-04 13:00:00',  55.00, 'CREDIT_CARD', 'FAILED'),
        ('TXN-005', 5,  1005, '2024-02-05 14:00:00', 310.75, 'PAYPAL',      'SUCCESS'),
        # Duplicate transaction IDs
        ('TXN-001', 1,  1001, '2024-02-01 10:05:00', 149.99, 'CREDIT_CARD', 'SUCCESS'),
        ('TXN-003', 3,  1003, '2024-02-03 12:10:00', 220.00, 'BANK_TRANSFER','SUCCESS'),
        ('TXN-007', 7,  1007, '2024-02-07 09:00:00',  90.00, 'DEBIT_CARD',  'SUCCESS'),
        ('TXN-007', 7,  1007, '2024-02-07 09:02:00',  90.00, 'DEBIT_CARD',  'SUCCESS'),
        ('TXN-007', 7,  1007, '2024-02-07 09:04:00',  90.00, 'DEBIT_CARD',  'SUCCESS'),  # tripled
        # Orphan customer IDs (don't exist in CUSTOMERS)
        ('TXN-010', 9999, 1010, '2024-02-10 08:00:00', 88.00, 'CREDIT_CARD', 'SUCCESS'),
        ('TXN-011', 8888, 1011, '2024-02-11 09:00:00', 50.00, 'PAYPAL',      'SUCCESS'),
        # Null transaction IDs
        (None,      8,  1008, '2024-02-08 15:00:00',  60.00, 'BANK_TRANSFER','PENDING'),
        (None,      9,  1009, '2024-02-09 16:00:00', 120.00, 'CREDIT_CARD',  'SUCCESS'),
        # Orphan order IDs
        ('TXN-015', 1,  9001, '2024-02-15 10:00:00', 200.00, 'CREDIT_CARD', 'SUCCESS'),
        ('TXN-016', 2,  9002, '2024-02-16 11:00:00', 150.00, 'DEBIT_CARD',  'SUCCESS'),
        # Null amounts
        ('TXN-017', 10, 1020, '2024-02-17 12:00:00', None,   'CREDIT_CARD', 'SUCCESS'),
        # Valid batch
        ('TXN-018', 11, 1023, '2024-03-01 10:00:00',  75.00, 'CREDIT_CARD', 'SUCCESS'),
        ('TXN-019', 12, 1024, '2024-03-02 11:00:00', 230.00, 'BANK_TRANSFER','SUCCESS'),
        ('TXN-020', 13, 1025, '2024-03-03 12:00:00',  45.00, 'PAYPAL',      'FAILED'),
    ]
    for row in transactions:
        vals = ', '.join(['NULL' if v is None else f"'{v}'" if isinstance(v, str) else str(v) for v in row])
        cur.execute(f"INSERT INTO PLAYGROUND_DB.TEST_DQ.TRANSACTIONS VALUES ({vals})")
    print(f"    inserted {len(transactions)} rows")

    # ── 4. PRODUCT_CATALOG — freshness + completeness ─────────────────────────
    print("\n[4/5] PRODUCT_CATALOG")
    run(cur, "DROP TABLE IF EXISTS PLAYGROUND_DB.TEST_DQ.PRODUCT_CATALOG")
    run(cur, """
        CREATE TABLE PLAYGROUND_DB.TEST_DQ.PRODUCT_CATALOG (
            PRODUCT_ID    INTEGER,
            PRODUCT_NAME  VARCHAR(200),
            CATEGORY      VARCHAR(100),
            PRICE         FLOAT,
            STOCK_QTY     INTEGER,
            SUPPLIER_ID   INTEGER,
            LAST_UPDATED  TIMESTAMP_NTZ,
            IS_ACTIVE     BOOLEAN
        )
    """)
    catalog = [
        # Very stale last_updated (2+ years ago)
        (101, 'Legacy Widget A',    'WIDGETS',      9.99,   0,   None, '2021-03-01 00:00:00', True),
        (102, 'Old Gadget B',       'GADGETS',     49.99,   5,   201,  '2020-11-15 00:00:00', True),
        (103, 'Vintage Gizmo C',    'GIZMOS',      19.99,  12,   202,  '2021-06-20 00:00:00', True),
        # Null product names
        (104, None,                 'WIDGETS',     14.99,  30,   201,  '2024-01-05 00:00:00', True),
        (105, None,                 'GADGETS',     99.00,   0,   None, '2024-02-10 00:00:00', True),
        # Null / zero prices
        (106, 'Free Sample D',      'SAMPLES',      0.00,  50,   203,  '2024-03-01 00:00:00', True),
        (107, 'Unpriced Item E',    'MISC',         None,  10,   204,  '2024-03-15 00:00:00', True),
        (108, 'Negative Price F',   'MISC',        -5.00,   8,   203,  '2024-03-20 00:00:00', True),
        # Null category
        (109, 'Mystery Product G',  None,          25.00,  20,   205,  '2024-04-01 00:00:00', True),
        (110, 'Unknown Item H',     None,          35.00,   0,   None, '2023-01-01 00:00:00', True),
        # Negative stock
        (111, 'Oversold Widget I',  'WIDGETS',     12.99, -10,   201,  '2024-04-10 00:00:00', True),
        (112, 'Oversold Gadget J',  'GADGETS',     79.00,  -3,   202,  '2024-04-11 00:00:00', True),
        # Null last_updated (no freshness data at all)
        (113, 'Ghost Product K',    'WIDGETS',     22.00,  15,   201,  None,                  True),
        (114, 'Phantom Item L',     'GADGETS',     55.00,   7,   202,  None,                  True),
        # is_active null
        (115, 'Ambiguous Product M','MISC',        18.00,  25,   203,  '2024-04-15 00:00:00', None),
        # Stale AND null name
        (116, None,                 'GIZMOS',       8.00,  40,   204,  '2021-12-31 00:00:00', True),
        # Valid recent rows
        (117, 'Fresh Widget N',     'WIDGETS',     11.99,  60,   201,  '2024-05-01 00:00:00', True),
        (118, 'New Gadget O',       'GADGETS',     65.00,  35,   202,  '2024-05-05 00:00:00', True),
        (119, 'Current Gizmo P',    'GIZMOS',      30.00,  18,   203,  '2024-05-10 00:00:00', True),
        (120, 'Active Item Q',      'MISC',        42.00,  22,   204,  '2024-05-15 00:00:00', False),
    ]
    for row in catalog:
        vals = ', '.join(['NULL' if v is None else f"'{v}'" if isinstance(v, str) else str(v) for v in row])
        cur.execute(f"INSERT INTO PLAYGROUND_DB.TEST_DQ.PRODUCT_CATALOG VALUES ({vals})")
    print(f"    inserted {len(catalog)} rows")

    # ── 5. DAILY_SALES — volume anomaly + validity ────────────────────────────
    print("\n[5/5] DAILY_SALES")
    run(cur, "DROP TABLE IF EXISTS PLAYGROUND_DB.TEST_DQ.DAILY_SALES")
    run(cur, """
        CREATE TABLE PLAYGROUND_DB.TEST_DQ.DAILY_SALES (
            SALE_DATE       DATE,
            REGION          VARCHAR(50),
            PRODUCT_ID      INTEGER,
            UNITS_SOLD      INTEGER,
            REVENUE         FLOAT,
            COST            FLOAT,
            CHANNEL         VARCHAR(30)
        )
    """)
    # Generate: Jan–Mar healthy volume (~15 rows/day), then sudden drop to 1–2 rows/day in Apr
    import datetime
    rows_ds = []

    regions = ['NORTH', 'SOUTH', 'EAST', 'WEST']
    channels = ['ONLINE', 'RETAIL', 'WHOLESALE']
    product_ids = [117, 118, 119, 120]

    # Jan–Mar: healthy data, 4 regions × 3 channels = 12 combos, but let's do 3 per day
    d = datetime.date(2024, 1, 1)
    end_healthy = datetime.date(2024, 3, 31)
    while d <= end_healthy:
        for i in range(3):
            region = regions[i % len(regions)]
            channel = channels[i % len(channels)]
            pid = product_ids[i % len(product_ids)]
            units = 10 + (i * 5)
            revenue = round(units * 25.0 + (i * 10), 2)
            cost = round(revenue * 0.6, 2)
            rows_ds.append((d.isoformat(), region, pid, units, revenue, cost, channel))
        d += datetime.timedelta(days=1)

    # Apr: sudden volume drop — only 1 row every other day (simulates pipeline failure)
    d = datetime.date(2024, 4, 1)
    end_drop = datetime.date(2024, 4, 30)
    toggle = True
    while d <= end_drop:
        if toggle:
            rows_ds.append((d.isoformat(), 'NORTH', 117, 8, 200.00, 120.00, 'ONLINE'))
        toggle = not toggle
        d += datetime.timedelta(days=1)

    # Sprinkle bad validity rows across the whole range
    bad_rows = [
        ('2024-01-15', 'NORTH',   117, -5,     250.00,  150.00, 'ONLINE'),     # negative units
        ('2024-01-20', 'SOUTH',   118,  0,       0.00,    0.00, 'RETAIL'),     # zero units
        ('2024-02-10', 'EAST',    119, 20,    -500.00,  300.00, 'WHOLESALE'),  # negative revenue
        ('2024-02-14', 'WEST',    120, 15,     375.00, 9999.00, 'ONLINE'),     # cost >> revenue
        ('2024-03-05', 'NORTH',   117, 10,     250.00,   -50.00,'RETAIL'),     # negative cost
        ('2024-03-20', None,      118, 12,     300.00,  180.00, 'ONLINE'),     # null region
        ('2024-04-10', 'SOUTH',   None, 8,     200.00,  120.00, 'RETAIL'),    # null product_id
        ('2024-04-15', 'EAST',    119,  5,      None,    75.00, 'WHOLESALE'),  # null revenue
    ]
    rows_ds.extend(bad_rows)

    for row in rows_ds:
        vals = ', '.join(['NULL' if v is None else f"'{v}'" if isinstance(v, str) else str(v) for v in row])
        cur.execute(f"INSERT INTO PLAYGROUND_DB.TEST_DQ.DAILY_SALES VALUES ({vals})")
    print(f"    inserted {len(rows_ds)} rows")

    cur.close()
    conn.close()

    print("\nDone. Tables in PLAYGROUND_DB.TEST_DQ:")
    print("  CUSTOMERS       - null names/emails, duplicate CUSTOMER_IDs")
    print("  ORDERS          - invalid STATUS values, negative/null AMOUNTs, future dates, bad CURRENCY")
    print("  TRANSACTIONS    - duplicate TRANSACTION_IDs, orphan customer/order refs, null IDs")
    print("  PRODUCT_CATALOG - stale LAST_UPDATED (2020-2021), null names/categories, negative PRICE/STOCK")
    print("  DAILY_SALES     - volume drop in April, negative UNITS/REVENUE/COST, null REGION/PRODUCT_ID")


if __name__ == "__main__":
    try:
        setup()
    except Exception as e:
        print(f"\n✗ Failed: {e}", file=sys.stderr)
        sys.exit(1)
