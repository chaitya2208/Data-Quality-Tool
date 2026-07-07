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
from sqlalchemy.orm import Session

from app.models.asset import Asset
from app.models.finding import Finding, FindingStatus
from app.models.rule import RuleSeverity
from app.models.scan import Scan, ScanStatus
from app.services.rule_engine import RuleEngine

logger = logging.getLogger(__name__)


class FindingsAgent:
    """
    Executes selected existing rules (with severity overrides) and
    persists all findings including AI rule violations.
    """

    def __init__(self, db: Session):
        self.db = db
        self.rule_engine = RuleEngine(db)

    def run(
        self,
        scan: Scan,
        table_asset: Asset,
        column_assets: List[Asset],
        classification: dict,
        ai_violations: List[Dict[str, Any]],
        intelligence_agent,  # RuleIntelligenceAgent instance for severity helpers
        allowed_codes: set = None,  # if provided, overrides classification selection
    ) -> List[Finding]:

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
        for fd in findings_data:
            self.db.add(Finding(**fd))

        # Complete the scan
        scan.rules_checked = len(selected_codes) + len(ai_violations)
        scan.findings_count = len(findings_data)
        scan.status = ScanStatus.COMPLETED
        scan.completed_at = datetime.utcnow()
        self.db.commit()

        findings = self.db.query(Finding).filter(Finding.scan_id == scan.id).all()
        logger.info(
            f"[FindingsAgent] Done — {len(findings)} findings "
            f"({len(findings) - len(ai_violations)} from existing rules, "
            f"{len(ai_violations)} from AI rules)"
        )
        return findings
