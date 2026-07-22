"""
Relationship discovery — cache-invalidation and FK detection.

Regression coverage for the 2026-07-21 ORPHAN_FK integration failure:

When a schema-wide relationship discovery ran BEFORE a new FK-having table
was seeded, the 24-hour cache returned a partial catalog that omitted the
new table's FK entries. RuleIntelligence then saw an empty
"KNOWN CROSS-TABLE RELATIONSHIPS" list for that table and refused to
propose a referential_integrity check, letting orphan-FK violations slip
through.

The fix: `get_or_refresh_catalog(..., require_table=X)` treats the cache as
stale when it has zero rows for X AND X has FK-shaped columns.
"""
import datetime
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest


def _rel(from_table, from_column="X_ID", to_table="OTHER", to_column="X_ID",
         status="confirmed", confidence="verified", orphan_rate=0.1,
         last_verified_at=None):
    return SimpleNamespace(
        from_table=from_table, from_column=from_column,
        to_table=to_table, to_column=to_column,
        status=status, confidence=confidence,
        orphan_rate=orphan_rate,
        sample_total=100, sample_orphans=10,
        last_verified_at=last_verified_at or datetime.datetime.now(datetime.timezone.utc),
    )


class TestCacheCoversTable:

    def test_cache_with_matching_from_table_is_fresh(self):
        """When the catalog has a row for `require_table`, cache is considered
        to cover it — no refresh needed."""
        from app.services.relationship_discovery import _cache_covers_table
        cached = [_rel(from_table="DQTEST_ORPHAN_FK")]
        # No SF query needed — early-return on matching from_table
        assert _cache_covers_table(cached, "DB", "SCH", "DQTEST_ORPHAN_FK") is True

    def test_cache_without_matching_but_no_fk_columns_is_fresh(self):
        """A table with zero FK-shaped columns legitimately has no catalog
        rows — cache is still considered to cover it."""
        from app.services.relationship_discovery import _cache_covers_table
        cached = [_rel(from_table="OTHER_TABLE")]

        with patch("app.services.relationship_discovery.sf_session") as mock_sf:
            # PARENT_TABLE.PARENT_ID actually matches _FK_SHAPE_RE (_ID suffix)
            # BUT it's in _own_pk_names for tables where the name matches;
            # for a table named PARENT_TABLE, own_pk = {..., 'PARENT_TABLE_ID'}
            # so PARENT_ID does NOT match own_pk → is FK-shaped → cache stale
            # Use a truly FK-columnless table for this test.
            mock_sf.query.return_value = [
                {"COLUMN_NAME": "NAME"},
                {"COLUMN_NAME": "CREATED_AT"},
            ]
            result = _cache_covers_table(cached, "DB", "SCH", "STANDALONE_TABLE")
        assert result is True

    def test_cache_missing_table_with_fk_columns_is_stale(self):
        """The regression case: cached catalog has no entry for
        DQTEST_ORPHAN_FK, but the table has an FK-shaped PARENT_ID column →
        cache treated as stale."""
        from app.services.relationship_discovery import _cache_covers_table
        cached = [_rel(from_table="OTHER_TABLE")]

        with patch("app.services.relationship_discovery.sf_session") as mock_sf:
            mock_sf.query.return_value = [
                {"COLUMN_NAME": "CHILD_ID"},
                {"COLUMN_NAME": "PARENT_ID"},
                {"COLUMN_NAME": "DETAIL"},
            ]
            result = _cache_covers_table(cached, "DB", "SCH", "DQTEST_ORPHAN_FK")
        assert result is False

    def test_information_schema_failure_treated_as_stale(self):
        """If the columns lookup errors, don't trust the cache — err on the
        side of refreshing."""
        from app.services.relationship_discovery import _cache_covers_table
        cached = [_rel(from_table="OTHER")]

        with patch("app.services.relationship_discovery.sf_session") as mock_sf:
            mock_sf.query.side_effect = ConnectionError("boom")
            result = _cache_covers_table(cached, "DB", "SCH", "NEW_TABLE")
        assert result is False


