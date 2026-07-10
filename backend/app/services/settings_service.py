"""
Settings service — typed, defaulted access to tunable app preferences.

SETTINGS_SPEC is the single source of truth: each entry defines the default,
type, and allowed range. Backend code reads values through the typed getters
(e.g. get_top_values_max_distinct) so an unset/invalid stored value transparently
falls back to the code default. The API exposes get_all()/update() for the UI.
"""
import logging
from typing import Any, Dict

from app.services import storage

logger = logging.getLogger(__name__)

# key -> {default, type, min, max, label, help}
SETTINGS_SPEC: Dict[str, Dict[str, Any]] = {
    "top_values_max_distinct": {
        "default": 50, "type": "int", "min": 1, "max": 10000,
        "label": "Top-values max distinct",
        "help": "Skip fetching top values for columns with more distinct values than this (a full GROUP BY scan is wasteful on high-cardinality columns).",
    },
    "outlier_stddev_mult": {
        "default": 4.0, "type": "float", "min": 1.0, "max": 20.0,
        "label": "Outlier sensitivity (× stddev)",
        "help": "Flag a numeric value as an outlier when it is this many standard deviations from the mean. Lower = more sensitive.",
    },
    "categorical_max_distinct": {
        "default": 15, "type": "int", "min": 1, "max": 1000,
        "label": "Categorical threshold (distinct)",
        "help": "A numeric column with at most this many distinct values is treated as categorical rather than a measure.",
    },
    "auto_verify_interval_min": {
        "default": 5, "type": "int", "min": 1, "max": 1440,
        "label": "Auto-verify interval (minutes)",
        "help": "How often the workflow re-checks findings while awaiting fixes.",
    },
}


def _coerce(spec: Dict[str, Any], raw: Any) -> Any:
    try:
        val = float(raw) if spec["type"] == "float" else int(raw)
    except (TypeError, ValueError):
        return spec["default"]
    val = max(spec["min"], min(spec["max"], val))
    return val


def get_all(db=None) -> Dict[str, Any]:
    """Return every setting with its effective (stored-or-default) value + metadata.

    `db` is accepted and ignored (kept for API call-site compatibility during
    the ORM→storage migration)."""
    stored = storage.get_all_settings()
    out = {}
    for key, spec in SETTINGS_SPEC.items():
        raw = stored.get(key, spec["default"])
        out[key] = {
            "value": _coerce(spec, raw),
            "default": spec["default"],
            "type": spec["type"],
            "min": spec["min"],
            "max": spec["max"],
            "label": spec["label"],
            "help": spec["help"],
        }
    return out


def update(updates: Dict[str, Any], db=None) -> Dict[str, Any]:
    """Persist a batch of {key: value}. Unknown keys ignored; values coerced/clamped.

    `db` is accepted and ignored (see get_all)."""
    for key, raw in updates.items():
        spec = SETTINGS_SPEC.get(key)
        if not spec:
            continue
        val = _coerce(spec, raw)
        storage.upsert_setting(key, val)
    return get_all()


def _get(key: str) -> Any:
    """Internal typed read used by backend code."""
    spec = SETTINGS_SPEC[key]
    try:
        raw = storage.get_setting(key)
        return _coerce(spec, raw) if raw is not None else spec["default"]
    except Exception:
        return spec["default"]


# ── Typed getters for backend code ─────────────────────────────────────────────
def get_top_values_max_distinct() -> int:   return int(_get("top_values_max_distinct"))
def get_outlier_stddev_mult() -> float:      return float(_get("outlier_stddev_mult"))
def get_categorical_max_distinct() -> int:   return int(_get("categorical_max_distinct"))
def get_auto_verify_interval_seconds() -> int: return int(_get("auto_verify_interval_min")) * 60
