from typing import List, Dict, Any, Optional, Set
from app.services import storage
from app.services.dynamic_rules import run_dynamic_checks, DYNAMIC_RULE_HANDLER_KEYS
from app.services.schema_drift import DRIFT_HANDLER_KEYS
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
        type-mismatch, etc.) or schema_drift.py (column_added/removed/
        type_changed/nullability_changed/table_removed) are excluded — they're
        real checks, just executed elsewhere (run_dynamic_checks in
        execute_all_rules; scan_service for drift), so including them here
        would only ever produce a "no handler found" warning for a check
        that DOES run, elsewhere. If allowed_rule_codes is given, only runs
        instances whose HANDLER_KEY (upper-cased) is in that set.
        """
        findings = []
        instances = [
            i for i in self.get_active_instances(asset.asset_type)
            if i.check_kind == "python_handler"
            and (i.handler_key or "").lower() not in DYNAMIC_RULE_HANDLER_KEYS
            and (i.handler_key or "").lower() not in DRIFT_HANDLER_KEYS
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
        instance_id_by_handler_key: Optional[Dict[str, str]] = None,
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

        # Dynamic pattern-based checks — only fire for approved per-table
        # instances. instance_id_by_handler_key wires each emitted finding
        # back to its RULE_INSTANCES row (globals are gone; findings without
        # an approved instance get dropped inside run_dynamic_checks).
        try:
            dynamic = run_dynamic_checks(
                table_asset, column_assets, scan_id,
                allowed_rule_codes=allowed_rule_codes,
                instance_id_by_handler_key=instance_id_by_handler_key,
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

        # Prefer the per-instance rationale (Claude's specific reasoning for
        # THIS target — e.g. "'XX9' corrupts any revenue aggregation") over
        # the definition's generic library description ("Flags rows whose
        # column value falls outside an explicit accepted-values list..."),
        # which reads as boilerplate on every finding. The definition
        # description is a decent fallback when rationale is empty, e.g. for
        # instances created outside the AI proposal path.
        instance_rationale = (getattr(instance, "rationale", "") or "").strip()
        detail = instance_rationale or (definition.description or "").strip()
        template_shape = getattr(definition, "template_shape", "") or ""
        is_aggregate = int(total) == 1 and template_shape in (
            "freshness", "row_count_min", "row_count_max",
            "metric_anomaly", "metric_relative_change", "category_disappeared",
        )

        # Fetch a small sample of failing rows for the finding UI drill-down.
        # Uses the same predicate as the count SQL so numbers + samples agree.
        # Best-effort — a failure here must NOT lose the finding itself.
        sample_rows: list[dict] = []
        try:
            from app.services import rule_sql_templates
            if template_shape:
                sample_sql = rule_sql_templates.failing_rows_sample_sql(
                    template_shape,
                    table_asset.database_name, table_asset.schema_name, table_asset.table_name,
                    instance.target_config or {},
                    instance.threshold_config or {},
                    limit=10,
                )
                if sample_sql:
                    src = source if source is not None else sf_session
                    sample_rows = src.query(sample_sql) or []
        except Exception as e:
            logger.warning(f"sample-rows fetch failed for instance {instance.id}: {e}")

        # Reconcile count vs samples. If the check SQL reports fewer failing
        # rows than we actually fetched as samples, the count SQL is buggy
        # (usually a Claude-authored draft that counts groups instead of rows,
        # or an aggregate that returned 1 as a sentinel while real rows are
        # bigger). Trust the samples — they are the actual failing rows and
        # the user will see them. Log so we can catch these instances.
        if not is_aggregate and len(sample_rows) > int(failed):
            logger.warning(
                f"[rule_engine] count/samples mismatch on instance {instance.id}: "
                f"FAILED_COUNT={failed}, samples={len(sample_rows)} — using samples."
            )
            failed = len(sample_rows)

        if is_aggregate:
            description = "This table-level check failed."
        else:
            description = f"{failed} of {total} rows fail this check."
        if detail:
            description = f"{description} {detail}"

        return {
            "asset_id": asset.id,
            "scan_id": scan_id,
            "instance_id": instance.id,
            "title": f"{definition.name} violated on {asset.fqn.split('.')[-1]}",
            "description": description,
            "severity": instance.severity,
            "status": "open",
            "context": {
                "rule_code": definition.name,
                "fqn": asset.fqn,
                "table_name": table_asset.table_name,
                "schema_name": table_asset.schema_name,
                "database_name": table_asset.database_name,
                "column_name": column_name,
                "ai_generated": True,
            },
            # Evidence contract: fail_count / total_count / sample_rows.
            # See docs in scan_finalizer.py — every rule result standardizes
            # these three keys so downstream (finding lifecycle, UI drill-down,
            # health trend) can rely on them.
            "evidence": {
                "fail_count": int(failed),
                "total_count": int(total),
                "sample_rows": sample_rows,
            },
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
                "status": "open",
                "context": {
                    "database_name": asset.database_name,
                    "schema_name": asset.schema_name,
                    "table_name": asset.table_name,
                    "fqn": asset.fqn,
                    "rule_code": instance.code,
                },
                "evidence": {
                    "fail_count": 1, "total_count": 1, "sample_rows": [],
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
                "status": "open",
                "context": {
                    "database_name": asset.database_name,
                    "schema_name": asset.schema_name,
                    "table_name": asset.table_name,
                    "fqn": asset.fqn,
                    "rule_code": instance.code,
                },
                "evidence": {
                    "fail_count": 1, "total_count": 1, "sample_rows": [],
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
                "status": "open",
                "context": {
                    "database_name": asset.database_name,
                    "schema_name": asset.schema_name,
                    "column_name": asset.column_name,
                    "table_name": asset.table_name,
                    "fqn": asset.fqn,
                    "rule_code": instance.code,
                },
                "evidence": {
                    "fail_count": 1, "total_count": 1, "sample_rows": [],
                    "current_comment": asset.comment,
                }
            }
        return None


def initialize_default_rules() -> None:
    """
    Ensure the system rule library holds the DEFINITIONS RuleIntelligence
    can propose on a per-table basis. No global instances (DATABASE_NAME='*')
    are created here anymore — a metadata audit runs on a table only if
    Claude proposes it (and a human reviews it) for that specific table, just
    like every other check. The library is now a list of concepts, not a set
    of always-on universal rules.

    Trimmed from the original 16-entry seed on 2026-07-15: dropped 8 noisy
    metadata handlers (missing_table_comment, missing_column_comment,
    missing_table_owner, too_many_columns, inconsistent_column_naming,
    generic_column_name, missing_created_at, missing_updated_at,
    fk_column_no_constraint) and the redundant column-type-mismatch variants
    that duplicate boolean/date-as-VARCHAR. Kept the 5 highest-signal
    metadata handlers.

    Self-heals if any of these rows are missing (e.g. after a fresh-start
    TRUNCATE) without needing a full setup_db.py re-run.
    """
    default_rules = [
        # ── Metadata/schema audits (python_handler, dispatched by handler_key
        # at findings time). Small deliberate set: each catches a real defect
        # that shows up in most tables and can't be expressed as a SELECT.
        ("NO_PRIMARY_KEY_HINT", "Table May Be Missing a Primary Key",
         "No column matching common primary-key naming patterns (ID, *_ID, PK_*, *_PK, "
         "*_KEY, *_SEQ) was found. Tables without a primary key risk duplicate rows and "
         "make joins, deduplication, and CDC harder.",
         "schema", "medium", ["table"]),
        ("NULLABLE_ID_COLUMN", "Nullable ID / Primary Key Column",
         "Primary key and identifier columns should never be NULL. A nullable PK column "
         "breaks referential integrity and causes unexpected results in GROUP BY, JOIN, "
         "and deduplication.",
         "schema", "high", ["column"]),
        ("PII_COLUMN_NO_MASKING", "Potential PII Column Without Masking Policy",
         "Columns whose names suggest personally identifiable information (e.g. EMAIL, "
         "SSN, PHONE, PASSWORD, DOB, SALARY) should have a Snowflake Dynamic Data "
         "Masking policy applied and a PII tag attached.",
         "security", "high", ["column"]),
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

    # ── SQL-template shapes ────────────────────────────────────────────────
    # These are the 8 canonical data-level check shapes RuleIntelligence picks
    # by name. Seeding them here means they survive any full DB wipe and are
    # always present for get_definition_by_template_shape lookups — RuleIntelligence
    # creates instances of these, never new definitions for them.
    template_rules = [
        ("not_null",             "Not Null",
         "A column that should never be empty has null values. Catches missing required "
         "fields — primary keys, foreign keys, mandatory attributes.",
         "data_quality", "high"),
        ("uniqueness",           "Column Uniqueness",
         "A column that should hold unique values per row has duplicates. Catches "
         "duplicate keys, emails, or other business alternate keys.",
         "data_quality", "high"),
        ("accepted_values",      "Accepted Values",
         "A column contains values outside the expected closed set. Catches typos, "
         "free-text drift, or undocumented code values in categorical columns.",
         "data_quality", "medium"),
        ("range",                "Numeric Range",
         "A numeric column has values outside the expected minimum/maximum bounds. "
         "Catches data entry errors, unit mismatches, and pipeline truncation.",
         "data_quality", "medium"),
        ("regex_match",          "Format / Regex Match",
         "A string column contains values that do not match the expected format pattern. "
         "Catches malformed emails, phone numbers, codes, or identifiers.",
         "data_quality", "medium"),
        ("freshness",            "Stale Timestamp / Data Freshness",
         "The most recent value in a record-creation or update timestamp column is older "
         "than an acceptable age, indicating the pipeline feeding the table has stopped "
         "delivering new rows.",
         "freshness", "high"),
        ("duplicate_key",        "Duplicate Composite Key",
         "A combination of columns that should be unique across rows has duplicates. "
         "Catches compound-key violations where no single column alone is the key.",
         "data_quality", "high"),
        ("referential_integrity","Referential Integrity",
         "A foreign key column references values that do not exist in the referenced "
         "table. Catches orphaned rows, stale references, and broken joins.",
         "data_quality", "high"),
    ]

    for shape, name, description, category, severity in template_rules:
        storage.ensure_template_definition(shape, name, description, category, severity)

    # ── Anomaly Tier A ──────────────────────────────────────────────────
    # These read the app's METRIC_SNAPSHOTS / METRIC_BASELINES rather than
    # the target table's data. RuleIntelligence / AnomalyProposalAgent
    # creates instances of these once a baseline has >= 14 samples.
    anomaly_template_rules = [
        ("metric_anomaly",
         "Metric Anomaly (MAD)",
         "A tracked metric — row count, null percentage, distinct count, "
         "freshness lag, etc. — deviated from its rolling-30d baseline by "
         "more than the configured number of median absolute deviations.",
         "anomaly", "medium", ["table"]),
        ("metric_relative_change",
         "Metric Relative Change",
         "A tracked metric changed by more than the configured percentage "
         "since the previous scan — complements MAD-based detection for "
         "sudden jumps or drops that a noisy baseline would hide.",
         "anomaly", "medium", ["table"]),
        ("category_disappeared",
         "Category Disappeared",
         "A value that consistently appeared in a low-cardinality column "
         "over the baseline window is missing from the latest scan — often "
         "signals an upstream source that stopped producing rows.",
         "anomaly", "medium", ["column"]),
    ]
    for shape, name, description, category, severity, scopes in anomaly_template_rules:
        storage.ensure_template_definition(
            shape, name, description, category, severity, allowed_scopes=scopes,
        )

    logger.info(
        f"Default rules initialized — {len(default_rules)} metadata-audit + "
        f"{len(template_rules)} sql-template definitions"
    )
