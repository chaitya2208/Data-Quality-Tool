"""
Real fingerprinting for RULE_INSTANCES dedup — replaces the old exact-CODE-
string matching. Two instances are "the same check" if they share the same
definition, scope, target, and threshold config, regardless of when or how
many times they were suggested.
"""
import hashlib
import json
from typing import Optional


def _canonical_json(value: Optional[dict]) -> str:
    return json.dumps(value or {}, sort_keys=True, separators=(",", ":"))


def compute_fingerprint(
    definition_id: str,
    scope: str,
    database_name: str,
    schema_name: Optional[str],
    table_name: Optional[str],
    target_config: Optional[dict],
    threshold_config: Optional[dict] = None,
) -> str:
    parts = "|".join([
        definition_id,
        scope,
        database_name or "",
        schema_name or "",
        table_name or "",
        _canonical_json(target_config),
        _canonical_json(threshold_config),
    ])
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()
