"""
Seed a fully-controlled lineage graph in PLAYGROUND_DB.TEST_DQ so we can
verify every lineage code-path without needing CREATE SCHEMA permission or
GET_LINEAGE privileges.

All Snowflake objects are created as simple tables/views in TEST_DQ with a
LIN_ prefix. The LINEAGE_CATALOG and LINEAGE_EDGES rows use *virtual* schema
names (LIN_RAW, LIN_BRONZE, LIN_SILVER, LIN_GOLD) so the graph renders a
realistic multi-schema medallion picture — the lineage page reads entirely
from those two tables, never re-querying INFORMATION_SCHEMA during rendering.

Architecture (virtual schemas → actual Snowflake object in TEST_DQ):

  LIN_RAW    │ LIN_STG_EVENTS_RAW  → PLAYGROUND_DB.TEST_DQ.LIN_STG_EVENTS_RAW
             │ LIN_STG_USERS_RAW   → PLAYGROUND_DB.TEST_DQ.LIN_STG_USERS_RAW
  ───────────┼──────────────────────────────────────────────────────────────────
  LIN_BRONZE │ LIN_EVENTS          → PLAYGROUND_DB.TEST_DQ.LIN_EVENTS
             │ LIN_USERS           → PLAYGROUND_DB.TEST_DQ.LIN_USERS
             │ LIN_PRODUCTS        → PLAYGROUND_DB.TEST_DQ.LIN_PRODUCTS
  ───────────┼──────────────────────────────────────────────────────────────────
  LIN_SILVER │ LIN_CLEAN_EVENTS    → PLAYGROUND_DB.TEST_DQ.LIN_CLEAN_EVENTS  (view)
             │ LIN_CLEAN_USERS     → PLAYGROUND_DB.TEST_DQ.LIN_CLEAN_USERS   (view)
             │ LIN_USER_EVENTS     → PLAYGROUND_DB.TEST_DQ.LIN_USER_EVENTS   (view)
             │ LIN_ORPHAN_TABLE    → PLAYGROUND_DB.TEST_DQ.LIN_ORPHAN_TABLE  (no edges)
  ───────────┼──────────────────────────────────────────────────────────────────
  LIN_GOLD   │ LIN_DAU             → PLAYGROUND_DB.TEST_DQ.LIN_DAU           (view)
             │ LIN_REVENUE         → PLAYGROUND_DB.TEST_DQ.LIN_REVENUE        (view)
             │ LIN_CROSS_VIEW      → PLAYGROUND_DB.TEST_DQ.LIN_CROSS_VIEW     (view)

Edge map (virtual schema names in LINEAGE_EDGES):
  LIN_STG_EVENTS_RAW  → LIN_EVENTS         (copy_into)
  LIN_STG_USERS_RAW   → LIN_USERS          (copy_into)
  LIN_EVENTS          → LIN_CLEAN_EVENTS   (view_dep)
  LIN_USERS           → LIN_CLEAN_USERS    (view_dep)
  LIN_CLEAN_EVENTS    → LIN_USER_EVENTS    (view_dep)   ← diamond: two parents
  LIN_CLEAN_USERS     → LIN_USER_EVENTS    (view_dep)   ← diamond
  LIN_USER_EVENTS     → LIN_DAU            (view_dep)
  LIN_USER_EVENTS     → LIN_REVENUE        (view_dep)
  LIN_PRODUCTS        → LIN_REVENUE        (view_dep)   ← fan-in
  LIN_EVENTS          → LIN_CROSS_VIEW     (view_dep)
  LIN_REVENUE         → LIN_CROSS_VIEW     (view_dep)
  + 1 intentional duplicate (LIN_EVENTS→LIN_CLEAN_EVENTS again) to test dedup

Bug traps:
  1. Diamond: BFS from LIN_USER_EVENTS must list both LIN_CLEAN_EVENTS and
     LIN_CLEAN_USERS as upstream (direction='upstream'), not just one.
  2. Fan-in: LIN_REVENUE upstream_count must equal 2.
  3. Orphan: LIN_ORPHAN_TABLE in catalog, zero edges → must still render.
  4. Hop limit: STG→BRONZE→SILVER→GOLD = 4 hops; hops=1 from LIN_DAU must
     NOT surface the RAW nodes.
  5. Dedup: 12 raw edges → 11 after removing the duplicate.
  6. Cross-virtual-schema: LIN_CROSS_VIEW references BRONZE (LIN_EVENTS) and
     GOLD (LIN_REVENUE) → ghost-node logic should not mis-classify them.

Run from Data-Quality-Tool/backend:
    python seed_lineage_test.py
"""

