"""
Rules Fetch Agent — loads the rule definition library (ACTIVE definitions
only) from storage.

Runs in parallel with MetadataAgent. Lightweight — just a couple of queries.
Exists as a named agent so it shows in the UI pipeline.
"""
import logging
import re
from typing import List, Any, Optional

from app.services import storage

logger = logging.getLogger(__name__)

# Cap on the number of definitions injected into the RuleIntelligence prompt
# once relevance filtering is enforced. Above this the reuse-loop pressure
# starts crowding out novel proposals — the whole point of the filter.
_RELEVANCE_INJECTION_CAP = 30
# Word count for splitting definition names/descriptions into tokens.
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]+")


class RulesFetchAgent:
    def __init__(self):
        pass

    def run(self) -> List[Any]:
        """Returns the ACTIVE definition library — the concepts Claude should
        reason about, not per-table instances."""
        definitions = storage.list_active_definitions()
        logger.info(f"[RulesFetchAgent] Loaded {len(definitions)} active definitions")
        return definitions

    def filter_relevant(
        self,
        definitions: List[Any],
        table_asset: Any,
        column_assets: Optional[List[Any]] = None,
        cap: int = _RELEVANCE_INJECTION_CAP,
    ) -> List[Any]:
        """Score each definition against this table and return the top `cap`.

        Not called from the coordinator yet — the library is small enough that
        injecting all definitions into the RuleIntelligence prompt is fine.
        When the library crosses ~30 definitions the reuse-loop pressure
        starts crowding out novel proposals, and this becomes the natural
        plug-in point: coordinator would call
            defs = rules_fetch.run()
            defs = rules_fetch.filter_relevant(defs, table_asset, column_assets)
        before passing `defs` to RuleIntelligenceAgent.

        Scoring signals (each contributes to a per-definition score):
        - table_type_match: definition's typical target table type matches
          this table's classification (fact/dim/etc). Currently a no-op —
          table_type isn't known at fetch time; we'd need to defer this to
          after RuleIntelligence's classification pass or store per-def type
          affinity on RULE_DEFINITIONS.
        - column_name_overlap: any word in the definition's name or
          description also appears in one of this table's column names. A
          coarse but portable signal — "email format" matches a table with
          an EMAIL column.
        - historical_approval_rate: approvals / (approvals + rejections) on
          RULE_DEFINITIONS (audit finding #8 already keeps these counters).
          Definitions with strong track records rank higher.
        """
        if len(definitions) <= cap:
            return definitions

        column_tokens = self._column_tokens(column_assets or [])
        scored: List[tuple[float, Any]] = []
        for d in definitions:
            score = 0.0

            # column name overlap
            def_tokens = self._tokens(f"{d.name} {getattr(d, 'description', '') or ''}")
            overlap = column_tokens & def_tokens
            if overlap:
                score += min(len(overlap), 5) * 2.0  # cap contribution; diminishing returns

            # historical approval rate — bounded to [0, 1] contribution
            approvals = getattr(d, "approval_count", 0) or 0
            rejections = getattr(d, "rejection_count", 0) or 0
            total_decisions = approvals + rejections
            if total_decisions > 0:
                score += (approvals / total_decisions)

            scored.append((score, d))

        scored.sort(key=lambda x: x[0], reverse=True)
        kept = [d for _, d in scored[:cap]]
        logger.info(
            f"[RulesFetchAgent] Relevance filter: {len(definitions)} → {len(kept)} "
            f"for {table_asset.fqn} (top scores: {[round(s, 2) for s, _ in scored[:3]]})"
        )
        return kept

    @staticmethod
    def _tokens(text: str) -> set:
        return {t.lower() for t in _TOKEN_RE.findall(text or "")}

    @classmethod
    def _column_tokens(cls, column_assets: List[Any]) -> set:
        tokens: set = set()
        for c in column_assets:
            name = getattr(c, "column_name", "") or ""
            # split on underscore so USER_EMAIL contributes user + email
            for part in name.replace("_", " ").split():
                tokens.add(part.lower())
        return tokens
