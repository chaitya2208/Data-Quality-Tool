from typing import List, Dict, Any, Optional, Set
from app.services import storage
from app.services.dynamic_rules import run_dynamic_checks
import logging

logger = logging.getLogger(__name__)


class RuleEngine:
    """
    Rule engine that executes deterministic rules against assets.
    Phase 0: Simple rule checks (naming, comments, ownership).
    """

    def __init__(self):
        pass

    def get_active_rules(self, asset_type: str) -> List[Any]:
        """Get all active rules that apply to the given asset type"""
        rules = storage.list_active_rules_for_type(asset_type)
        return [r for r in rules if asset_type in (r.applies_to or [])]

    def execute_rules(
        self,
        asset: Any,
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
        table_asset: Any,
        column_assets: List[Any],
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
                table_asset, column_assets, scan_id,
                allowed_rule_codes=allowed_rule_codes,
            )
            findings.extend(dynamic)
            logger.info(
                f"Dynamic rules added {len(dynamic)} findings for {table_asset.fqn}"
            )
        except Exception as e:
            logger.error(f"Dynamic rule check failed for {table_asset.fqn}: {e}")

        return findings

    def _execute_rule(self, rule: Any, asset: Any, scan_id: str) -> Optional[Dict[str, Any]]:
        """
        Execute a single rule against an asset.
        Returns a finding dict if rule is violated, None otherwise.
        """
        rule_handlers = {
            "MISSING_TABLE_COMMENT": self._check_missing_table_comment,
            "MISSING_TABLE_OWNER": self._check_missing_table_owner,
            "MISSING_COLUMN_COMMENT": self._check_missing_column_comment,
        }

        handler = rule_handlers.get(rule.code)
        if not handler:
            logger.warning(f"No handler found for rule code: {rule.code}")
            return None

        return handler(rule, asset, scan_id)

    def _check_missing_table_comment(self, rule: Any, asset: Any, scan_id: str) -> Optional[Dict[str, Any]]:
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
                "severity": rule.severity,
                "status": "detected",
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

    def _check_missing_table_owner(self, rule: Any, asset: Any, scan_id: str) -> Optional[Dict[str, Any]]:
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
                "severity": rule.severity,
                "status": "detected",
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

    def _check_missing_column_comment(self, rule: Any, asset: Any, scan_id: str) -> Optional[Dict[str, Any]]:
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
                "severity": rule.severity,
                "status": "detected",
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


def initialize_default_rules() -> None:
    """
    Ensure the system rule library exists. The actual seed data lives in
    snowflake/03_seed_default_rules.sql (run once via setup_db.py); this
    just self-heals if any of those rows are missing (e.g. a fresh-start
    TRUNCATE) without needing a full setup_db.py re-run.
    """
    default_rules = [
        ("MISSING_TABLE_COMMENT", "Missing Table Comment",
         "Tables should have descriptive comments explaining their purpose",
         "documentation", "medium", ["table"]),
        ("MISSING_TABLE_OWNER", "Missing Table Owner",
         "Tables should have an assigned owner for accountability",
         "ownership", "high", ["table"]),
        ("MISSING_COLUMN_COMMENT", "Missing Column Comment",
         "Columns should have descriptive comments explaining their data",
         "documentation", "low", ["column"]),
        ("NO_PRIMARY_KEY_HINT", "Table May Be Missing a Primary Key",
         "No column matching common primary-key naming patterns (ID, *_ID, PK_*, *_PK, "
         "*_KEY, *_SEQ) was found. Tables without a primary key risk duplicate rows and "
         "make joins, deduplication, and CDC harder.",
         "schema", "medium", ["table"]),
        ("MISSING_CREATED_AT", "Missing Row Creation Timestamp",
         "Production tables should track when rows were inserted via a column such as "
         "CREATED_AT, CREATE_DATE, or INSERT_TS. This enables auditing, incremental "
         "loads, and change tracking.",
         "schema", "medium", ["table"]),
        ("MISSING_UPDATED_AT", "Missing Row Updated Timestamp",
         "Mutable tables should track the last modification time via UPDATED_AT, "
         "MODIFIED_DATE, or equivalent. Required for CDC, incremental ETL, and auditing.",
         "schema", "low", ["table"]),
        ("TOO_MANY_COLUMNS", "Table Has Too Many Columns",
         "Tables with more than 50 columns often indicate poor normalisation, merged "
         "business entities, or accumulated technical debt. Consider decomposing into "
         "focused, related tables.",
         "schema", "low", ["table"]),
        ("INCONSISTENT_COLUMN_NAMING", "Inconsistent Column Naming Style",
         "Column names should follow a single naming convention throughout a table "
         "(e.g. all UPPER_SNAKE_CASE). Mixing styles makes queries harder to write and "
         "datasets harder to join.",
         "naming", "low", ["table"]),
        ("PII_COLUMN_NO_MASKING", "Potential PII Column Without Masking Policy",
         "Columns whose names suggest personally identifiable information (e.g. EMAIL, "
         "SSN, PHONE, PASSWORD, DOB, SALARY) should have a Snowflake Dynamic Data "
         "Masking policy applied and a PII tag attached.",
         "security", "high", ["column"]),
        ("GENERIC_COLUMN_NAME", "Generic / Uninformative Column Name",
         "Column names like COL1, DATA, VALUE, FIELD, or MISC provide no semantic "
         "context. Rename them to describe what they actually store.",
         "naming", "low", ["column"]),
        ("COLUMN_ID_WRONG_TYPE", "Column Type Mismatch — ID/key column should be numeric",
         "Columns ending in _ID, _KEY, _FK, or _PK should use a numeric type (NUMBER, "
         "INTEGER, BIGINT). Storing them as VARCHAR causes implicit conversions and "
         "silent join failures.",
         "schema", "medium", ["column"]),
        ("COLUMN_DATE_WRONG_TYPE", "Column Type Mismatch — Date column should be DATE or TIMESTAMP",
         "Columns ending in _DATE, _DT, or _DAY should use DATE or TIMESTAMP. Storing "
         "them as other types prevents date arithmetic and proper sorting.",
         "schema", "medium", ["column"]),
        ("FK_COLUMN_NO_CONSTRAINT", "Foreign Key Column Without FK Constraint",
         "Columns ending in '_ID' typically reference another table. Add an unenforced "
         "REFERENCES clause for documentation and data lineage tools.",
         "schema", "low", ["column"]),
        ("NULLABLE_ID_COLUMN", "Nullable ID / Primary Key Column",
         "Primary key and identifier columns should never be NULL. A nullable PK column "
         "breaks referential integrity and causes unexpected results in GROUP BY, JOIN, "
         "and deduplication.",
         "schema", "high", ["column"]),
        ("BOOLEAN_STORED_AS_VARCHAR", "Boolean/Flag Column Stored as VARCHAR",
         "Columns whose names suggest a boolean or flag value (_FL, _FLAG, _IND, IS_, "
         "_YN) are stored as VARCHAR. This allows invalid values and prevents efficient "
         "filtering. Use BOOLEAN or a small integer type.",
         "data_quality", "medium", ["column"]),
        ("DATE_STORED_AS_VARCHAR", "Date/Timestamp Column Stored as VARCHAR",
         "Columns whose names suggest a date or timestamp are stored as VARCHAR. This "
         "prevents date arithmetic, sorting, filtering, and indexing. Convert to DATE or "
         "TIMESTAMP.",
         "data_quality", "high", ["column"]),
    ]

    for code, name, description, category, severity, applies_to in default_rules:
        storage.ensure_rule(code, name, description, category, severity, applies_to)

    logger.info("Default rules initialized")
