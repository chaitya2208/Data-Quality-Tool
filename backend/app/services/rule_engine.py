from typing import List, Dict, Any, Optional, Set
from sqlalchemy.orm import Session
from app.models.rule import Rule, RuleSeverity, RuleCategory
from app.models.asset import Asset
from app.models.finding import Finding, FindingStatus
from app.services.dynamic_rules import run_dynamic_checks
import logging

logger = logging.getLogger(__name__)


class RuleEngine:
    """
    Rule engine that executes deterministic rules against assets.
    Phase 0: Simple rule checks (naming, comments, ownership).
    """

    def __init__(self, db: Session):
        self.db = db

    def get_active_rules(self, asset_type: str) -> List[Rule]:
        """Get all active rules that apply to the given asset type"""
        rules = self.db.query(Rule).filter(
            Rule.is_active == True
        ).all()

        # Filter rules that apply to this asset type
        applicable_rules = []
        for rule in rules:
            if asset_type in rule.applies_to:
                applicable_rules.append(rule)

        return applicable_rules

    def execute_rules(
        self,
        asset: Asset,
        scan_id: str,
        allowed_rule_codes: Optional[Set[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Execute static rules against a single asset.
        If allowed_rule_codes is given, only runs rules in that set.
        """
        findings = []
        rules = self.get_active_rules(asset.asset_type)

        if allowed_rule_codes is not None:
            rules = [r for r in rules if r.code in allowed_rule_codes]

        logger.info(f"Executing {len(rules)} static rules against asset {asset.fqn}")

        for rule in rules:
            try:
                result = self._execute_rule(rule, asset, scan_id)
                if result:
                    findings.append(result)
            except Exception as e:
                logger.error(f"Error executing rule {rule.code} on asset {asset.fqn}: {str(e)}")

        return findings

    def execute_all_rules(
        self,
        table_asset: Asset,
        column_assets: List[Asset],
        scan_id: str,
        allowed_rule_codes: Optional[Set[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Run static + dynamic rules. If allowed_rule_codes is provided, only
        rules whose code is in that set will be executed (classifier filter).
        """
        findings: List[Dict[str, Any]] = []

        # Static rules on the table
        findings.extend(self.execute_rules(table_asset, scan_id, allowed_rule_codes))

        # Static rules on each column
        for col_asset in column_assets:
            findings.extend(self.execute_rules(col_asset, scan_id, allowed_rule_codes))

        # Dynamic pattern-based rules
        try:
            dynamic = run_dynamic_checks(
                self.db, table_asset, column_assets, scan_id,
                allowed_rule_codes=allowed_rule_codes,
            )
            findings.extend(dynamic)
            logger.info(
                f"Dynamic rules added {len(dynamic)} findings for {table_asset.fqn}"
            )
        except Exception as e:
            logger.error(f"Dynamic rule check failed for {table_asset.fqn}: {e}")

        return findings

    def _execute_rule(self, rule: Rule, asset: Asset, scan_id: str) -> Optional[Dict[str, Any]]:
        """
        Execute a single rule against an asset.
        Static rules use hardcoded handlers.
        AI-generated rules (rule_config.check_type set) use _check_ai_rule().
        """
        rule_handlers = {
            "MISSING_TABLE_COMMENT": self._check_missing_table_comment,
            "MISSING_TABLE_OWNER":   self._check_missing_table_owner,
            "MISSING_COLUMN_COMMENT": self._check_missing_column_comment,
        }

        handler = rule_handlers.get(rule.code)
        if handler:
            return handler(rule, asset, scan_id)

        # AI-generated rule with stored check logic
        cfg = rule.rule_config or {}
        if cfg.get("ai_generated") and cfg.get("check_type"):
            return self._check_ai_rule(rule, asset, scan_id)

        logger.warning(f"No handler found for rule code: {rule.code}")
        return None

    def _check_ai_rule(self, rule: Rule, asset: Asset, scan_id: str) -> Optional[Dict[str, Any]]:
        """
        Execute an AI-generated rule using the check_type + check_config
        stored in rule_config. Works purely from schema metadata — no live
        Snowflake query needed (compatible with DDL validation).
        """
        cfg          = rule.rule_config or {}
        check_type   = cfg.get("check_type", "")
        check_cfg    = cfg.get("check_config") or {}
        col_name     = check_cfg.get("column", "")
        violated     = False
        evidence     = {}

        # Resolve target column metadata
        col_meta: dict = {}
        target_asset = asset
        if col_name and asset.asset_type == "table":
            # For table-level asset, we can't resolve the column here
            # (columns are separate assets); skip gracefully
            return None
        if asset.asset_type == "column":
            col_meta = asset.raw_metadata or {}
            col_name = col_name or asset.column_name or ""

        try:
            if check_type == "not_null":
                if asset.asset_type != "column":
                    return None
                nullable = col_meta.get("is_nullable", "Y")
                if str(nullable).upper() in ("Y", "YES", "TRUE", "1"):
                    violated = True
                    evidence = {"is_nullable": nullable, "expected": "NOT NULL"}

            elif check_type == "not_empty":
                if asset.asset_type != "column":
                    return None
                nullable = col_meta.get("is_nullable", "Y")
                if str(nullable).upper() in ("Y", "YES", "TRUE", "1"):
                    violated = True
                    evidence = {"is_nullable": nullable, "expected": "NOT NULL and non-empty"}

            elif check_type == "allowed_values":
                if asset.asset_type != "column":
                    return None
                data_type = (col_meta.get("data_type") or "").upper()
                # Can only check at DDL time if the column type is VARCHAR/text
                if "VARCHAR" in data_type or "TEXT" in data_type or "STRING" in data_type:
                    # We can't validate actual values from DDL alone — flag as advisory
                    pass

            elif check_type in ("positive", "non_negative", "min_value", "max_value"):
                if asset.asset_type != "column":
                    return None
                data_type = (col_meta.get("data_type") or "").upper()
                numeric_types = {"NUMBER", "INTEGER", "INT", "BIGINT", "FLOAT",
                                 "DOUBLE", "DECIMAL", "NUMERIC", "FIXED"}
                base_type = data_type.split("(")[0].strip()
                if base_type not in numeric_types:
                    violated = True
                    evidence = {
                        "data_type": data_type,
                        "reason": f"Column expected to be numeric for {check_type} check",
                    }

            elif check_type == "column_exists":
                # Only makes sense at table level; can't check column existence
                # against individual column assets
                if asset.asset_type == "table":
                    required = check_cfg.get("required_columns") or (
                        [check_cfg.get("column")] if check_cfg.get("column") else []
                    )
                    if required:
                        # Will be checked in execute_all_rules via the column assets list
                        pass

            elif check_type == "comparison":
                # Two-column comparison — can only verify both columns are same type at DDL
                pass

        except Exception as e:
            logger.warning(f"[RuleEngine] AI rule check failed for {rule.code}: {e}")
            return None

        if not violated:
            return None

        return {
            "asset_id":    asset.id,
            "scan_id":     scan_id,
            "rule_id":     rule.id,
            "title":       f"{rule.name} violated on {asset.column_name or asset.table_name}",
            "description": rule.description,
            "severity":    rule.severity.value if hasattr(rule.severity, "value") else str(rule.severity),
            "status":      "detected",
            "context": {
                "rule_code":     rule.code,
                "fqn":           asset.fqn,
                "table_name":    asset.table_name,
                "schema_name":   asset.schema_name,
                "database_name": asset.database_name,
                "column_name":   asset.column_name or "",
                "ai_generated":  True,
                "check_type":    check_type,
            },
            "evidence": evidence,
        }

    def _check_missing_table_comment(self, rule: Rule, asset: Asset, scan_id: str) -> Optional[Dict[str, Any]]:
        """Check if table has a comment/description"""
        if asset.asset_type != "table":
            return None

        if not asset.comment or asset.comment.strip() == "":
            return {
                "asset_id": asset.id,
                "scan_id": scan_id,
                "rule_id": rule.id,
                "title": f"Table {asset.table_name} is missing a comment",
                "description": f"The table {asset.fqn} does not have a description/comment. "
                              f"All tables should be documented with meaningful comments.",
                "severity": rule.severity.value,
                "status": FindingStatus.DETECTED,
                "context": {
                    "database_name": asset.database_name,
                    "schema_name": asset.schema_name,
                    "table_name": asset.table_name,
                    "fqn": asset.fqn,
                    "rule_code": rule.code,
                },
                "evidence": {
                    "current_comment": asset.comment,
                }
            }
        return None

    def _check_missing_table_owner(self, rule: Rule, asset: Asset, scan_id: str) -> Optional[Dict[str, Any]]:
        if asset.asset_type != "table":
            return None

        if not asset.owner or asset.owner.strip() == "":
            return {
                "asset_id": asset.id,
                "scan_id": scan_id,
                "rule_id": rule.id,
                "title": f"Table {asset.table_name} is missing an owner",
                "description": f"The table {asset.fqn} does not have an assigned owner. "
                              f"All tables should have a designated owner for accountability.",
                "severity": rule.severity.value,
                "status": FindingStatus.DETECTED,
                "context": {
                    "database_name": asset.database_name,
                    "schema_name": asset.schema_name,
                    "table_name": asset.table_name,
                    "fqn": asset.fqn,
                    "rule_code": rule.code,
                },
                "evidence": {
                    "current_owner": asset.owner,
                }
            }
        return None

    def _check_missing_column_comment(self, rule: Rule, asset: Asset, scan_id: str) -> Optional[Dict[str, Any]]:
        if asset.asset_type != "column":
            return None

        if not asset.comment or asset.comment.strip() == "":
            return {
                "asset_id": asset.id,
                "scan_id": scan_id,
                "rule_id": rule.id,
                "title": f"Column {asset.column_name} is missing a comment",
                "description": f"The column {asset.fqn} does not have a description/comment. "
                              f"All columns should be documented with meaningful comments.",
                "severity": rule.severity.value,
                "status": FindingStatus.DETECTED,
                "context": {
                    "database_name": asset.database_name,
                    "schema_name": asset.schema_name,
                    "column_name": asset.column_name,
                    "table_name": asset.table_name,
                    "fqn": asset.fqn,
                    "rule_code": rule.code,
                },
                "evidence": {
                    "current_comment": asset.comment,
                }
            }
        return None


def initialize_default_rules(db: Session) -> None:
    """
    Initialize the database with default rules.
    This should be run once during initial setup.
    """
    default_rules = [
        {
            "code": "MISSING_TABLE_COMMENT",
            "name": "Missing Table Comment",
            "description": "Tables should have descriptive comments explaining their purpose",
            "category": RuleCategory.DOCUMENTATION,
            "severity": RuleSeverity.MEDIUM,
            "applies_to": ["table"],
            "rule_config": {},
            "is_active": True,
        },
        {
            "code": "MISSING_TABLE_OWNER",
            "name": "Missing Table Owner",
            "description": "Tables should have an assigned owner for accountability",
            "category": RuleCategory.OWNERSHIP,
            "severity": RuleSeverity.HIGH,
            "applies_to": ["table"],
            "rule_config": {},
            "is_active": True,
        },
        {
            "code": "MISSING_COLUMN_COMMENT",
            "name": "Missing Column Comment",
            "description": "Columns should have descriptive comments explaining their data",
            "category": RuleCategory.DOCUMENTATION,
            "severity": RuleSeverity.LOW,
            "applies_to": ["column"],
            "rule_config": {},
            "is_active": True,
        },
    ]

    # All dynamic rules — pre-registered so they appear in the Rules page
    # even before a scan fires them on a matching table/column.
    dynamic_rules = [
        # ── Table-level ──────────────────────────────────────────────────────
        {
            "code": "NO_PRIMARY_KEY_HINT",
            "name": "Table May Be Missing a Primary Key",
            "description": (
                "No column matching common primary-key naming patterns "
                "(ID, *_ID, PK_*, *_PK, *_KEY, *_SEQ) was found. "
                "Tables without a primary key risk duplicate rows and make "
                "joins, deduplication, and CDC harder."
            ),
            "category": RuleCategory.SCHEMA,
            "severity": RuleSeverity.MEDIUM,
            "applies_to": ["table"],
            "rule_config": {},
            "is_active": True,
        },
        {
            "code": "MISSING_CREATED_AT",
            "name": "Missing Row Creation Timestamp",
            "description": (
                "Production tables should track when rows were inserted via a "
                "column such as CREATED_AT, CREATE_DATE, or INSERT_TS. "
                "This enables auditing, incremental loads, and change tracking."
            ),
            "category": RuleCategory.SCHEMA,
            "severity": RuleSeverity.MEDIUM,
            "applies_to": ["table"],
            "rule_config": {},
            "is_active": True,
        },
        {
            "code": "MISSING_UPDATED_AT",
            "name": "Missing Row Updated Timestamp",
            "description": (
                "Mutable tables should track the last modification time via "
                "UPDATED_AT, MODIFIED_DATE, or equivalent. "
                "Required for CDC, incremental ETL, and auditing."
            ),
            "category": RuleCategory.SCHEMA,
            "severity": RuleSeverity.LOW,
            "applies_to": ["table"],
            "rule_config": {},
            "is_active": True,
        },
        {
            "code": "TOO_MANY_COLUMNS",
            "name": "Table Has Too Many Columns",
            "description": (
                "Tables with more than 50 columns often indicate poor normalisation, "
                "merged business entities, or accumulated technical debt. "
                "Consider decomposing into focused, related tables."
            ),
            "category": RuleCategory.SCHEMA,
            "severity": RuleSeverity.LOW,
            "applies_to": ["table"],
            "rule_config": {"threshold": 50},
            "is_active": True,
        },
        {
            "code": "INCONSISTENT_COLUMN_NAMING",
            "name": "Inconsistent Column Naming Style",
            "description": (
                "Column names should follow a single naming convention throughout "
                "a table (e.g. all UPPER_SNAKE_CASE). Mixing styles makes queries "
                "harder to write and datasets harder to join."
            ),
            "category": RuleCategory.NAMING,
            "severity": RuleSeverity.LOW,
            "applies_to": ["table"],
            "rule_config": {},
            "is_active": True,
        },
        # ── Column-level ─────────────────────────────────────────────────────
        {
            "code": "PII_COLUMN_NO_MASKING",
            "name": "Potential PII Column Without Masking Policy",
            "description": (
                "Columns whose names suggest personally identifiable information "
                "(e.g. EMAIL, SSN, PHONE, PASSWORD, DOB, SALARY) should have a "
                "Snowflake Dynamic Data Masking policy applied and a PII tag attached."
            ),
            "category": RuleCategory.SECURITY,
            "severity": RuleSeverity.HIGH,
            "applies_to": ["column"],
            "rule_config": {},
            "is_active": True,
        },
        {
            "code": "GENERIC_COLUMN_NAME",
            "name": "Generic / Uninformative Column Name",
            "description": (
                "Column names like COL1, DATA, VALUE, FIELD, or MISC provide no "
                "semantic context. Rename them to describe what they actually store."
            ),
            "category": RuleCategory.NAMING,
            "severity": RuleSeverity.LOW,
            "applies_to": ["column"],
            "rule_config": {},
            "is_active": True,
        },
        {
            "code": "COLUMN_ID_WRONG_TYPE",
            "name": "Column Type Mismatch — ID/key column should be numeric",
            "description": (
                "Columns ending in _ID, _KEY, _FK, or _PK should use a numeric type "
                "(NUMBER, INTEGER, BIGINT). Storing them as VARCHAR causes implicit "
                "conversions and silent join failures."
            ),
            "category": RuleCategory.SCHEMA,
            "severity": RuleSeverity.MEDIUM,
            "applies_to": ["column"],
            "rule_config": {},
            "is_active": True,
        },
        {
            "code": "COLUMN_DATE_WRONG_TYPE",
            "name": "Column Type Mismatch — Date column should be DATE or TIMESTAMP",
            "description": (
                "Columns ending in _DATE, _DT, or _DAY should use DATE or TIMESTAMP. "
                "Storing them as other types prevents date arithmetic and proper sorting."
            ),
            "category": RuleCategory.SCHEMA,
            "severity": RuleSeverity.MEDIUM,
            "applies_to": ["column"],
            "rule_config": {},
            "is_active": True,
        },
        {
            "code": "FK_COLUMN_NO_CONSTRAINT",
            "name": "Foreign Key Column Without FK Constraint",
            "description": (
                "Columns ending in '_ID' typically reference another table. "
                "Add an unenforced REFERENCES clause for documentation and "
                "data lineage tools."
            ),
            "category": RuleCategory.SCHEMA,
            "severity": RuleSeverity.LOW,
            "applies_to": ["column"],
            "rule_config": {},
            "is_active": True,
        },
        {
            "code": "NULLABLE_ID_COLUMN",
            "name": "Nullable ID / Primary Key Column",
            "description": (
                "Primary key and identifier columns should never be NULL. "
                "A nullable PK column breaks referential integrity and causes "
                "unexpected results in GROUP BY, JOIN, and deduplication."
            ),
            "category": RuleCategory.SCHEMA,
            "severity": RuleSeverity.HIGH,
            "applies_to": ["column"],
            "rule_config": {},
            "is_active": True,
        },
        {
            "code": "BOOLEAN_STORED_AS_VARCHAR",
            "name": "Boolean/Flag Column Stored as VARCHAR",
            "description": (
                "Columns whose names suggest a boolean or flag value (_FL, _FLAG, _IND, "
                "IS_, _YN) are stored as VARCHAR. This allows invalid values and prevents "
                "efficient filtering. Use BOOLEAN or a small integer type."
            ),
            "category": RuleCategory.DATA_QUALITY,
            "severity": RuleSeverity.MEDIUM,
            "applies_to": ["column"],
            "rule_config": {},
            "is_active": True,
        },
        {
            "code": "DATE_STORED_AS_VARCHAR",
            "name": "Date/Timestamp Column Stored as VARCHAR",
            "description": (
                "Columns whose names suggest a date or timestamp are stored as VARCHAR. "
                "This prevents date arithmetic, sorting, filtering, and indexing. "
                "Convert to DATE or TIMESTAMP."
            ),
            "category": RuleCategory.DATA_QUALITY,
            "severity": RuleSeverity.HIGH,
            "applies_to": ["column"],
            "rule_config": {},
            "is_active": True,
        },
    ]

    all_rules = default_rules + dynamic_rules

    for rule_data in all_rules:
        existing_rule = db.query(Rule).filter(Rule.code == rule_data["code"]).first()
        if not existing_rule:
            # System-seeded rules are pre-approved and active
            from app.models.rule import RuleStatus
            rule_data.setdefault("owner",      "data-governance-team")
            rule_data.setdefault("created_by", "system")
            rule_data.setdefault("status",     RuleStatus.ACTIVE)
            rule_data.setdefault("version",    1)
            rule = Rule(**rule_data)
            db.add(rule)
            logger.info(f"Created default rule: {rule_data['code']}")
        else:
            # Backfill missing fields on existing rows
            from app.models.rule import RuleStatus
            if not existing_rule.owner:
                existing_rule.owner = "data-governance-team"
            if not existing_rule.created_by:
                existing_rule.created_by = "system"
            if not existing_rule.status:
                existing_rule.status = RuleStatus.ACTIVE
            if not existing_rule.version:
                existing_rule.version = 1

    db.commit()
    logger.info("Default rules initialized")
