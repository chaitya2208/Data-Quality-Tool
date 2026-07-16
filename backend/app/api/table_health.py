"""Per-table Data Health view.

Aggregates RULE_INSTANCES × latest RULE_EXECUTIONS × open FINDINGS for a
single (database, schema, table). Powers the Data Health tab on the Data
Explorer page.

Health score formula: severity-weighted pass rate over the last N executions
per instance, then a weighted average across all active instances on the
table. Weights — critical=5, high=3, medium=2, low=1, info=1.
"""
from fastapi import APIRouter
from typing import Optional
from collections import defaultdict
from app.services import storage
from app.services.snowflake_session import session as sf_session

router = APIRouter()

SEVERITY_WEIGHT = {"critical": 5, "high": 3, "medium": 2, "low": 1, "info": 1}
HISTORY_LIMIT = 20  # last N executions per instance for pass-rate + sparkline


def _columns_for_instance(inst) -> list[str]:
    """Instance's target_config carries the column list under a few historic
    keys — accept all of them so older instances still map correctly."""
    tc = inst.target_config or {}
    if isinstance(tc.get("column"), str):
        return [tc["column"]]
    for k in ("columns", "target_columns", "column_names"):
        v = tc.get(k)
        if isinstance(v, list):
            return [c for c in v if isinstance(c, str)]
    return []


def _status_dot(latest_status: Optional[str]) -> str:
    if latest_status == "passed":
        return "green"
    if latest_status in ("failed", "error"):
        return "red"
    if latest_status is None:
        return "gray"
    return "amber"


