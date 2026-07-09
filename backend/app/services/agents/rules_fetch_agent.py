"""
Rules Fetch Agent — loads all active rules from storage.

Runs in parallel with MetadataAgent. Lightweight — just a couple of queries.
Exists as a named agent so it shows in the UI pipeline.
"""
import logging
from typing import List, Any

from app.services.rule_engine import RuleEngine

logger = logging.getLogger(__name__)


class RulesFetchAgent:
    def __init__(self):
        pass

    def run(self) -> List[Any]:
        engine = RuleEngine()
        table_rules  = engine.get_active_rules("table")
        column_rules = engine.get_active_rules("column")

        # Deduplicate
        seen, unique = set(), []
        for r in table_rules + column_rules:
            if r.code not in seen:
                seen.add(r.code)
                unique.append(r)

        logger.info(f"[RulesFetchAgent] Loaded {len(unique)} active rules")
        return unique