from __future__ import annotations
import os
import sys
import uuid
import json

import snowflake.connector
from dotenv import load_dotenv

load_dotenv()

DB         = "PLAYGROUND_DB"
REAL_SC    = "TEST_DQ"           # only schema we can write to
APP_SCHEMA = os.environ.get("SNOWFLAKE_APP_SCHEMA", "DQ_APP")

# Virtual schema names used in LINEAGE_CATALOG / LINEAGE_EDGES
V_RAW    = "LIN_RAW"
V_BRONZE = "LIN_BRONZE"
V_SILVER = "LIN_SILVER"
V_GOLD   = "LIN_GOLD"


def real_fqn(table: str) -> str:
    """Actual Snowflake FQN — everything lives in TEST_DQ."""
    return f"{DB}.{REAL_SC}.{table}"


def virt_fqn(vschema: str, vname: str) -> str:
    """Virtual FQN stored in LINEAGE_CATALOG / LINEAGE_EDGES."""
    return f"{DB}.{vschema}.{vname}"


# (virtual_schema, virtual_name, object_kind, actual_table_name_in_TEST_DQ, ddl)
OBJECTS = [
    # ── RAW ─────────────────────────────────────────────────────────────────
    (V_RAW, "LIN_STG_EVENTS_RAW", "table", "LIN_STG_EVENTS_RAW",
     f"CREATE TABLE IF NOT EXISTS {DB}.{REAL_SC}.LIN_STG_EVENTS_RAW "
     f"(EVENT_ID VARCHAR(36), USER_ID VARCHAR(36), EVENT_TYPE VARCHAR(50), "
     f"EVENT_TS TIMESTAMP_NTZ, RAW_PAYLOAD VARCHAR(4096))"),

    (V_RAW, "LIN_STG_USERS_RAW", "table", "LIN_STG_USERS_RAW",
     f"CREATE TABLE IF NOT EXISTS {DB}.{REAL_SC}.LIN_STG_USERS_RAW "
     f"(USER_ID VARCHAR(36), EMAIL VARCHAR(200), SIGNUP_DATE DATE, COUNTRY VARCHAR(50))"),

    # ── BRONZE ──────────────────────────────────────────────────────────────
    (V_BRONZE, "LIN_EVENTS", "table", "LIN_EVENTS",
     f"CREATE TABLE IF NOT EXISTS {DB}.{REAL_SC}.LIN_EVENTS "
     f"(EVENT_ID VARCHAR(36), USER_ID VARCHAR(36), EVENT_TYPE VARCHAR(50), "
     f"EVENT_TS TIMESTAMP_NTZ, _LOADED_AT TIMESTAMP_NTZ)"),

    (V_BRONZE, "LIN_USERS", "table", "LIN_USERS",
     f"CREATE TABLE IF NOT EXISTS {DB}.{REAL_SC}.LIN_USERS "
     f"(USER_ID VARCHAR(36), EMAIL VARCHAR(200), SIGNUP_DATE DATE, "
     f"COUNTRY VARCHAR(50), _LOADED_AT TIMESTAMP_NTZ)"),

    (V_BRONZE, "LIN_PRODUCTS", "table", "LIN_PRODUCTS",
     f"CREATE TABLE IF NOT EXISTS {DB}.{REAL_SC}.LIN_PRODUCTS "
     f"(PRODUCT_ID INTEGER, NAME VARCHAR(200), PRICE FLOAT, CATEGORY VARCHAR(100))"),

    # ── SILVER ──────────────────────────────────────────────────────────────
    (V_SILVER, "LIN_CLEAN_EVENTS", "view", "LIN_CLEAN_EVENTS",
     f"CREATE OR REPLACE VIEW {DB}.{REAL_SC}.LIN_CLEAN_EVENTS AS "
     f"SELECT EVENT_ID, USER_ID, EVENT_TYPE, EVENT_TS "
     f"FROM {DB}.{REAL_SC}.LIN_EVENTS WHERE EVENT_ID IS NOT NULL"),

    (V_SILVER, "LIN_CLEAN_USERS", "view", "LIN_CLEAN_USERS",
     f"CREATE OR REPLACE VIEW {DB}.{REAL_SC}.LIN_CLEAN_USERS AS "
     f"SELECT USER_ID, EMAIL, SIGNUP_DATE, COUNTRY "
     f"FROM {DB}.{REAL_SC}.LIN_USERS WHERE USER_ID IS NOT NULL"),

    (V_SILVER, "LIN_USER_EVENTS", "view", "LIN_USER_EVENTS",
     f"CREATE OR REPLACE VIEW {DB}.{REAL_SC}.LIN_USER_EVENTS AS "
     f"SELECT e.EVENT_ID, e.EVENT_TYPE, e.EVENT_TS, u.EMAIL, u.COUNTRY "
     f"FROM {DB}.{REAL_SC}.LIN_CLEAN_EVENTS e "
     f"JOIN {DB}.{REAL_SC}.LIN_CLEAN_USERS u ON e.USER_ID = u.USER_ID"),

    (V_SILVER, "LIN_ORPHAN_TABLE", "table", "LIN_ORPHAN_TABLE",
     f"CREATE TABLE IF NOT EXISTS {DB}.{REAL_SC}.LIN_ORPHAN_TABLE "
     f"(ID INTEGER, VAL VARCHAR(100))"),

    # ── GOLD ────────────────────────────────────────────────────────────────
    (V_GOLD, "LIN_DAU", "view", "LIN_DAU",
     f"CREATE OR REPLACE VIEW {DB}.{REAL_SC}.LIN_DAU AS "
     f"SELECT DATE_TRUNC('day', EVENT_TS) AS DAY, COUNT(DISTINCT EMAIL) AS DAU "
     f"FROM {DB}.{REAL_SC}.LIN_USER_EVENTS GROUP BY 1"),

    (V_GOLD, "LIN_REVENUE", "view", "LIN_REVENUE",
     f"CREATE OR REPLACE VIEW {DB}.{REAL_SC}.LIN_REVENUE AS "
     f"SELECT u.COUNTRY, p.CATEGORY, COUNT(*) AS EVENTS, SUM(p.PRICE) AS REVENUE "
     f"FROM {DB}.{REAL_SC}.LIN_USER_EVENTS u "
     f"JOIN {DB}.{REAL_SC}.LIN_PRODUCTS p ON p.PRODUCT_ID = 1 GROUP BY 1, 2"),

    (V_GOLD, "LIN_CROSS_VIEW", "view", "LIN_CROSS_VIEW",
     f"CREATE OR REPLACE VIEW {DB}.{REAL_SC}.LIN_CROSS_VIEW AS "
     f"SELECT e.EVENT_TYPE, r.REVENUE "
     f"FROM {DB}.{REAL_SC}.LIN_EVENTS e "
     f"JOIN {DB}.{REAL_SC}.LIN_REVENUE r ON 1=1"),
]


