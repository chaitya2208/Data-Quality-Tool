"""
Rules Fetch Agent — loads the rule definition library (ACTIVE definitions
only) from storage.

Runs in parallel with MetadataAgent. Lightweight — just a couple of queries.
Exists as a named agent so it shows in the UI pipeline.
"""
import logging
from typing import List, Any

from app.services import storage

logger = logging.getLogger(__name__)


class RulesFetchAgent:
    def __init__(self):
        pass

    def run(self) -> List[Any]:
        """Returns the ACTIVE definition library — the concepts Claude should
        reason about, not per-table instances."""
        definitions = storage.list_active_definitions()
        logger.info(f"[RulesFetchAgent] Loaded {len(definitions)} active definitions")
        return definitions
