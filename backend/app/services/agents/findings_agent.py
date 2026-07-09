"""
Findings Agent — runs selected existing rules + AI rule violations into Finding rows.

Receives:
  - scan: Scan (from MetadataAgent)
  - table_asset, column_assets: from MetadataAgent
  - classification: from RuleIntelligenceAgent (which rules to run, severity overrides)
  - ai_violations: finding dicts for violated AI rules (from RuleIntelligenceAgent)
  - ai_rules: Rule objects created by RuleIntelligenceAgent

Returns: List[Finding] — all persisted findings for this run.
"""
import logging
from datetime import datetime
from typing import List, Dict, Any, Set

from app.services import storage
from app.services.rule_engine import RuleEngine

logger = logging.getLogger(__name__)


class FindingsAgent:
    """
    Executes selected existing rules (with severity overrides) and
    persists all findings including AI rule violations.
    """

    def __init__(self):
        self.rule_engine = RuleEngine()

    def run(
        self,
        scan: Any,
        table_asset: Any,
        column_assets: List[Any],
        classification: dict,
        ai_violations: List[Dict[str, Any]],
        intelligence_agent,  # RuleIntelligenceAgent instance for severity helpers
        allowed_codes: set = None,  # if provided, overrides classification selection
    ) -> List[Any]:

        # Use explicit allowed_codes if provided (post-review), else use classification
        selected_codes: Set[str] = allowed_codes if allowed_codes is not None else intelligence_agent.get_selected_codes(classification)
        skipped_codes:  Set[str] = intelligence_agent.get_skipped_codes(classification) if allowed_codes is None else set()

        logger.info(
            f"[FindingsAgent] Running {len(selected_codes)} selected rules + "
            f"{len(ai_violations)} AI violations on {table_asset.fqn}"
        )

        # Apply severity overrides temporarily
        intelligence_agent.apply_severity_overrides(classification)

        # Run existing rules (filtered to selected)
        findings_data = self.rule_engine.execute_all_rules(
            table_asset, column_assets, scan.id,
            allowed_rule_codes=selected_codes if selected_codes else None,
        )

        # Restore overrides
        intelligence_agent.restore_severity_overrides(classification)

        # Add AI rule violations
        for vf in ai_violations:
            vf["scan_id"] = scan.id  # fill scan_id now that we have it
            findings_data.append(vf)

        # Persist all findings
        storage.create_findings_bulk(findings_data)

        # Complete the scan
        storage.update_scan(
            scan.id,
            rules_checked=len(selected_codes) + len(ai_violations),
            findings_count=len(findings_data),
            status="completed",
            completed_at=datetime.utcnow(),
        )

        findings = storage.list_findings_by_scan(scan.id)
        logger.info(
            f"[FindingsAgent] Done — {len(findings)} findings "
            f"({len(findings) - len(ai_violations)} from existing rules, "
            f"{len(ai_violations)} from AI rules)"
        )
        return findings