# (src_vschema, src_vname, tgt_vschema, tgt_vname, edge_type)
# Last entry is intentional duplicate to test dedup
EDGES = [
    (V_RAW,    "LIN_STG_EVENTS_RAW",  V_BRONZE, "LIN_EVENTS",        "copy_into"),
    (V_RAW,    "LIN_STG_USERS_RAW",   V_BRONZE, "LIN_USERS",         "copy_into"),
    (V_BRONZE, "LIN_EVENTS",          V_SILVER, "LIN_CLEAN_EVENTS",   "view_dep"),
    (V_BRONZE, "LIN_USERS",           V_SILVER, "LIN_CLEAN_USERS",    "view_dep"),
    (V_SILVER, "LIN_CLEAN_EVENTS",    V_SILVER, "LIN_USER_EVENTS",    "view_dep"),  # diamond
    (V_SILVER, "LIN_CLEAN_USERS",     V_SILVER, "LIN_USER_EVENTS",    "view_dep"),  # diamond
    (V_SILVER, "LIN_USER_EVENTS",     V_GOLD,   "LIN_DAU",            "view_dep"),
    (V_SILVER, "LIN_USER_EVENTS",     V_GOLD,   "LIN_REVENUE",        "view_dep"),
    (V_BRONZE, "LIN_PRODUCTS",        V_GOLD,   "LIN_REVENUE",        "view_dep"),  # fan-in
    (V_BRONZE, "LIN_EVENTS",          V_GOLD,   "LIN_CROSS_VIEW",     "view_dep"),
    (V_GOLD,   "LIN_REVENUE",         V_GOLD,   "LIN_CROSS_VIEW",     "view_dep"),
    # intentional duplicate — dedup must remove this:
    (V_BRONZE, "LIN_EVENTS",          V_SILVER, "LIN_CLEAN_EVENTS",   "view_dep"),
]

