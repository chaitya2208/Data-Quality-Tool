import logging
from datetime import datetime
from typing import List, Optional
from sqlalchemy.orm import Session
from app.models.asset import Asset
from app.models.finding import Finding
from app.models.rule import RuleSeverity
from app.models.scan import Scan, ScanStatus
from app.services.rule_engine import RuleEngine

logger = logging.getLogger(__name__)


class RulesAgent:
    """
    Executes data quality rules against assets from MetadataAgent.
    When a RuleClassification is provided, only runs rules the classifier
    selected and applies any severity overrides it specified.
    Falls back to running all rules if no classification is given.
    """

    def __init__(self, db: Session):
        self.db = db
        self.rule_engine = RuleEngine(db)

    def run(
        self,
        scan: Scan,
        table_asset: Asset,
        column_assets: List[Asset],
        classification=None,  # Optional[RuleClassification]
    ) -> List[Finding]:
        if classification:
            logger.info(
                f"[RulesAgent] Running CLASSIFIED rules on {table_asset.fqn} — "
                f"table type: {classification.table_type}, "
                f"selected {len(classification.selected_codes())} rules, "
                f"skipping {len(classification.skipped_codes())}"
            )
        else:
            logger.info(
                f"[RulesAgent] Running ALL rules on {table_asset.fqn} "
                f"({len(column_assets)} columns)"
            )

        # Apply severity overrides from classification before running
        if classification:
            self._apply_severity_overrides(classification)

        findings_data = self.rule_engine.execute_all_rules(
            table_asset, column_assets, scan.id,
            allowed_rule_codes=set(classification.selected_codes()) if classification else None,
        )

        for finding_data in findings_data:
            self.db.add(Finding(**finding_data))

        rules_run = (
            len(classification.selected_codes()) if classification
            else len(self.rule_engine.get_active_rules("table"))
                + len(self.rule_engine.get_active_rules("column")) * len(column_assets)
        )
        scan.rules_checked = rules_run
        scan.findings_count = len(findings_data)
        scan.status = ScanStatus.COMPLETED
        scan.completed_at = datetime.utcnow()
        self.db.commit()

        # Restore severity overrides after scan so DB rules aren't permanently changed
        if classification:
            self._restore_severities(classification)

        findings = self.db.query(Finding).filter(Finding.scan_id == scan.id).all()
        logger.info(f"[RulesAgent] Done — {len(findings)} findings created")
        return findings

    def _apply_severity_overrides(self, classification) -> None:
        """Temporarily patch Rule.severity for overridden rules."""
        from app.models.rule import Rule
        for rule_code in classification.selected_codes():
            override = classification.severity_override(rule_code)
            if not override:
                continue
            rule = self.db.query(Rule).filter(Rule.code == rule_code).first()
            if rule:
                # Store original on the instance so we can restore
                rule._original_severity = rule.severity
                try:
                    rule.severity = RuleSeverity(override)
                    logger.info(
                        f"[RulesAgent] Severity override: {rule_code} → {override}"
                    )
                except ValueError:
                    logger.warning(f"[RulesAgent] Invalid severity override '{override}' for {rule_code}")
        self.db.flush()

    def _restore_severities(self, classification) -> None:
        """Restore any temporarily overridden severities."""
        from app.models.rule import Rule
        for rule_code in classification.selected_codes():
            if not classification.severity_override(rule_code):
                continue
            rule = self.db.query(Rule).filter(Rule.code == rule_code).first()
            if rule and hasattr(rule, "_original_severity"):
                rule.severity = rule._original_severity
                del rule._original_severity
        self.db.commit()