@router.get("/fleet/overview")
def get_fleet_overview(connection_id: Optional[str] = None, days: int = 30, top_n: int = 8):
    """Fleet-wide dashboard aggregation: overall health score, per-table
    ranked list, open incidents count, flapping incidents count, and a daily
    pass-rate trend across all rules on all tables.

    - Health score: severity-weighted pass rate across every RULE_EXECUTIONS
      row in the window (same formula as per-table, but fleet-wide).
    - Worst tables: ranked by (open incidents desc, health-score asc). Only
      tables that have at least one execution in the window appear.
    - Flapping: findings with REOPENED_COUNT > 0 currently open.
    """
    days = max(1, min(int(days), 365))
    top_n = max(1, min(int(top_n), 50))

    conn_filter_scan = ""
    conn_filter_scan_alias = ""
    params: dict = {"days": days}
    if connection_id:
        conn_filter_scan = "AND S.CONNECTION_ID = %(conn)s"
        conn_filter_scan_alias = "AND S2.CONNECTION_ID = %(conn)s"
        params["conn"] = connection_id

    # ── Overall trend (daily pass-rate + failed-run count) ──────────────
    trend_rows = sf_session.query(
        f"""
        SELECT
            TO_CHAR(DATE_TRUNC('DAY', E.EXECUTED_AT), 'YYYY-MM-DD') AS DAY,
            E.STATUS,
            COUNT(*) AS N
        FROM RULE_EXECUTIONS E
        LEFT JOIN SCANS S ON S.ID = E.SCAN_ID
        WHERE E.EXECUTED_AT >= DATEADD('day', -%(days)s, CURRENT_TIMESTAMP())
        {conn_filter_scan}
        GROUP BY 1, 2 ORDER BY 1
        """, params,
    )
    by_day: dict = defaultdict(lambda: {"passed": 0, "failed": 0, "error": 0})
    for r in trend_rows:
        day = r["DAY"]; st = (r["STATUS"] or "").lower()
        if st in ("passed", "failed", "error"):
            by_day[day][st] += int(r["N"] or 0)
    trend = []
    total_passed = total_all = 0
    for day, c in sorted(by_day.items()):
        total = c["passed"] + c["failed"] + c["error"]
        trend.append({
            "day": day, "passed": c["passed"], "failed": c["failed"],
            "error": c["error"], "total": total,
            "pass_rate": (c["passed"] / total) if total else None,
        })
        total_passed += c["passed"]; total_all += total
    overall_health = (total_passed / total_all) if total_all else None

    # ── Per-table aggregation: exec counts + latest-status by instance ──
    # Group by (database_name, schema_name, table_name) via RULE_INSTANCES
    # to sidestep the FQN parsing hell in FINDINGS/ASSETS joins.
    table_rows = sf_session.query(
        f"""
        SELECT
            I.DATABASE_NAME AS DB, I.SCHEMA_NAME AS SC, I.TABLE_NAME AS TB,
            E.STATUS AS STATUS,
            COUNT(*) AS N
        FROM RULE_EXECUTIONS E
        JOIN RULE_INSTANCES I ON I.ID = E.INSTANCE_ID
        LEFT JOIN SCANS S ON S.ID = E.SCAN_ID
        WHERE E.EXECUTED_AT >= DATEADD('day', -%(days)s, CURRENT_TIMESTAMP())
        {conn_filter_scan}
        GROUP BY 1, 2, 3, 4
        """, params,
    )
    per_table: dict = defaultdict(lambda: {"passed": 0, "failed": 0, "error": 0})
    for r in table_rows:
        key = (r["DB"], r["SC"], r["TB"])
        st = (r["STATUS"] or "").lower()
        if st in ("passed", "failed", "error"):
            per_table[key][st] += int(r["N"] or 0)

    # Open + flapping incident counts per table.
    incident_rows = sf_session.query(
        f"""
        SELECT
            I.DATABASE_NAME AS DB, I.SCHEMA_NAME AS SC, I.TABLE_NAME AS TB,
            COUNT_IF(F.STATUS IN ('detected','validated','in_progress','assigned','acknowledged')) AS OPEN_CT,
            COUNT_IF(F.STATUS IN ('detected','validated','in_progress','assigned','acknowledged')
                     AND COALESCE(F.REOPENED_COUNT, 0) > 0) AS FLAP_CT,
            MIN(CASE WHEN F.STATUS IN ('detected','validated','in_progress','assigned','acknowledged')
                     THEN F.FIRST_DETECTED_AT END) AS OLDEST_OPEN_AT
        FROM FINDINGS F
        JOIN RULE_INSTANCES I ON I.ID = F.INSTANCE_ID
        LEFT JOIN SCANS S2 ON S2.ID = F.SCAN_ID
        WHERE 1=1 {conn_filter_scan_alias}
        GROUP BY 1, 2, 3
        """, params,
    )
    open_by_table: dict = {}
    flap_by_table: dict = {}
    oldest_by_table: dict = {}
    fleet_open = 0
    fleet_flap = 0
    fleet_oldest_open_at = None
    for r in incident_rows:
        key = (r["DB"], r["SC"], r["TB"])
        open_by_table[key]   = int(r["OPEN_CT"] or 0)
        flap_by_table[key]   = int(r["FLAP_CT"] or 0)
        oldest_by_table[key] = r["OLDEST_OPEN_AT"]
        fleet_open += int(r["OPEN_CT"] or 0)
        fleet_flap += int(r["FLAP_CT"] or 0)
        if r["OLDEST_OPEN_AT"] and (fleet_oldest_open_at is None or r["OLDEST_OPEN_AT"] < fleet_oldest_open_at):
            fleet_oldest_open_at = r["OLDEST_OPEN_AT"]

    # Merge into per-table rows and rank.
    tables = []
    all_keys = set(per_table.keys()) | set(open_by_table.keys())
    for key in all_keys:
        c = per_table.get(key, {"passed": 0, "failed": 0, "error": 0})
        total = c["passed"] + c["failed"] + c["error"]
        pass_rate = (c["passed"] / total) if total else None
        tables.append({
            "database": key[0], "schema": key[1], "table": key[2],
            "runs": total, "passed": c["passed"], "failed": c["failed"], "error": c["error"],
            "pass_rate": pass_rate,
            "open_findings": open_by_table.get(key, 0),
            "flapping": flap_by_table.get(key, 0),
            "oldest_open_at": oldest_by_table.get(key).isoformat() if oldest_by_table.get(key) else None,
        })
    # Worst first: most open incidents, then lowest pass-rate.
    tables.sort(key=lambda t: (
        -(t["open_findings"] or 0),
        (t["pass_rate"] if t["pass_rate"] is not None else 1.0),
        -(t["failed"] or 0),
    ))

    return {
        "days": days,
        "overall_health_score": overall_health,
        "fleet_open_findings": fleet_open,
        "fleet_flapping_findings": fleet_flap,
        "fleet_oldest_open_at": fleet_oldest_open_at.isoformat() if fleet_oldest_open_at else None,
        "trend": trend,
        "tables": tables[:top_n],
        "tables_total": len(tables),
    }


