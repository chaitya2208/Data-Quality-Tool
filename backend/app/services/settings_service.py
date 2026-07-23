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
# String settings omit min/max and may set default to "" (empty = not configured).
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
    # UI-configurable overrides for the SSO session. Empty means "fall back to
    # SNOWFLAKE_ROLE / SNOWFLAKE_WAREHOUSE from .env". snowflake_session.connect()
    # applies these on startup, and settings_service.update() re-applies them
    # live so the change takes effect without a backend restart.
    "default_role": {
        "default": "", "type": "str",
        "label": "Default role",
        "help": "Snowflake role to switch to after login. Leave empty to use the .env / user default.",
    },
    "default_warehouse": {
        "default": "", "type": "str",
        "label": "Default warehouse",
        "help": "Snowflake warehouse to use for the app-storage session. Leave empty to use the .env value.",
    },
}


def _coerce(spec: Dict[str, Any], raw: Any) -> Any:
    if spec["type"] == "str":
        if raw is None:
            return spec["default"]
        return str(raw).strip()
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
        entry = {
            "value": _coerce(spec, raw),
            "default": spec["default"],
            "type": spec["type"],
            "label": spec["label"],
            "help": spec["help"],
        }
        if "min" in spec: entry["min"] = spec["min"]
        if "max" in spec: entry["max"] = spec["max"]
        out[key] = entry
    return out


def update(updates: Dict[str, Any], db=None) -> Dict[str, Any]:
    """Persist a batch of {key: value}. Unknown keys ignored; values coerced/clamped.

    `db` is accepted and ignored (see get_all)."""
    session_touched = False
    for key, raw in updates.items():
        spec = SETTINGS_SPEC.get(key)
        if not spec:
            continue
        val = _coerce(spec, raw)
        storage.upsert_setting(key, val)
        if key in ("default_role", "default_warehouse"):
            session_touched = True
    # Live-apply the new role/warehouse on the shared session so the change
    # takes effect without a backend restart. Best-effort — a bad value logs
    # and leaves the current session state intact.
    if session_touched:
        try:
            from app.services.snowflake_session import session as sf_session
            sf_session.apply_session_defaults()
        except Exception as e:
            logger.warning(f"apply_session_defaults failed: {e}")
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
def get_default_role() -> str:      return str(_get("default_role") or "").strip()
def get_default_warehouse() -> str: return str(_get("default_warehouse") or "").strip()