EXPECTED_EDGE_COUNT = len(EDGES) - 1   # 11 after removing 1 duplicate


# ─── helpers ─────────────────────────────────────────────────────────────────

def connect() -> snowflake.connector.SnowflakeConnection:
    return snowflake.connector.connect(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        password=os.environ.get("SNOWFLAKE_PASSWORD") or None,
        authenticator=os.environ.get("SNOWFLAKE_AUTH_METHOD", "externalbrowser"),
        role=os.environ.get("SNOWFLAKE_ROLE") or None,
        warehouse=os.environ["SNOWFLAKE_WAREHOUSE"],
        database=DB,
        client_store_temporary_credential=True,
    )


def step(msg: str):
    print(f"\n[seed] {msg}")


def run(cur, sql: str, label: str = ""):
    short = label or sql[:80].replace("\n", " ").strip()
    print(f"  > {short}")
    cur.execute(sql)


def get_connection_id(cur) -> str:
    step("Looking up connection_id from DQ_APP.CONNECTIONS")
    cur.execute(
        f"SELECT ID FROM {DB}.{APP_SCHEMA}.CONNECTIONS "
        f"WHERE UPPER(TYPE) = 'SNOWFLAKE' ORDER BY CREATED_AT ASC LIMIT 1"
    )
    row = cur.fetchone()
    if not row:
        cur.execute(
            f"SELECT ID FROM {DB}.{APP_SCHEMA}.CONNECTIONS ORDER BY CREATED_AT ASC LIMIT 1"
        )
        row = cur.fetchone()
    if not row:
        raise RuntimeError(
            "No rows in CONNECTIONS table. Open the app in a browser once "
            "(it will register the Snowflake connection), then re-run this script."
        )
    cid = row[0]
    print(f"  connection_id = {cid}")
    return cid


# ─── step 1: create real objects in TEST_DQ ───────────────────────────────────

def create_objects(cur):
    step(f"Creating LIN_* tables/views in {DB}.{REAL_SC}")
    for _vsc, _vnm, kind, real_name, ddl in OBJECTS:
        run(cur, ddl, f"{kind}  {REAL_SC}.{real_name}")
    print(f"  created {len(OBJECTS)} objects")


# ─── step 2: clear prior seed ─────────────────────────────────────────────────

def clear_prior_seed(cur, cid: str):
    step("Clearing any prior seed data from DQ_APP lineage tables")

    # LINEAGE_EDGES — remove by virtual schema names (source or target)
    vschemas = "', '".join([V_RAW, V_BRONZE, V_SILVER, V_GOLD])
    cur.execute(
        f"DELETE FROM {DB}.{APP_SCHEMA}.LINEAGE_EDGES "
        f"WHERE CONNECTION_ID = %(cid)s "
        f"  AND (SOURCE_SCHEMA IN ('{vschemas}') OR TARGET_SCHEMA IN ('{vschemas}'))",
        {"cid": cid},
    )
    print(f"  cleared LINEAGE_EDGES")

    # LINEAGE_CATALOG — remove by virtual schema names
    cur.execute(
        f"DELETE FROM {DB}.{APP_SCHEMA}.LINEAGE_CATALOG "
        f"WHERE CONNECTION_ID = %(cid)s "
        f"  AND SCHEMA_NAME IN ('{vschemas}')",
        {"cid": cid},
    )
    print(f"  cleared LINEAGE_CATALOG")

    # LINEAGE_REFRESH_STATE — remove the PLAYGROUND_DB entry so our new one wins
    cur.execute(
        f"DELETE FROM {DB}.{APP_SCHEMA}.LINEAGE_REFRESH_STATE "
        f"WHERE CONNECTION_ID = %(cid)s AND DATABASE_NAME = %(db)s",
        {"cid": cid, "db": DB},
    )
    print(f"  cleared LINEAGE_REFRESH_STATE for {DB}")