@router.get("/{database}/{schema}/{table}")
def get_table_health(database: str, schema: str, table: str):
    _, instances = storage.list_instances(
        database_name=database, schema_name=schema, table_name=table,
        is_active=True, limit=1000,
    )

    # Definitions in one batch (avoid N+1)
    def_ids = list({i.definition_id for i in instances})
    definitions = storage.get_definitions_by_ids(def_ids) if def_ids else {}

    # Table asset resolved once — feeds open-finding + mute lookups per rule.
    table_asset = storage.get_table_asset(database, schema, table)

    rules_out = []
    last_run_at = None
    total_weight = 0.0
    weighted_pass = 0.0
    per_column_status: dict[str, str] = defaultdict(lambda: "gray")
    # green > amber > red > gray priority-wise for column rollup:
    # actually worst-status wins so users notice failures. red > amber > gray > green
    STATUS_RANK = {"red": 3, "amber": 2, "gray": 1, "green": 0}
    column_worst: dict[str, str] = {}

    for inst in instances:
        defn = definitions.get(inst.definition_id)
        executions = storage.list_executions_for_instance(inst.id, limit=HISTORY_LIMIT)
        latest = executions[0] if executions else None
        history = list(reversed(executions))  # oldest → newest for sparkline

        passes = sum(1 for e in executions if e.status == "passed")
        fails  = sum(1 for e in executions if e.status == "failed")
        errors = sum(1 for e in executions if e.status == "error")
        total = passes + fails + errors
        pass_rate = (passes / total) if total else None

        sev = (inst.severity or "medium").lower()
        weight = SEVERITY_WEIGHT.get(sev, 2)
        if pass_rate is not None:
            total_weight += weight
            weighted_pass += weight * pass_rate

        if latest and (last_run_at is None or (latest.executed_at and latest.executed_at > last_run_at)):
            last_run_at = latest.executed_at

        dot = _status_dot(latest.status if latest else None)
        cols = _columns_for_instance(inst)
        for c in cols:
            prev = column_worst.get(c, "green")
            if STATUS_RANK[dot] > STATUS_RANK[prev]:
                column_worst[c] = dot

        # Enrich with the open finding's lifecycle data (if any) so the panel
        # can show "failing for 3 days" + flapping badges per rule.
        open_finding = storage.find_open_finding(inst.id, table_asset.id) if table_asset else None
        first_detected_at = open_finding.first_detected_at if open_finding else None
        reopened_count    = open_finding.reopened_count if open_finding else 0
        current_fail_count  = open_finding.current_fail_count if open_finding else None
        current_total_count = open_finding.current_total_count if open_finding else None
        muted = storage.is_muted(inst.id, table_asset.id) if table_asset else False

        rules_out.append({
            "instance_id": inst.id,
            "definition_id": inst.definition_id,
            "name": defn.name if defn else "(unknown)",
            "category": defn.category if defn else None,
            "check_kind": defn.check_kind if defn else None,
            "severity": sev,
            "columns": cols,
            "owner": inst.owner,
            "latest_status": latest.status if latest else None,
            "last_executed_at": latest.executed_at.isoformat() if latest and latest.executed_at else None,
            "pass_count": passes,
            "fail_count": fails,
            "error_count": errors,
            "total_runs": total,
            "pass_rate": pass_rate,
            "history": [
                {"status": e.status, "at": e.executed_at.isoformat() if e.executed_at else None}
                for e in history
            ],
            "first_detected_at": first_detected_at.isoformat() if first_detected_at else None,
            "reopened_count": reopened_count,
            "current_fail_count": current_fail_count,
            "current_total_count": current_total_count,
            "open_finding_id": open_finding.id if open_finding else None,
            "muted": muted,
        })

    # Sort: failing/erroring first, then by severity, then name
    STATUS_SORT = {"failed": 0, "error": 1, None: 2, "passed": 3}
    SEV_SORT    = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    rules_out.sort(key=lambda r: (
        STATUS_SORT.get(r["latest_status"], 2),
        SEV_SORT.get(r["severity"], 2),
        r["name"] or "",
    ))

    health_score = (weighted_pass / total_weight) if total_weight else None

    # Open findings for this table's asset
    open_findings_count = _count_open_findings(table_asset.id) if table_asset else 0

    return {
        "database": database,
        "schema": schema,
        "table": table,
        "asset_id": table_asset.id if table_asset else None,
        "health_score": health_score,   # 0.0–1.0 or null
        "rules_total": len(rules_out),
        "rules_failing": sum(1 for r in rules_out if r["latest_status"] in ("failed", "error")),
        "rules_passing": sum(1 for r in rules_out if r["latest_status"] == "passed"),
        "rules_unrun":   sum(1 for r in rules_out if r["latest_status"] is None),
        "open_findings": open_findings_count,
        "last_run_at": last_run_at.isoformat() if last_run_at else None,
        "column_status": column_worst,  # {column_name: "green"|"amber"|"red"|"gray"}
        "rules": rules_out,
    }