class TestGetOrRefreshCatalog:

    def test_fresh_cache_returned_without_require_table(self):
        """Without require_table, existing behavior — fresh cache is used
        as-is."""
        from app.services.relationship_discovery import get_or_refresh_catalog
        cached = [_rel(from_table="ANY")]

        with patch("app.services.relationship_discovery.storage") as mock_storage, \
             patch("app.services.relationship_discovery._fresh_enough", return_value=True), \
             patch("app.services.relationship_discovery._discover") as mock_discover:
            mock_storage.list_relationships.return_value = cached
            result = get_or_refresh_catalog("DB", "SCH")

        assert result == cached
        mock_discover.assert_not_called()

    def test_fresh_cache_with_covered_table_returned(self):
        """require_table matches a from_table in cache → use cache."""
        from app.services.relationship_discovery import get_or_refresh_catalog
        cached = [_rel(from_table="DQTEST_ORPHAN_FK")]

        with patch("app.services.relationship_discovery.storage") as mock_storage, \
             patch("app.services.relationship_discovery._fresh_enough", return_value=True), \
             patch("app.services.relationship_discovery._discover") as mock_discover:
            mock_storage.list_relationships.return_value = cached
            result = get_or_refresh_catalog("DB", "SCH", require_table="DQTEST_ORPHAN_FK")

        assert result == cached
        mock_discover.assert_not_called()

    def test_fresh_cache_missing_table_triggers_refresh(self):
        """Regression case: cache is fresh but doesn't cover the required
        table → _discover is called to refresh."""
        from app.services.relationship_discovery import get_or_refresh_catalog
        stale_cached = [_rel(from_table="OTHER")]
        rediscovered = [_rel(from_table="DQTEST_ORPHAN_FK"),
                         _rel(from_table="OTHER")]

        with patch("app.services.relationship_discovery.storage") as mock_storage, \
             patch("app.services.relationship_discovery._fresh_enough", return_value=True), \
             patch("app.services.relationship_discovery._cache_covers_table", return_value=False), \
             patch("app.services.relationship_discovery._discover", return_value=rediscovered) as mock_discover:
            mock_storage.list_relationships.return_value = stale_cached
            result = get_or_refresh_catalog("DB", "SCH", require_table="DQTEST_ORPHAN_FK")

        assert result == rediscovered
        mock_discover.assert_called_once_with("DB", "SCH")

    def test_force_true_always_refreshes(self):
        """force=True bypasses the cache regardless of freshness or coverage."""
        from app.services.relationship_discovery import get_or_refresh_catalog
        cached = [_rel(from_table="ANY")]
        rediscovered = [_rel(from_table="NEW")]

        with patch("app.services.relationship_discovery.storage") as mock_storage, \
             patch("app.services.relationship_discovery._discover", return_value=rediscovered) as mock_discover:
            mock_storage.list_relationships.return_value = cached
            result = get_or_refresh_catalog("DB", "SCH", force=True)

        assert result == rediscovered
        mock_discover.assert_called_once()

    def test_stale_cache_triggers_refresh_regardless(self):
        from app.services.relationship_discovery import get_or_refresh_catalog
        rediscovered = [_rel(from_table="FRESH")]

        with patch("app.services.relationship_discovery.storage") as mock_storage, \
             patch("app.services.relationship_discovery._fresh_enough", return_value=False), \
             patch("app.services.relationship_discovery._discover", return_value=rediscovered) as mock_discover:
            mock_storage.list_relationships.return_value = [_rel(from_table="OLD")]
            result = get_or_refresh_catalog("DB", "SCH")

        assert result == rediscovered
        mock_discover.assert_called_once()


class TestFkShapeDetection:
    """Sanity: the regex used for FK shape correctly identifies foreign-key
    columns from typical naming conventions."""

    def test_parent_id_is_fk_shape(self):
        from app.services.relationship_discovery import _FK_SHAPE_RE
        assert _FK_SHAPE_RE.search("PARENT_ID")
        assert _FK_SHAPE_RE.search("CUSTOMER_ID")
        assert _FK_SHAPE_RE.search("ORDER_ID")

    def test_plain_id_is_fk_shape(self):
        from app.services.relationship_discovery import _FK_SHAPE_RE
        # bare "ID" is also FK-shape via _ID$ match (the $ needs a preceding _)
        # actually — the regex is _ID$, so "ID" alone won't match. That's OK
        # for FK detection (bare ID is typically the table's own key).
        assert not _FK_SHAPE_RE.search("ID")

    def test_non_fk_columns_not_matched(self):
        from app.services.relationship_discovery import _FK_SHAPE_RE
        assert not _FK_SHAPE_RE.search("NAME")
        assert not _FK_SHAPE_RE.search("CREATED_AT")
        assert not _FK_SHAPE_RE.search("EMAIL")


