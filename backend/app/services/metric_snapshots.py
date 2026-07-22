"""
Anomaly Detection Tier A — metric snapshots + rolling MAD baselines.

On every scan the coordinator calls `record_metric_snapshots()` after
ProfilingAgent produces its facts. We persist a small set of numeric
metrics per (asset, column, scan) into METRIC_SNAPSHOTS and refresh the
rolling 30-day median + MAD into METRIC_BASELINES.

Downstream:
  - metric_anomaly / metric_relative_change / category_disappeared
    template rules read METRIC_SNAPSHOTS + METRIC_BASELINES.
  - AnomalyProposalAgent gates auto-proposals on
    `get_baseline(...).sample_count >= 14`.

Storage lives in this module (not storage.py) to keep the schema-drift
analogue clean — schema_drift.py does the same for its own tables.
"""
from __future__ import annotations

import json
import logging
import statistics
import uuid
from typing import Any, Dict, List, Optional

from app.services import storage
from app.services.snowflake_session import session as sf

logger = logging.getLogger(__name__)

# Rolling window (days) for baseline computation. Tier A uses raw
# sample count as the maturity gate; window bounds keep MAD from drifting
# on very old scans that no longer reflect current behavior.
_BASELINE_WINDOW_DAYS = 30
# Minimum samples before a baseline is considered "ready" for anomaly
# proposals. Matches future.md Tier A gate.
BASELINE_MIN_SAMPLES = 14

def _new_id() -> str:
    return str(uuid.uuid4())