@router.get("/{database}/{schema}/{table}/history")
def get_table_health_history(database: str, schema: str, table: str, days: int = 30):
    """Daily pass/fail/error counts over the last `days` — feeds a LineChart on
    the Data Health tab. Aggregates RULE_EXECUTIONS for every active instance
    on the table, one row per (day, status).
    """
    days = max(1, min(int(days), 365))
    _, instances = storage.list_instances(
        database_name=database, schema_name=schema, table_name=table,
        is_active=True, limit=1000,
    )
    instance_ids = [i.id for i in instances]
    if not instance_ids:
        return {"days": days, "series": []}

    placeholders = ", ".join(f"%(iid{n})s" for n in range(len(instance_ids)))
    params = {f"iid{n}": iid for n, iid in enumerate(instance_ids)}
    params["days"] = days
    rows = sf_session.query(
        f"""
        SELECT
            TO_CHAR(DATE_TRUNC('DAY', EXECUTED_AT), 'YYYY-MM-DD') AS DAY,
            STATUS,
            COUNT(*) AS N
        FROM RULE_EXECUTIONS
        WHERE INSTANCE_ID IN ({placeholders})
          AND EXECUTED_AT >= DATEADD('day', -%(days)s, CURRENT_TIMESTAMP())
        GROUP BY 1, 2
        ORDER BY 1
        """,
        params,
    )

    by_day: dict = defaultdict(lambda: {"passed": 0, "failed": 0, "error": 0})
    for r in rows:
        day = r["DAY"]
        st = (r["STATUS"] or "").lower()
        if st not in ("passed", "failed", "error"):
            continue
        by_day[day][st] += int(r["N"] or 0)

    series = []
    for day, counts in sorted(by_day.items()):
        total = counts["passed"] + counts["failed"] + counts["error"]
        series.append({
            "day": day,
            "passed": counts["passed"],
            "failed": counts["failed"],
            "error":  counts["error"],
            "total":  total,
            "pass_rate": (counts["passed"] / total) if total else None,
        })

    return {"days": days, "series": series}


def _count_open_findings(asset_id: str) -> int:
    """Count findings for an asset that are still open (lifecycle-open)."""
    OPEN_STATUSES = ("detected", "validated", "in_progress", "assigned", "acknowledged")
    total = 0
    for st in OPEN_STATUSES:
        t, _ = storage.list_findings(asset_id=asset_id, status=st, limit=1)
        total += t
    return total