class TestFreshEnough:

    def test_fresh_cache_within_ttl_is_fresh(self):
        from app.services.relationship_discovery import _fresh_enough
        recent = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)
        rows = [_rel("T", last_verified_at=recent)]
        assert _fresh_enough(rows, ttl_hours=24) is True

    def test_old_cache_beyond_ttl_is_stale(self):
        from app.services.relationship_discovery import _fresh_enough
        old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=25)
        rows = [_rel("T", last_verified_at=old)]
        assert _fresh_enough(rows, ttl_hours=24) is False

    def test_empty_rows_is_stale(self):
        from app.services.relationship_discovery import _fresh_enough
        assert _fresh_enough([], ttl_hours=24) is False

    def test_all_null_timestamps_is_stale(self):
        """Regression: was crashing with ValueError from min() on empty sequence."""
        from app.services.relationship_discovery import _fresh_enough
        r = _rel("T")
        r.last_verified_at = None
        assert _fresh_enough([r], ttl_hours=24) is False

    def test_naive_datetime_treated_as_utc(self):
        from app.services.relationship_discovery import _fresh_enough
        naive = datetime.datetime.utcnow() - datetime.timedelta(hours=1)
        rows = [_rel("T", last_verified_at=naive)]
        assert _fresh_enough(rows, ttl_hours=24) is True

    def test_oldest_timestamp_used(self):
        """With mixed timestamps, the oldest determines freshness."""
        from app.services.relationship_discovery import _fresh_enough
        recent = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)
        old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=25)
        rows = [_rel("T1", last_verified_at=recent), _rel("T2", last_verified_at=old)]
        assert _fresh_enough(rows, ttl_hours=24) is False


class TestTypeCompatibility:

    def test_same_numeric_types_are_compatible(self):
        from app.services.relationship_discovery import _types_compatible
        assert _types_compatible("NUMBER", "INTEGER") is True
        assert _types_compatible("BIGINT", "DECIMAL") is True

    def test_same_text_types_are_compatible(self):
        from app.services.relationship_discovery import _types_compatible
        assert _types_compatible("VARCHAR", "TEXT") is True

    def test_numeric_vs_text_is_incompatible(self):
        from app.services.relationship_discovery import _types_compatible
        assert _types_compatible("NUMBER", "VARCHAR") is False

    def test_unknown_type_treated_as_compatible(self):
        """Unknown types must not over-skip — treat as compatible."""
        from app.services.relationship_discovery import _types_compatible
        assert _types_compatible(None, "VARCHAR") is True
        assert _types_compatible("ARRAY", "NUMBER") is True

    def test_type_with_precision_suffix_handled(self):
        from app.services.relationship_discovery import _types_compatible
        assert _types_compatible("NUMBER(38,0)", "INTEGER") is True
        assert _types_compatible("VARCHAR(256)", "TEXT") is True
        assert _types_compatible("NUMBER(10)", "VARCHAR(50)") is False


class TestBuildCandidates:

    def test_matching_column_name_produces_candidate(self):
        from app.services.relationship_discovery import _build_candidates
        fk = {"ORDERS": ["CUSTOMER_ID"]}
        pk = {"CUSTOMERS": ["CUSTOMER_ID"]}
        result = _build_candidates(fk, pk)
        assert len(result) == 1
        assert result[0] == {
            "from_table": "ORDERS", "from_column": "CUSTOMER_ID",
            "to_table": "CUSTOMERS", "to_column": "CUSTOMER_ID",
        }

    def test_same_table_excluded(self):
        from app.services.relationship_discovery import _build_candidates
        fk = {"CUSTOMERS": ["CUSTOMER_ID"]}
        pk = {"CUSTOMERS": ["CUSTOMER_ID"]}
        assert _build_candidates(fk, pk) == []

    def test_no_name_match_produces_no_candidates(self):
        from app.services.relationship_discovery import _build_candidates
        fk = {"ORDERS": ["VENDOR_ID"]}
        pk = {"CUSTOMERS": ["CUSTOMER_ID"]}
        assert _build_candidates(fk, pk) == []

    def test_case_insensitive_name_match(self):
        from app.services.relationship_discovery import _build_candidates
        fk = {"ORDERS": ["customer_id"]}
        pk = {"CUSTOMERS": ["CUSTOMER_ID"]}
        result = _build_candidates(fk, pk)
        assert len(result) == 1

    def test_multiple_fk_columns_expand_correctly(self):
        from app.services.relationship_discovery import _build_candidates
        fk = {"ORDERS": ["CUSTOMER_ID", "PRODUCT_ID"]}
        pk = {"CUSTOMERS": ["CUSTOMER_ID"], "PRODUCTS": ["PRODUCT_ID"]}
        result = _build_candidates(fk, pk)
        pairs = {(r["from_column"], r["to_table"]) for r in result}
        assert pairs == {("CUSTOMER_ID", "CUSTOMERS"), ("PRODUCT_ID", "PRODUCTS")}