def _coerce_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # Reject NaN / inf — they'd poison MAD.
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _extract_metrics(
    facts: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Turn a ProfilingAgent facts dict into a list of metric rows.

    Each row: {column_name, metric_name, metric_value, metric_meta}.
    column_name is None for table-level metrics."""
    rows: List[Dict[str, Any]] = []
    column_stats: Dict[str, Any] = facts.get("column_stats") or {}
    closed_sets: Dict[str, Any] = facts.get("closed_set_columns") or {}
    freshness: List[Dict[str, Any]] = facts.get("freshness_signals") or []

    # ── Table-level: row_count (best-effort — take the max non-null
    # total_count seen across columns; column_stats.total is populated
    # from the same underlying profile). ────────────────────────────
    max_total = None
    for stats in column_stats.values():
        if not isinstance(stats, dict):
            continue
        t = _coerce_float(stats.get("total"))
        if t is not None and (max_total is None or t > max_total):
            max_total = t
    if max_total is not None:
        rows.append({
            "column_name": None,
            "metric_name": "row_count",
            "metric_value": max_total,
            "metric_meta": None,
        })

    # ── Table-level: minimum freshness lag (age_days → hours). ──────
    min_age_days = None
    freshest_col = None
    for sig in freshness:
        age = _coerce_float(sig.get("age_days"))
        if age is None:
            continue
        if min_age_days is None or age < min_age_days:
            min_age_days = age
            freshest_col = sig.get("column")
    if min_age_days is not None:
        rows.append({
            "column_name": None,
            "metric_name": "freshness_lag_hours",
            "metric_value": min_age_days * 24.0,
            "metric_meta": {"source_column": freshest_col},
        })

    # ── Per-column metrics. Sourced from ProfilingAgent's column_stats,
    # which produces {total, nulls, null_pct, distinct, top_values,
    # tail_values, data_type, min_value, max_value, avg_value, stddev,
    # duplicate_count, pattern_match_pct}. Not every metric is meaningful
    # for every column — the type-check helpers below gate emission so
    # we don't push "mean" for a VARCHAR column. Enrollment gate later in
    # record_metric_snapshots drops anything not being tracked, so
    # emitting a superset here is cheap. ─────────────────────────────
    for col, stats in column_stats.items():
        if not isinstance(stats, dict):
            continue

        data_type = str(stats.get("data_type") or "").upper()
        is_numeric = any(
            data_type.startswith(p) for p in
            ("NUMBER", "INT", "BIGINT", "SMALLINT", "TINYINT",
             "FLOAT", "DOUBLE", "DECIMAL", "NUMERIC", "REAL")
        )
        is_string = any(
            data_type.startswith(p) for p in
            ("VARCHAR", "STRING", "TEXT", "CHAR")
        )

        total = _coerce_float(stats.get("total"))
        nulls = _coerce_float(stats.get("nulls"))
        distinct = _coerce_float(stats.get("distinct"))

        null_pct = _coerce_float(stats.get("null_pct"))
        if null_pct is not None:
            null_pct = max(0.0, min(100.0, null_pct))
            rows.append({"column_name": col, "metric_name": "null_pct",
                         "metric_value": null_pct, "metric_meta": None})

        if distinct is not None:
            rows.append({"column_name": col, "metric_name": "distinct_count",
                         "metric_value": distinct, "metric_meta": None})

        # ── Universal (any type) ──────────────────────────────────
        # duplicate_pct = 1 − (distinct / non_null_total). Signals repetition
        # regardless of type. Skip when non-null total is 0 (all nulls).
        if total is not None and nulls is not None and distinct is not None:
            non_null = total - nulls
            if non_null > 0:
                dup_pct = max(0.0, min(100.0, (1.0 - distinct / non_null) * 100.0))
                rows.append({"column_name": col, "metric_name": "duplicate_pct",
                             "metric_value": dup_pct, "metric_meta": None})

        # ── Numeric-only ──────────────────────────────────────────
        if is_numeric:
            avg = _coerce_float(stats.get("avg_value"))
            std = _coerce_float(stats.get("stddev"))
            mn = _coerce_float(stats.get("min_value"))
            mx = _coerce_float(stats.get("max_value"))
            if avg is not None:
                rows.append({"column_name": col, "metric_name": "mean",
                             "metric_value": avg, "metric_meta": None})
            if std is not None:
                rows.append({"column_name": col, "metric_name": "stddev",
                             "metric_value": std, "metric_meta": None})
            if mn is not None:
                rows.append({"column_name": col, "metric_name": "min_value",
                             "metric_value": mn, "metric_meta": None})
            if mx is not None:
                rows.append({"column_name": col, "metric_name": "max_value",
                             "metric_value": mx, "metric_meta": None})

        # ── String-only ───────────────────────────────────────────
        # avg_length / max_length are derived from top_values samples when the
        # profiler didn't compute them directly. Cheap: bounded to top_values.
        if is_string:
            tvs = stats.get("top_values") or []
            lengths = [len(str(t.get("value") or "")) for t in tvs if isinstance(t, dict)]
            if lengths:
                # Weight by count so avg reflects actual data distribution, not
                # just the top-value list.
                total_len = 0
                total_ct = 0
                for t in tvs:
                    v = t.get("value") if isinstance(t, dict) else None
                    ct = t.get("count") if isinstance(t, dict) else None
                    if v is None or ct is None:
                        continue
                    total_len += len(str(v)) * int(ct)
                    total_ct += int(ct)
                if total_ct > 0:
                    rows.append({
                        "column_name": col, "metric_name": "avg_length",
                        "metric_value": total_len / total_ct, "metric_meta": None,
                    })
                rows.append({
                    "column_name": col, "metric_name": "max_length",
                    "metric_value": float(max(lengths)), "metric_meta": None,
                })

        # ── Pattern conformity (email/phone/etc, deterministic) ────
        pattern_pct = _coerce_float(stats.get("pattern_match_pct"))
        if pattern_pct is not None:
            pattern_pct = max(0.0, min(100.0, pattern_pct))
            rows.append({"column_name": col, "metric_name": "pattern_match_pct",
                         "metric_value": pattern_pct, "metric_meta": None})

    # ── Per-column: observed closed-set. Stored under a synthetic
    # metric_name so the AnomalyProposalAgent can find them for
    # category_disappeared checks. metric_value is the cardinality;
    # metric_meta carries the observed value list. ─────────────────
    for col, info in closed_sets.items():
        if not isinstance(info, dict):
            continue
        values = info.get("values") or []
        rows.append({
            "column_name": col,
            "metric_name": "observed_categories",
            "metric_value": _coerce_float(info.get("distinct_count")) or float(len(values)),
            "metric_meta": {"values": list(values)},
        })

    return rows


# ── Per-column auto-seeding ─────────────────────────────────────────────
# Which metrics get auto-enrolled for a *column* on first observation. The
# rule: enroll when the value carries signal — a column that's always all-null
# doesn't need null_pct monitoring, a column with 0 distinct values isn't
# worth tracking distinct_count for. Users can always add anything manually.

def _column_metric_worth_tracking(metric_name: str, value: float) -> bool:
    if metric_name == "null_pct":
        # 0% null forever isn't interesting; 100% null column is dead — skip both.
        return 0.5 <= value <= 99.5
    if metric_name == "distinct_count":
        # Constant column (distinct=1) or empty (0) — skip.
        return value >= 2
    if metric_name == "duplicate_pct":
        # 0% dup on primary keys is expected; 100% on constants — skip both.
        return 1.0 <= value <= 99.0
    if metric_name in {"mean", "stddev", "min_value", "max_value",
                       "avg_length", "max_length", "pattern_match_pct",
                       "observed_categories"}:
        # These carry signal from any finite non-null value.
        return True
    return False


def _auto_seed_column_metrics(asset_id: str, extracted_rows: List[Dict[str, Any]]) -> int:
    """Enroll per-column metrics that clear the worth-tracking gate.
    Idempotent — no-op for already-enrolled (column, metric) pairs."""
    added = 0
    seen: set = set()
    for r in extracted_rows:
        col = r.get("column_name")
        if col is None:  # table-level already handled elsewhere
            continue
        metric = r["metric_name"]
        key = (col, metric)
        if key in seen:
            continue
        seen.add(key)
        v = _coerce_float(r.get("metric_value"))
        if v is None:
            continue
        if not _column_metric_worth_tracking(metric, v):
            continue
        try:
            if storage.enroll_metric(asset_id, col, metric, "auto_column", "auto_seed"):
                added += 1
        except Exception as exc:
            logger.debug(
                f"[MetricSnapshots] auto-enroll failed asset={asset_id} "
                f"col={col} metric={metric}: {exc}"
            )
    if added:
        logger.info(f"[MetricSnapshots] auto-enrolled {added} per-column metric(s) for asset={asset_id}")
    return added


def record_metric_snapshots(
    scan_id: str,
    asset_id: str,
    database_name: str,
    schema_name: str,
    table_name: str,
    facts: Dict[str, Any],
) -> int:
    """Persist metric snapshots for this scan and refresh baselines.

    Returns the number of snapshot rows written. Safe to call zero times
    (empty facts). Failures are logged and swallowed — the caller should
    not let anomaly-substrate capture block a scan from finalizing.
    """
    try:
        rows = _extract_metrics(facts or {})
    except Exception as exc:
        logger.warning(f"[MetricSnapshots] extract failed for scan={scan_id}: {exc}")
        return 0

    if not rows:
        return 0

    # Ensure the universal table-level metrics are enrolled for this asset
    # before we filter. First scan on a fresh asset seeds them here.
    try:
        storage.ensure_default_table_metrics(asset_id)
    except Exception as exc:
        logger.warning(f"[MetricSnapshots] default-metric enroll failed asset={asset_id}: {exc}")

    # Auto-seed per-column enrollments — deterministic gate on the profiler
    # values so we track columns that *actually vary*. First scan a column
    # produces meaningful stats, its per-column metrics enroll here.
    # Everything constant, all-null, or single-valued stays unenrolled.
    try:
        _auto_seed_column_metrics(asset_id, rows)
    except Exception as exc:
        logger.warning(f"[MetricSnapshots] auto-seed failed asset={asset_id}: {exc}")

    # Enrollment gate: only persist metrics someone (auto or user) opted into.
    # Everything else the profiler emits stays transient — no snapshot row, no
    # baseline. This is the ~90% storage cut vs. the old capture-everything path.
    try:
        monitored = storage.get_monitored_metric_set(asset_id)
    except Exception as exc:
        logger.warning(f"[MetricSnapshots] enrollment lookup failed asset={asset_id}: {exc}")
        monitored = set()
    if not monitored:
        # Nothing enrolled for this asset yet. ensure_default_table_metrics
        # above should have seeded row_count / freshness — an empty set means
        # the enrollment call failed and we'd write nothing anyway.
        return 0

    before = len(rows)
    rows = [r for r in rows if (r["column_name"], r["metric_name"]) in monitored]
    if len(rows) < before:
        logger.debug(
            f"[MetricSnapshots] filtered {before - len(rows)} non-enrolled "
            f"metric(s) for asset={asset_id}"
        )
    if not rows:
        return 0

    inserted = 0
    for r in rows:
        try:
            sf.execute(
                """
                INSERT INTO METRIC_SNAPSHOTS
                    (ID, SCAN_ID, ASSET_ID, DATABASE_NAME, SCHEMA_NAME, TABLE_NAME,
                     COLUMN_NAME, METRIC_NAME, METRIC_VALUE, METRIC_META)
                SELECT
                    %(id)s, %(scan_id)s, %(asset_id)s, %(db)s, %(sch)s, %(tbl)s,
                    %(col)s, %(metric)s, %(value)s, PARSE_JSON(%(meta)s)
                """,
                {
                    "id": _new_id(),
                    "scan_id": scan_id,
                    "asset_id": asset_id,
                    "db": database_name,
                    "sch": schema_name,
                    "tbl": table_name,
                    "col": r["column_name"],
                    "metric": r["metric_name"],
                    "value": r["metric_value"],
                    "meta": (json.dumps(r["metric_meta"], default=storage._json_default)
                            if r["metric_meta"] is not None else None),
                },
            )
            inserted += 1
        except Exception as exc:
            logger.warning(
                f"[MetricSnapshots] insert failed for scan={scan_id} "
                f"col={r.get('column_name')} metric={r.get('metric_name')}: {exc}"
            )

    # Refresh baselines inline so RuleIntelligence / AnomalyProposalAgent
    # can query fresh MADs immediately after the scan. Scans happen daily
    # on most tables, so no separate nightly scheduler is needed for Tier A.
    unique_pairs = {(r["column_name"], r["metric_name"]) for r in rows}
    for col, metric in unique_pairs:
        try:
            refresh_baseline(asset_id, col, metric)
        except Exception as exc:
            logger.warning(
                f"[MetricSnapshots] baseline refresh failed asset={asset_id} "
                f"col={col} metric={metric}: {exc}"
            )

    logger.info(
        f"[MetricSnapshots] {database_name}.{schema_name}.{table_name} "
        f"scan={scan_id}: {inserted} snapshot(s), "
        f"{len(unique_pairs)} baseline(s) refreshed"
    )
    return inserted


def _median_absolute_deviation(values: List[float], median: float) -> float:
    deviations = [abs(v - median) for v in values]
    return statistics.median(deviations) if deviations else 0.0


def refresh_baseline(
    asset_id: str,
    column_name: Optional[str],
    metric_name: str,
) -> None:
    """Recompute the rolling-30d median + MAD for one (asset, column, metric)
    and upsert into METRIC_BASELINES. `observed_categories` is treated
    specially: instead of a numeric baseline, the union of all values seen
    across the window is stored under OBSERVED_SET.
    """
    col_predicate = "COLUMN_NAME = %(col)s" if column_name is not None else "COLUMN_NAME IS NULL"
    params = {
        "asset": asset_id,
        "metric": metric_name,
        "days": _BASELINE_WINDOW_DAYS,
    }
    if column_name is not None:
        params["col"] = column_name

    rows = sf.query(
        f"""
        SELECT METRIC_VALUE, METRIC_META, CAPTURED_AT
        FROM METRIC_SNAPSHOTS
        WHERE ASSET_ID = %(asset)s
          AND METRIC_NAME = %(metric)s
          AND {col_predicate}
          AND CAPTURED_AT >= DATEADD(day, -%(days)s, CURRENT_TIMESTAMP())
        ORDER BY CAPTURED_AT ASC
        """,
        params,
    )
    if not rows:
        return

    numeric_values: List[float] = []
    observed_union: set = set()
    window_start = None
    window_end = None
    for r in rows:
        v = _coerce_float(r.get("METRIC_VALUE"))
        if v is not None:
            numeric_values.append(v)
        meta = r.get("METRIC_META")
        if meta is not None:
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    meta = None
            if isinstance(meta, dict):
                for val in (meta.get("values") or []):
                    observed_union.add(str(val))
        ts = r.get("CAPTURED_AT")
        if ts is not None:
            if window_start is None or ts < window_start:
                window_start = ts
            if window_end is None or ts > window_end:
                window_end = ts

    if metric_name == "observed_categories":
        median_value = None
        mad_value = None
        observed_set_json = json.dumps(sorted(observed_union))
    else:
        if not numeric_values:
            return
        median_value = statistics.median(numeric_values)
        mad_value = _median_absolute_deviation(numeric_values, median_value)
        observed_set_json = None

    sample_count = len(rows)

    # Upsert. Snowflake supports MERGE, but a two-step (DELETE + INSERT)
    # keeps this readable and matches how other helpers in storage.py
    # perform full-row replacement.
    delete_params = {"asset": asset_id, "metric": metric_name}
    if column_name is not None:
        delete_params["col"] = column_name
        sf.execute(
            """
            DELETE FROM METRIC_BASELINES
            WHERE ASSET_ID = %(asset)s
              AND METRIC_NAME = %(metric)s
              AND COLUMN_NAME = %(col)s
            """,
            delete_params,
        )
    else:
        sf.execute(
            """
            DELETE FROM METRIC_BASELINES
            WHERE ASSET_ID = %(asset)s
              AND METRIC_NAME = %(metric)s
              AND COLUMN_NAME IS NULL
            """,
            delete_params,
        )

    sf.execute(
        """
        INSERT INTO METRIC_BASELINES
            (ID, ASSET_ID, COLUMN_NAME, METRIC_NAME, MEDIAN_VALUE, MAD_VALUE,
             SAMPLE_COUNT, OBSERVED_SET, WINDOW_START, WINDOW_END)
        SELECT
            %(id)s, %(asset)s, %(col)s, %(metric)s, %(median)s, %(mad)s,
            %(sample)s, PARSE_JSON(%(obs)s), %(ws)s, %(we)s
        """,
        {
            "id": _new_id(),
            "asset": asset_id,
            "col": column_name,
            "metric": metric_name,
            "median": median_value,
            "mad": mad_value,
            "sample": sample_count,
            "obs": observed_set_json,
            "ws": window_start,
            "we": window_end,
        },
    )


def get_baseline(
    asset_id: str,
    column_name: Optional[str],
    metric_name: str,
) -> Optional[Dict[str, Any]]:
    """Return the latest baseline row for one (asset, column, metric), or
    None if none exists. Used by AnomalyProposalAgent for the 14-sample
    gate and by anomaly rule SQL for the median/MAD comparison."""
    col_predicate = "COLUMN_NAME = %(col)s" if column_name is not None else "COLUMN_NAME IS NULL"
    params = {"asset": asset_id, "metric": metric_name}
    if column_name is not None:
        params["col"] = column_name
    rows = sf.query(
        f"""
        SELECT * FROM METRIC_BASELINES
        WHERE ASSET_ID = %(asset)s
          AND METRIC_NAME = %(metric)s
          AND {col_predicate}
        ORDER BY UPDATED_AT DESC
        LIMIT 1
        """,
        params,
    )
    if not rows:
        return None
    r = rows[0]
    observed = r.get("OBSERVED_SET")
    if isinstance(observed, str):
        try:
            observed = json.loads(observed)
        except Exception:
            observed = None
    return {
        "asset_id": r["ASSET_ID"],
        "column_name": r.get("COLUMN_NAME"),
        "metric_name": r["METRIC_NAME"],
        "median": _coerce_float(r.get("MEDIAN_VALUE")),
        "mad": _coerce_float(r.get("MAD_VALUE")),
        "sample_count": int(r.get("SAMPLE_COUNT") or 0),
        "observed_set": observed,
        "window_start": r.get("WINDOW_START"),
        "window_end": r.get("WINDOW_END"),
    }


def list_ready_baselines(asset_id: str) -> List[Dict[str, Any]]:
    """Baselines with >= BASELINE_MIN_SAMPLES observations for this asset —
    the eligible set for anomaly proposals."""
    rows = sf.query(
        """
        SELECT * FROM METRIC_BASELINES
        WHERE ASSET_ID = %(asset)s
          AND SAMPLE_COUNT >= %(min_samples)s
        """,
        {"asset": asset_id, "min_samples": BASELINE_MIN_SAMPLES},
    )
    out: List[Dict[str, Any]] = []
    for r in rows:
        observed = r.get("OBSERVED_SET")
        if isinstance(observed, str):
            try:
                observed = json.loads(observed)
            except Exception:
                observed = None
        out.append({
            "asset_id": r["ASSET_ID"],
            "column_name": r.get("COLUMN_NAME"),
            "metric_name": r["METRIC_NAME"],
            "median": _coerce_float(r.get("MEDIAN_VALUE")),
            "mad": _coerce_float(r.get("MAD_VALUE")),
            "sample_count": int(r.get("SAMPLE_COUNT") or 0),
            "observed_set": observed,
        })
    return out
