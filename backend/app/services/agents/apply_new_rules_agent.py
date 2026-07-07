import logging
from datetime import datetime
from typing import List
from sqlalchemy.orm import Session

from app.models.agent_run import AgentRun, AgentTask
from app.models.asset import Asset
from app.models.finding import Finding
from app.models.rule import Rule, RuleStatus
from app.services.rule_engine import RuleEngine

logger = logging.getLogger(__name__)


class ApplyNewRulesAgent:
    """
    Runs newly approved rules (proposed by RuleSuggestionAgent for this run)
    against the already-stored assets. No new Snowflake call needed —
    assets + column metadata are already in the DB from the Metadata step.
    Creates additional Finding rows if any approved rules are violated.
    """

    def __init__(self, db: Session):
        self.db = db
        self.rule_engine = RuleEngine(db)

    def run(self, run: AgentRun, task: AgentTask) -> int:
        task.output = {"progress": "Looking for newly approved rules..."}
        self.db.commit()

        # Find rules that were suggested by this run and are now ACTIVE (approved since pause)
        approved_rules = (
            self.db.query(Rule)
            .filter(
                Rule.created_by == "rule_suggestion_agent",
                Rule.status == RuleStatus.ACTIVE,
                Rule.is_active == True,
            )
            .all()
        )

        # Filter to rules from this specific run
        run_rules = [
            r for r in approved_rules
            if (r.rule_config or {}).get("source_run_id") == run.id
        ]

        logger.info(
            f"[ApplyNewRulesAgent] {len(run_rules)} newly approved rules for run {run.id}"
        )

        if not run_rules:
            task.output = {
                "progress": "No newly approved rules to apply",
                "new_findings": 0,
            }
            self.db.commit()
            return 0

        # Fetch existing assets for this run's table
        table_asset = (
            self.db.query(Asset)
            .filter(
                Asset.database_name == run.database,
                Asset.schema_name == run.schema_name,
                Asset.table_name == run.table,
                Asset.asset_type == "table",
            )
            .first()
        )
        if not table_asset:
            raise ValueError(f"Table asset not found for {run.database}.{run.schema_name}.{run.table}")

        column_assets = (
            self.db.query(Asset)
            .filter(
                Asset.database_name == run.database,
                Asset.schema_name == run.schema_name,
                Asset.table_name == run.table,
                Asset.asset_type == "column",
            )
            .all()
        )

        task.output = {
            "progress": f"Running {len(run_rules)} approved rules against {table_asset.fqn}...",
        }
        self.db.commit()

        # Run only the newly approved rules
        new_findings = 0
        for rule in run_rules:
            try:
                findings_data = []
                if "table" in (rule.applies_to or []):
                    result = self.rule_engine._execute_rule(rule, table_asset, run.scan_id)
                    if result:
                        findings_data.append(result)
                if "column" in (rule.applies_to or []):
                    for col in column_assets:
                        result = self.rule_engine._execute_rule(rule, col, run.scan_id)
                        if result:
                            findings_data.append(result)

                for fd in findings_data:
                    self.db.add(Finding(**fd))
                    new_findings += 1
            except Exception as e:
                logger.warning(f"[ApplyNewRulesAgent] Rule {rule.code} failed: {e}")

        self.db.commit()

        # Update run findings count
        run.findings_count = (
            self.db.query(Finding)
            .filter(Finding.scan_id == run.scan_id)
            .count()
        )
        self.db.commit()

        logger.info(f"[ApplyNewRulesAgent] Added {new_findings} new findings")
        task.output = {
            "progress": f"Done — {new_findings} new findings from approved rules",
            "approved_rules_applied": len(run_rules),
            "new_findings": new_findings,
        }
        self.db.commit()
        return new_findings