class TestOwnPkNames:

    def test_orders_excludes_order_id(self):
        from app.services.relationship_discovery import _own_pk_names
        names = _own_pk_names("ORDERS")
        assert "ORDER_ID" in names

    def test_customers_excludes_customer_id(self):
        from app.services.relationship_discovery import _own_pk_names
        names = _own_pk_names("CUSTOMERS")
        assert "CUSTOMER_ID" in names

    def test_surrogate_keys_always_excluded(self):
        from app.services.relationship_discovery import _own_pk_names
        names = _own_pk_names("ANY_TABLE")
        assert "SURROGATE_KEY" in names
        assert "ROW_ID" in names
        assert "RECORD_ID" in names


class TestFetchRowCountsCaseNormalization:
    """Regression: SHOW TABLES returns lowercase 'name' on many Snowflake
    setups, but from_table comes from INFORMATION_SCHEMA (uppercase).
    Without .upper() normalization, row_counts.get(from_table) always
    returned 0, making the LARGE_TABLE_ROW_GUARD branch dead code."""

    def test_lowercase_name_normalized_to_uppercase_key(self):
        from app.services.relationship_discovery import _fetch_row_counts
        with patch("app.services.relationship_discovery.sf_session") as mock_sf:
            mock_sf.query.return_value = [{"name": "orders", "rows": "6000000"}]
            result = _fetch_row_counts("DB", "SCH")
        assert "ORDERS" in result
        assert result["ORDERS"] == 6_000_000

    def test_uppercase_name_key_preserved(self):
        from app.services.relationship_discovery import _fetch_row_counts
        with patch("app.services.relationship_discovery.sf_session") as mock_sf:
            mock_sf.query.return_value = [{"NAME": "ORDERS", "ROWS": "500"}]
            result = _fetch_row_counts("DB", "SCH")
        assert result.get("ORDERS") == 500

    def test_show_tables_failure_returns_empty(self):
        from app.services.relationship_discovery import _fetch_row_counts
        with patch("app.services.relationship_discovery.sf_session") as mock_sf:
            mock_sf.query.side_effect = Exception("permission denied")
            result = _fetch_row_counts("DB", "SCH")
        assert result == {}


class TestResolvePkOwnership:

    def test_most_unique_table_wins_pk_ownership(self):
        from app.services.relationship_discovery import _resolve_pk_ownership_by_relative_uniqueness
        pk_shaped = {"CUSTOMERS": ["CUSTOMER_ID"], "ORDERS": ["CUSTOMER_ID"]}
        with patch("app.services.relationship_discovery.sf_session") as mock_sf:
            def side_effect(sql):
                if "CUSTOMERS" in sql:
                    return [{"TOTAL": 100, "D_0": 100}]   # 100% unique → owner
                if "ORDERS" in sql:
                    return [{"TOTAL": 500, "D_0": 80}]    # 16% unique → referencer
                return []
            mock_sf.query.side_effect = side_effect
            result = _resolve_pk_ownership_by_relative_uniqueness("DB", "SCH", pk_shaped)
        assert "CUSTOMERS" in result
        assert "CUSTOMER_ID" in result["CUSTOMERS"]
        assert "ORDERS" not in result

    def test_below_min_ratio_excluded(self):
        """A column that's only e.g. 10% unique in the best table is not a real
        PK — should not appear in the result at all."""
        from app.services.relationship_discovery import _resolve_pk_ownership_by_relative_uniqueness
        pk_shaped = {"STAGING": ["ORDER_ID"]}
        with patch("app.services.relationship_discovery.sf_session") as mock_sf:
            mock_sf.query.return_value = [{"TOTAL": 1000, "D_0": 80}]  # 8% unique
            result = _resolve_pk_ownership_by_relative_uniqueness("DB", "SCH", pk_shaped)
        assert result == {}

    def test_query_failure_skips_table_gracefully(self):
        from app.services.relationship_discovery import _resolve_pk_ownership_by_relative_uniqueness
        pk_shaped = {"CUSTOMERS": ["CUSTOMER_ID"]}
        with patch("app.services.relationship_discovery.sf_session") as mock_sf:
            mock_sf.query.side_effect = Exception("timeout")
            result = _resolve_pk_ownership_by_relative_uniqueness("DB", "SCH", pk_shaped)
        assert result == {}


