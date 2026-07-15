from typing import List, Dict, Any, Optional, Set
from app.services import storage
from app.services.dynamic_rules import run_dynamic_checks, DYNAMIC_RULE_HANDLER_KEYS
from app.services.snowflake_session import session as sf_session
import logging

logger = logging.getLogger(__name__)


class RuleEngine:
    """
    Rule engine that executes deterministic rules against assets.
    Dispatches on each active instance's definition.check_kind:
      - python_handler: calls a Python function keyed by handler_key against
        already-fetched metadata (no live query).
      - sql_template: executes instance.rule_sql (already validated at
        proposal time by RuleIntelligenceAgent) against Snowflake, expecting
        exactly one row shaped (FAILED_COUNT, TOTAL_COUNT).
    """

    _HANDLERS = {
        "missing_table_comment": "_check_missing_table_comment",
        "missing_table_owner": "_check_missing_table_owner",
        "missing_column_comment": "_check_missing_column_comment",
    }

    def __init__(self):
        pass

    def get_active_instances(self, scope: str) -> List[Any]:
        """Active instances with a given scope ('table' | 'column'), each
        annotated with `.handler_key`/`.check_kind` from its definition and a
        `.code` (HANDLER_KEY upper-cased) for legacy-shaped call sites.

        Definitions are batch-fetched in ONE query (get_definitions_by_ids)
        instead of one get_definition() call per instance in the loop — the
        old N+1 pattern meant a 24-instance scan issued 24+ sequential
        Snowflake round-trips (each ~300-500ms) just to resolve definitions,
        which is what made findings runs feel stuck rather than merely slow."""
        instances = storage.list_active_instances_for_scope(scope)
        definitions_by_id = storage.get_definitions_by_ids(
            [inst.definition_id for inst in instances]
        )
        result = []
        for inst in instances:
            definition = definitions_by_id.get(inst.definition_id)
            if not definition:
                continue
            # Definition-level disable gates every instance under it — the
            # "turn off this whole check concept" toggle in Rule Library
            # (app/api/rules.py toggle_rule_definition) has no effect unless
            # this filter exists; instance.is_active alone can't express it.
            if definition.status == "disabled":
                continue
            inst.check_kind = definition.check_kind
            inst.handler_key = definition.handler_key
            inst.code = (definition.handler_key or "").upper()
            inst.name = definition.name
            inst.description = definition.description
            inst.category = definition.category
            result.append(inst)
        return result

    # Backward-shaped alias used by callers still passing a "code" set
    def get_active_rules(self, asset_type: str) -> List[Any]:
        return self.get_active_instances(asset_type)

    def execute_rules(
        self,
        asset: Any,
        scan_id: str,
        allowed_rule_codes: Optional[Set[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Execute python_handler instances against a single asset via the
        _HANDLERS dispatch table (missing_table_comment/owner, missing_
        column_comment — the only 3 keys with a method here). Instances
        whose handler_key belongs to dynamic_rules.py (PII, nullable-ID,
        type-mismatch, etc.) are excluded — they're real checks, just
        executed via run_dynamic_checks() in execute_all_rules() instead of
        this dispatch table, so including them here would only ever produce
        a "no handler found" warning for a check that DOES run, elsewhere.
        If allowed_rule_codes is given, only runs instances whose HANDLER_KEY
        (upper-cased) is in that set.
        """
        findings = []
        instances = [
            i for i in self.get_active_instances(asset.asset_type)
            if i.check_kind == "python_handler"
            and (i.handler_key or "").lower() not in DYNAMIC_RULE_HANDLER_KEYS
        ]

        if allowed_rule_codes is not None:
            instances = [i for i in instances if i.code in allowed_rule_codes]

        logger.info(f"Executing {len(instances)} static instances against asset {asset.fqn}")

        for instance in instances:
            try:
                result = self._execute_instance(instance, asset, scan_id)
                if result:
                    findings.append(result)
            except Exception as e:
                logger.error(f"Error executing instance {instance.code} on asset {asset.fqn}: {str(e)}")

        return findings

    def execute_all_rules(
        self,
        table_asset: Any,
        column_assets: List[Any],
        scan_id: str,
        allowed_rule_codes: Optional[Set[str]] = None,
        allowed_instance_ids: Optional[Set[str]] = None,
        source: Any = None,
    ) -> List[Dict[str, Any]]:
        """
        Run static (python_handler) + dynamic + sql_template instances.
        allowed_rule_codes filters python_handler instances by HANDLER_KEY
        (upper-cased); allowed_instance_ids filters sql_template instances by
        instance id directly, since Claude-authored checks have no stable
        "code" the way static/dynamic ones do.
        """
        findings: List[Dict[str, Any]] = []

        # Static instances on the table
        findings.extend(self.execute_rules(table_asset, scan_id, allowed_rule_codes))

        # Static instances on each column
        for col_asset in column_assets:
            findings.extend(self.execute_rules(col_asset, scan_id, allowed_rule_codes))

        # Dynamic pattern-based checks
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

        # SQL-template instances (Claude-authored, real executable SQL) —
        # scoped to this table regardless of column/table target shape,
        # since the SQL itself already encodes the target.
        if allowed_instance_ids:
            findings.extend(
                self.execute_sql_instances(table_asset, scan_id, allowed_instance_ids, source=source)
            )

        return findings

    def execute_sql_instances(
        self, table_asset: Any, scan_id: str, allowed_instance_ids: Set[str],
        source: Any = None,
    ) -> List[Dict[str, Any]]:
        """Run every active sql_template instance in allowed_instance_ids that
        belongs to this table. sql_template instances are ALWAYS bound to a
        concrete table: their rule_sql has the fully-qualified db.schema.table
        baked in at proposal time (see rule_sql_templates._fqn). A global
        ('*'-scoped) sql_template instance therefore can't exist coherently —
        its SQL still names one specific table — so we match on the concrete
        database_name only. (Global '*' instances are a python_handler concept,
        created via the CRUD API, and are handled by execute_rules, not here.)

        Instances and definitions are batch-fetched (2 queries total) instead
        of one get_instance()+get_definition() round-trip per instance in the
        loop — the old N+1 pattern was the main source of "findings run feels
        stuck" reports on tables with many approved instances."""
        findings: List[Dict[str, Any]] = []
        instances_by_id = storage.get_instances_by_ids(list(allowed_instance_ids))
        definitions_by_id = storage.get_definitions_by_ids(
            [inst.definition_id for inst in instances_by_id.values()]
        )
        for instance_id in allowed_instance_ids:
            instance = instances_by_id.get(instance_id)
            if not instance:
                continue
            definition = definitions_by_id.get(instance.definition_id)
            if not definition or definition.check_kind != "sql_template":
                continue
            if definition.status == "disabled":
                continue
            if instance.database_name != table_asset.database_name:
                continue
            try:
                result = self._execute_sql_instance(instance, definition, table_asset, scan_id, source=source)
                if result:
                    findings.append(result)
            except Exception as e:
                logger.error(f"Error executing sql_template instance {instance.id}: {e}")
        return findings

    def _execute_sql_instance(
        self, instance: Any, definition: Any, table_asset: Any, scan_id: str,
        source: Any = None,
    ) -> Optional[Dict[str, Any]]:
        if not instance.rule_sql:
            logger.warning(f"sql_template instance {instance.id} has no rule_sql")
            return None
        # Run against the finding's OWN source (Postgres/RDS or Snowflake) when
        # one is provided; fall back to the shared Snowflake session otherwise
        # (legacy/global calls). Snowflake keys come back UPPERCASE, Postgres
        # lowercase — read both.
        rows = source.query(instance.rule_sql) if source is not None else sf_session.query(instance.rule_sql)
        if not rows:
            return None
        row = rows[0]
        failed = row.get("FAILED_COUNT", row.get("failed_count"))
        total = row.get("TOTAL_COUNT", row.get("total_count"))
        if failed is None or total is None:
            logger.warning(f"sql_template instance {instance.id} SQL did not return FAILED_COUNT/TOTAL_COUNT")
            return None
        if int(failed) <= 0:
            return None

        column_name = (instance.target_config or {}).get("column", "")
        asset = table_asset
        if column_name:
            col_fqn = f"{table_asset.fqn}.{column_name}"
            asset = storage.get_asset_by_fqn(col_fqn) or table_asset

        return {
            "asset_id": asset.id,
            "scan_id": scan_id,
            "instance_id": instance.id,
            "title": f"{definition.name} violated on {asset.fqn.split('.')[-1]}",
            "description": f"{failed} of {total} rows fail this check. {definition.description}",
            "severity": instance.severity,
            "status": "detected",
            "context": {
                "rule_code": definition.name,
                "fqn": asset.fqn,
                "table_name": table_asset.table_name,
                "schema_name": table_asset.schema_name,
                "database_name": table_asset.database_name,
                "column_name": column_name,
                "ai_generated": True,
            },
            "evidence": {"failed_count": int(failed), "total_count": int(total)},
        }

    def _execute_instance(self, instance: Any, asset: Any, scan_id: str) -> Optional[Dict[str, Any]]:
        """
        Execute a single python_handler instance against an asset.
        Returns a finding dict if violated, None otherwise.
        """
        if instance.check_kind != "python_handler":
            logger.warning(f"Instance {instance.id} has unsupported check_kind: {instance.check_kind}")
            return None

        method_name = self._HANDLERS.get(instance.handler_key)
        if not method_name:
            logger.warning(f"No handler found for handler_key: {instance.handler_key}")
            return None

        handler = getattr(self, method_name)
        return handler(instance, asset, scan_id)

    def _check_missing_table_comment(self, instance: Any, asset: Any, scan_id: str) -> Optional[Dict[str, Any]]:
        """Check if table has a comment/description"""
        if asset.asset_type != "table":
            return None

        if not asset.comment or asset.comment.strip() == "":
            return {
                "asset_id": asset.id,
                "scan_id": scan_id,
                "instance_id": instance.id,
                "title": f"Table {asset.table_name} is missing a comment",
                "description": f"The table {asset.fqn} does not have a description/comment. "
                              f"All tables should be documented with meaningful comments.",
                "severity": instance.severity,
                "status": "detected",
                "context": {
                    "database_name": asset.database_name,
                    "schema_name": asset.schema_name,
                    "table_name": asset.table_name,
                    "fqn": asset.fqn,
                    "rule_code": instance.code,
                },
                "evidence": {
                    "current_comment": asset.comment,
                }
            }
        return None

    def _check_missing_table_owner(self, instance: Any, asset: Any, scan_id: str) -> Optional[Dict[str, Any]]:
        if asset.asset_type != "table":
            return None

        if not asset.owner or asset.owner.strip() == "":
            return {
                "asset_id": asset.id,
                "scan_id": scan_id,
                "instance_id": instance.id,
                "title": f"Table {asset.table_name} is missing an owner",
                "description": f"The table {asset.fqn} does not have an assigned owner. "
                              f"All tables should have a designated owner for accountability.",
                "severity": instance.severity,
                "status": "detected",
                "context": {
                    "database_name": asset.database_name,
                    "schema_name": asset.schema_name,
                    "table_name": asset.table_name,
                    "fqn": asset.fqn,
                    "rule_code": instance.code,
                },
                "evidence": {
                    "current_owner": asset.owner,
                }
            }
        return None

    def _check_missing_column_comment(self, instance: Any, asset: Any, scan_id: str) -> Optional[Dict[str, Any]]:
        if asset.asset_type != "column":
            return None

        if not asset.comment or asset.comment.strip() == "":
            return {
                "asset_id": asset.id,
                "scan_id": scan_id,
                "instance_id": instance.id,
                "title": f"Column {asset.column_name} is missing a comment",
                "description": f"The column {asset.fqn} does not have a description/comment. "
                              f"All columns should be documented with meaningful comments.",
                "severity": instance.severity,
                "status": "detected",
                "context": {
                    "database_name": asset.database_name,
                    "schema_name": asset.schema_name,
                    "column_name": asset.column_name,
                    "table_name": asset.table_name,
                    "fqn": asset.fqn,
                    "rule_code": instance.code,
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
        storage.ensure_definition(code.lower(), name, description, category, severity, applies_to)

    logger.info("Default rules initialized")