# ─── step 3: seed LINEAGE_CATALOG ─────────────────────────────────────────────

def seed_catalog(cur, cid: str):
    step("Seeding LINEAGE_CATALOG")
    rows = []

    # One schema-level row per virtual schema
    for vschema in (V_RAW, V_BRONZE, V_SILVER, V_GOLD):
        rows.append({
            "id": str(uuid.uuid4()), "cid": cid,
            "db": DB, "sc": vschema, "tb": None,
            "kind": "schema", "fqn": f"{DB}.{vschema}",
            "rc": None, "sb": None,
        })

    # One row per object (using virtual schema names for the catalog entry)
    for vsc, vnm, kind, _real, _ in OBJECTS:
        rows.append({
            "id": str(uuid.uuid4()), "cid": cid,
            "db": DB, "sc": vsc, "tb": vnm,
            "kind": kind, "fqn": virt_fqn(vsc, vnm),
            "rc": 0, "sb": 0,
        })

    for r in rows:
        cur.execute(
            f"""
            INSERT INTO {DB}.{APP_SCHEMA}.LINEAGE_CATALOG
                (ID, CONNECTION_ID, DATABASE_NAME, SCHEMA_NAME, TABLE_NAME,
                 OBJECT_KIND, FQN, ROW_COUNT, SIZE_BYTES)
            VALUES (%(id)s, %(cid)s, %(db)s, %(sc)s, %(tb)s,
                    %(kind)s, %(fqn)s, %(rc)s, %(sb)s)
            """,
            r,
        )
    print(f"  inserted {len(rows)} catalog rows ({len(OBJECTS)} objects + 4 schema entries)")


# ─── step 4: seed LINEAGE_EDGES ───────────────────────────────────────────────

def seed_edges(cur, cid: str) -> int:
    step("Seeding LINEAGE_EDGES")

    # Dedup by (source_fqn, target_fqn, edge_type) — mirrors app logic
    seen: dict[tuple, dict] = {}
    for src_vsc, src_vnm, tgt_vsc, tgt_vnm, etype in EDGES:
        key = (virt_fqn(src_vsc, src_vnm), virt_fqn(tgt_vsc, tgt_vnm), etype)
        if key not in seen:
            src_kind = "table" if src_vsc in (V_RAW, V_BRONZE) else "view"
            tgt_kind = "table" if tgt_vnm in ("LIN_EVENTS", "LIN_USERS", "LIN_PRODUCTS",
                                               "LIN_STG_EVENTS_RAW", "LIN_STG_USERS_RAW",
                                               "LIN_ORPHAN_TABLE") else "view"
            seen[key] = {
                "id": str(uuid.uuid4()), "cid": cid,
                "sdb": DB, "ssc": src_vsc, "stb": src_vnm,
                "sfq": virt_fqn(src_vsc, src_vnm), "sk": src_kind,
                "tdb": DB, "tsc": tgt_vsc, "ttb": tgt_vnm,
                "tfq": virt_fqn(tgt_vsc, tgt_vnm), "tk": tgt_kind,
                "et": etype, "ds": "object_dependencies",
                "ev": json.dumps({"note": "seeded_for_testing"}),
            }

    raw = len(EDGES)
    deduped = list(seen.values())
    dropped = raw - len(deduped)
    print(f"  raw edges={raw}  after dedup={len(deduped)}  (dropped {dropped} duplicate(s))")

    for r in deduped:
        cur.execute(
            f"""
            INSERT INTO {DB}.{APP_SCHEMA}.LINEAGE_EDGES
                (ID, CONNECTION_ID,
                 SOURCE_DATABASE, SOURCE_SCHEMA, SOURCE_TABLE, SOURCE_FQN, SOURCE_KIND,
                 TARGET_DATABASE, TARGET_SCHEMA, TARGET_TABLE, TARGET_FQN, TARGET_KIND,
                 EDGE_TYPE, DISCOVERY_SOURCE, EVIDENCE,
                 FIRST_DISCOVERED_AT, LAST_SEEN_AT)
            SELECT %(id)s, %(cid)s,
                   %(sdb)s, %(ssc)s, %(stb)s, %(sfq)s, %(sk)s,
                   %(tdb)s, %(tsc)s, %(ttb)s, %(tfq)s, %(tk)s,
                   %(et)s, %(ds)s, PARSE_JSON(%(ev)s),
                   CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP()
            """,
            r,
        )
    print(f"  inserted {len(deduped)} edge rows")
    return len(deduped)


