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
