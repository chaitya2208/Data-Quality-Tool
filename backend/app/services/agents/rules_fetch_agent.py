"""
Rules Fetch Agent — loads all active rules from DB.

Runs in parallel with MetadataAgent. Lightweight — just a DB query.
Exists as a named agent so it shows in the UI pipeline.
"""
import logging
from typing import List
from sqlalchemy.orm import Session

from app.models.rule import Rule
from app.services.rule_engine import RuleEngine

logger = logging.getLogger(__name__)


class RulesFetchAgent:
    def __init__(self, db: Session):
        self.db = db

    def run(self) -> List[Rule]:
        engine = RuleEngine(self.db)
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