# ─── step 5: seed LINEAGE_REFRESH_STATE ───────────────────────────────────────

def seed_refresh_state(cur, cid: str, edge_count: int):
    step("Seeding LINEAGE_REFRESH_STATE")
    cur.execute(
        f"""
        INSERT INTO {DB}.{APP_SCHEMA}.LINEAGE_REFRESH_STATE
            (CONNECTION_ID, DATABASE_NAME, LAST_REFRESHED_AT, LAST_STATUS,
             EDGE_COUNT, DISCOVERY_METHOD_USED)
        VALUES (%(cid)s, %(db)s, CURRENT_TIMESTAMP(), 'ok', %(ec)s, 'object_dependencies')
        """,
        {"cid": cid, "db": DB, "ec": edge_count},
    )
    print(f"  inserted refresh state for {DB} (edge_count={edge_count}, status=ok)")


# ─── verification ─────────────────────────────────────────────────────────────

def _check(label: str, actual, expected):
    ok = actual == expected
    mark = "OK  " if ok else "FAIL"
    note = "" if ok else f"  << got {actual!r}, expected {expected!r}"
    print(f"    [{mark}] {label}{note}")


def verify(cur, cid: str):
    print("\n" + "=" * 62)
    print("AUTOMATED VERIFICATION")
    print("=" * 62)

    # 1. Catalog: count scoped to our virtual schemas only
    vschemas_in = "', '".join([V_RAW, V_BRONZE, V_SILVER, V_GOLD])
    cur.execute(
        f"SELECT OBJECT_KIND, COUNT(*) AS N "
        f"FROM {DB}.{APP_SCHEMA}.LINEAGE_CATALOG "
        f"WHERE CONNECTION_ID = %(cid)s AND DATABASE_NAME = %(db)s "
        f"  AND (SCHEMA_NAME IN ('{vschemas_in}') "
        f"       OR (OBJECT_KIND = 'schema' AND FQN IN ("
        f"           '{DB}.{V_RAW}', '{DB}.{V_BRONZE}', "
        f"           '{DB}.{V_SILVER}', '{DB}.{V_GOLD}'))) "
        f"GROUP BY OBJECT_KIND ORDER BY OBJECT_KIND",
        {"cid": cid, "db": DB},
    )
    kind_counts = dict(cur.fetchall())
    print("\n[1] LINEAGE_CATALOG object counts:")
    for k, n in sorted(kind_counts.items()):
        print(f"    {k:<20} {n}")
    exp_tables = sum(1 for _, _, k, _, _ in OBJECTS if k == "table")
    exp_views  = sum(1 for _, _, k, _, _ in OBJECTS if k == "view")
    _check("schema entries = 4",  kind_counts.get("schema", 0), 4)
    _check(f"table entries = {exp_tables}",  kind_counts.get("table", 0),  exp_tables)
    _check(f"view entries  = {exp_views}",   kind_counts.get("view", 0),   exp_views)

    # 2. Edge total after dedup
    cur.execute(
        f"SELECT COUNT(*) FROM {DB}.{APP_SCHEMA}.LINEAGE_EDGES "
        f"WHERE CONNECTION_ID = %(cid)s "
        f"  AND SOURCE_SCHEMA IN (%(r)s, %(b)s, %(s)s, %(g)s)",
        {"cid": cid, "r": V_RAW, "b": V_BRONZE, "s": V_SILVER, "g": V_GOLD},
    )
    n_edges = cur.fetchone()[0]
    print(f"\n[2] LINEAGE_EDGES total: {n_edges}")
    _check(f"edge_count = {EXPECTED_EDGE_COUNT}", n_edges, EXPECTED_EDGE_COUNT)

    # 3. Diamond: LIN_USER_EVENTS must have exactly 2 upstream edges
    cur.execute(
        f"SELECT COUNT(*) FROM {DB}.{APP_SCHEMA}.LINEAGE_EDGES "
        f"WHERE CONNECTION_ID = %(cid)s "
        f"  AND TARGET_SCHEMA = %(sc)s AND TARGET_TABLE = 'LIN_USER_EVENTS'",
        {"cid": cid, "sc": V_SILVER},
    )
    n_ue = cur.fetchone()[0]
    print(f"\n[3] Diamond — upstream edges into LIN_USER_EVENTS: {n_ue}")
    _check("diamond upstream_count = 2", n_ue, 2)

    # 4. Fan-in: LIN_REVENUE must have exactly 2 upstream edges
    cur.execute(
        f"SELECT COUNT(*) FROM {DB}.{APP_SCHEMA}.LINEAGE_EDGES "
        f"WHERE CONNECTION_ID = %(cid)s "
        f"  AND TARGET_SCHEMA = %(sc)s AND TARGET_TABLE = 'LIN_REVENUE'",
        {"cid": cid, "sc": V_GOLD},
    )
    n_rev = cur.fetchone()[0]
    print(f"\n[4] Fan-in — upstream edges into LIN_REVENUE: {n_rev}")
    _check("fanin upstream_count = 2", n_rev, 2)

    # 5. Orphan: in catalog, zero edges
    cur.execute(
        f"SELECT COUNT(*) FROM {DB}.{APP_SCHEMA}.LINEAGE_CATALOG "
        f"WHERE CONNECTION_ID = %(cid)s AND TABLE_NAME = 'LIN_ORPHAN_TABLE'",
        {"cid": cid},
    )
    n_cat = cur.fetchone()[0]
    cur.execute(
        f"SELECT COUNT(*) FROM {DB}.{APP_SCHEMA}.LINEAGE_EDGES "
        f"WHERE CONNECTION_ID = %(cid)s "
        f"  AND (SOURCE_TABLE = 'LIN_ORPHAN_TABLE' OR TARGET_TABLE = 'LIN_ORPHAN_TABLE')",
        {"cid": cid},
    )
    n_oe = cur.fetchone()[0]
    print(f"\n[5] Orphan — catalog rows: {n_cat},  edge rows: {n_oe}")
    _check("orphan in catalog",    n_cat, 1)
    _check("orphan has no edges",  n_oe,  0)

    # 6. copy_into edges
    cur.execute(
        f"SELECT COUNT(*) FROM {DB}.{APP_SCHEMA}.LINEAGE_EDGES "
        f"WHERE CONNECTION_ID = %(cid)s AND EDGE_TYPE = 'copy_into' "
        f"  AND SOURCE_SCHEMA = %(r)s",
        {"cid": cid, "r": V_RAW},
    )
    n_ci = cur.fetchone()[0]
    print(f"\n[6] copy_into edges from LIN_RAW: {n_ci}")
    _check("copy_into_edges = 2", n_ci, 2)

    # 7. Refresh state
    cur.execute(
        f"SELECT LAST_STATUS, EDGE_COUNT, DISCOVERY_METHOD_USED "
        f"FROM {DB}.{APP_SCHEMA}.LINEAGE_REFRESH_STATE "
        f"WHERE CONNECTION_ID = %(cid)s AND DATABASE_NAME = %(db)s",
        {"cid": cid, "db": DB},
    )
    rs = cur.fetchone()
    print(f"\n[7] LINEAGE_REFRESH_STATE: {rs}")
    if rs:
        _check("status = ok",      rs[0], "ok")
        _check(f"edge_count = {EXPECTED_EDGE_COUNT}", rs[1], EXPECTED_EDGE_COUNT)
    else:
        print("    [FAIL] no LINEAGE_REFRESH_STATE row found")

    # 8. Actual Snowflake objects created in TEST_DQ
    cur.execute(f"SHOW TABLES LIKE 'LIN\\_%' IN SCHEMA {DB}.{REAL_SC}")
    real_tables = [r[1] for r in cur.fetchall()]
    cur.execute(f"SHOW VIEWS LIKE 'LIN\\_%' IN SCHEMA {DB}.{REAL_SC}")
    real_views = [r[1] for r in cur.fetchall()]
    exp_real_tables = [o[3] for o in OBJECTS if o[2] == "table"]
    exp_real_views  = [o[3] for o in OBJECTS if o[2] == "view"]
    print(f"\n[8] Real objects in {REAL_SC}:")
    print(f"    tables: {real_tables}")
    print(f"    views:  {real_views}")
    _check(f"table count = {len(exp_real_tables)}",
           len(real_tables), len(exp_real_tables))
    _check(f"view count = {len(exp_real_views)}",
           len(real_views), len(exp_real_views))

    print("\n" + "=" * 62)
    print("MANUAL UI CHECKS (open the app and verify these)")
    print("=" * 62)
    lines = [
        "",
        f"  /lineage  (database landing cards)",
        f"    PLAYGROUND_DB card: edge_count={EXPECTED_EDGE_COUNT}, discovery_method=object_dependencies, status=ok",
        "",
        f"  /lineage/graph/PLAYGROUND_DB",
        f"    4 schema containers: LIN_RAW, LIN_BRONZE, LIN_SILVER, LIN_GOLD",
        f"    each containing nested tables/views with arrows between them",
        "",
        f"  /lineage/graph/PLAYGROUND_DB/LIN_SILVER",
        f"    4 nodes: LIN_CLEAN_EVENTS, LIN_CLEAN_USERS, LIN_USER_EVENTS, LIN_ORPHAN_TABLE",
        f"    LIN_ORPHAN_TABLE must render with NO arrows",
        "",
        f"  /lineage/table/PLAYGROUND_DB/LIN_GOLD/LIN_DAU?hops=3",
        f"    upstream: LIN_USER_EVENTS, LIN_CLEAN_EVENTS, LIN_CLEAN_USERS, LIN_EVENTS, LIN_USERS (5 nodes)",
        f"    LIN_STG_* must NOT appear (needs hops=4)",
        "",
        f"  /lineage/table/PLAYGROUND_DB/LIN_GOLD/LIN_DAU?hops=1",
        f"    only LIN_USER_EVENTS upstream, nothing else",
        "",
        f"  /lineage/table/PLAYGROUND_DB/LIN_SILVER/LIN_USER_EVENTS",
        f"    direction='upstream' on BOTH LIN_CLEAN_EVENTS AND LIN_CLEAN_USERS (diamond test)",
        "",
        f"  /lineage/table/PLAYGROUND_DB/LIN_GOLD/LIN_REVENUE",
        f"    upstream_count=2 (LIN_USER_EVENTS + LIN_PRODUCTS), downstream_count=1 (LIN_CROSS_VIEW)",
        "",
    ]
    print("\n".join(lines))


# ─── main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"Seeding lineage test data -> {DB}.{APP_SCHEMA}")
    print(f"Real objects will be created in {DB}.{REAL_SC} with LIN_ prefix")
    conn = connect()
    cur = conn.cursor()
    try:
        create_objects(cur)
        cid = get_connection_id(cur)
        clear_prior_seed(cur, cid)
        seed_catalog(cur, cid)
        n = seed_edges(cur, cid)
        seed_refresh_state(cur, cid, n)
        conn.commit()
        verify(cur, cid)
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\nFailed: {e}", file=sys.stderr)
        import traceback; traceback.print_exc()
        sys.exit(1)