# ── Gap fixes ─────────────────────────────────────────────────────────────


class TestFkShapeExtended:
    """Gap #2: _FK_SHAPE_RE now matches _KEY and _REF suffixes."""

    def test_key_suffix_is_fk_shape(self):
        from app.services.relationship_discovery import _FK_SHAPE_RE
        assert _FK_SHAPE_RE.search("CUSTOMER_KEY")
        assert _FK_SHAPE_RE.search("PRODUCT_KEY")

    def test_ref_suffix_is_fk_shape(self):
        from app.services.relationship_discovery import _FK_SHAPE_RE
        assert _FK_SHAPE_RE.search("CUSTOMER_REF")
        assert _FK_SHAPE_RE.search("ORDER_REF")

    def test_id_suffix_still_matches(self):
        from app.services.relationship_discovery import _FK_SHAPE_RE
        assert _FK_SHAPE_RE.search("CUSTOMER_ID")

    def test_non_fk_suffixes_not_matched(self):
        from app.services.relationship_discovery import _FK_SHAPE_RE
        assert not _FK_SHAPE_RE.search("CUSTOMER_NAME")
        assert not _FK_SHAPE_RE.search("CREATED_AT")
        assert not _FK_SHAPE_RE.search("IDEMPOTENCY_TOKEN")  # doesn't end in _ID


class TestOwnPkNamesFixed:
    """Gap #3: _own_pk_names no longer produces mangled names for irregular
    plurals or multi-word table names."""

    def test_orders_excludes_order_id_and_orders_id(self):
        from app.services.relationship_discovery import _own_pk_names
        names = _own_pk_names("ORDERS")
        assert "ORDER_ID" in names    # strip-S singularization
        assert "ORDERS_ID" in names   # full-name form

    def test_categories_excludes_correct_forms(self):
        from app.services.relationship_discovery import _own_pk_names
        names = _own_pk_names("CATEGORIES")
        # old rstrip("S") produced CATEGORIE_ID — must NOT appear
        assert "CATEGORIE_ID" not in names
        assert "CATEGORIES_ID" in names   # full-name always present

    def test_status_excludes_correct_forms(self):
        from app.services.relationship_discovery import _own_pk_names
        names = _own_pk_names("STATUS")
        # old rstrip("S") produced STATU_ID — must NOT appear
        assert "STATU_ID" not in names
        assert "STATUS_ID" in names

    def test_product_categories_includes_acronym(self):
        from app.services.relationship_discovery import _own_pk_names
        names = _own_pk_names("PRODUCT_CATEGORIES")
        assert "PC_ID" in names

    def test_universal_surrogates_always_present(self):
        from app.services.relationship_discovery import _own_pk_names
        names = _own_pk_names("ANY_TABLE")
        assert "SURROGATE_KEY" in names
        assert "ROW_ID" in names
        assert "RECORD_ID" in names
        assert "ID" in names


class TestBuildPrefixCandidates:
    """Gap #1: ORDERS.CUSTOMER_ID → CUSTOMERS.ID (FK name ≠ PK name)."""

    def test_prefix_match_finds_bare_id_pk(self):
        from app.services.relationship_discovery import _build_prefix_candidates
        fk = {"ORDERS": ["CUSTOMER_ID"]}
        pk = {"CUSTOMERS": ["ID"]}
        all_tables = ["ORDERS", "CUSTOMERS"]
        result = _build_prefix_candidates(fk, pk, all_tables)
        assert len(result) == 1
        assert result[0] == {
            "from_table": "ORDERS", "from_column": "CUSTOMER_ID",
            "to_table": "CUSTOMERS", "to_column": "ID",
        }

    def test_prefix_match_singular_table_name(self):
        """ORDERS.CUSTOMER_ID → CUSTOMER.ID (singular, no S)."""
        from app.services.relationship_discovery import _build_prefix_candidates
        fk = {"ORDERS": ["CUSTOMER_ID"]}
        pk = {"CUSTOMER": ["ID"]}
        all_tables = ["ORDERS", "CUSTOMER"]
        result = _build_prefix_candidates(fk, pk, all_tables)
        assert len(result) == 1
        assert result[0]["to_table"] == "CUSTOMER"

    def test_prefix_match_key_suffix(self):
        """ORDERS.CUSTOMER_KEY → CUSTOMERS.ID."""
        from app.services.relationship_discovery import _build_prefix_candidates
        fk = {"ORDERS": ["CUSTOMER_KEY"]}
        pk = {"CUSTOMERS": ["ID"]}
        all_tables = ["ORDERS", "CUSTOMERS"]
        result = _build_prefix_candidates(fk, pk, all_tables)
        assert len(result) == 1

    def test_no_match_when_target_has_no_bare_pk(self):
        """Target table exists but has CUSTOMER_ID not bare ID — no prefix match."""
        from app.services.relationship_discovery import _build_prefix_candidates
        fk = {"ORDERS": ["CUSTOMER_ID"]}
        pk = {"CUSTOMERS": ["CUSTOMER_ID"]}   # no bare ID
        all_tables = ["ORDERS", "CUSTOMERS"]
        result = _build_prefix_candidates(fk, pk, all_tables)
        assert result == []

    def test_ambiguous_table_names_skipped(self):
        """Both CUSTOMER and CUSTOMERS exist → ambiguous → no candidate."""
        from app.services.relationship_discovery import _build_prefix_candidates
        fk = {"ORDERS": ["CUSTOMER_ID"]}
        pk = {"CUSTOMER": ["ID"], "CUSTOMERS": ["ID"]}
        all_tables = ["ORDERS", "CUSTOMER", "CUSTOMERS"]
        result = _build_prefix_candidates(fk, pk, all_tables)
        assert result == []

    def test_same_table_excluded(self):
        from app.services.relationship_discovery import _build_prefix_candidates
        fk = {"CUSTOMERS": ["CUSTOMER_ID"]}
        pk = {"CUSTOMERS": ["ID"]}
        all_tables = ["CUSTOMERS"]
        result = _build_prefix_candidates(fk, pk, all_tables)
        assert result == []

    def test_no_suffix_column_produces_no_candidate(self):
        from app.services.relationship_discovery import _build_prefix_candidates
        fk = {"ORDERS": ["CUSTOMER_CODE"]}  # _CODE not a recognized FK suffix
        pk = {"CUSTOMERS": ["ID"]}
        all_tables = ["ORDERS", "CUSTOMERS"]
        result = _build_prefix_candidates(fk, pk, all_tables)
        assert result == []


class TestVerifyOrphanRateEmptyTable:
    """Gap #4: empty child table returns 'empty' sentinel, not None."""

    def test_empty_table_returns_sentinel(self):
        from app.services.relationship_discovery import _verify_orphan_rate
        with patch("app.services.relationship_discovery.sf_session") as mock_sf:
            mock_sf.query.return_value = [{"TOTAL": 0, "ORPHANS": 0}]
            result = _verify_orphan_rate("DB", "SCH", "ORDERS", "CUSTOMER_ID",
                                         "CUSTOMERS", "CUSTOMER_ID", {})
        assert result == "empty"

    def test_normal_result_still_returns_tuple(self):
        from app.services.relationship_discovery import _verify_orphan_rate
        with patch("app.services.relationship_discovery.sf_session") as mock_sf:
            mock_sf.query.return_value = [{"TOTAL": 100, "ORPHANS": 5}]
            result = _verify_orphan_rate("DB", "SCH", "ORDERS", "CUSTOMER_ID",
                                         "CUSTOMERS", "CUSTOMER_ID", {})
        assert isinstance(result, tuple)
        orphan_rate, total, orphans = result
        assert orphan_rate == 0.05
        assert total == 100
        assert orphans == 5

    def test_query_failure_returns_none(self):
        from app.services.relationship_discovery import _verify_orphan_rate
        with patch("app.services.relationship_discovery.sf_session") as mock_sf:
            mock_sf.query.side_effect = Exception("SQL error")
            result = _verify_orphan_rate("DB", "SCH", "ORDERS", "CUSTOMER_ID",
                                         "CUSTOMERS", "CUSTOMER_ID", {})
        assert result is None

    def test_empty_query_result_returns_none(self):
        from app.services.relationship_discovery import _verify_orphan_rate
        with patch("app.services.relationship_discovery.sf_session") as mock_sf:
            mock_sf.query.return_value = []
            result = _verify_orphan_rate("DB", "SCH", "ORDERS", "CUSTOMER_ID",
                                         "CUSTOMERS", "CUSTOMER_ID", {})
        assert result is None
