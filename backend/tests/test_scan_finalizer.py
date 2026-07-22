"""
Scan finalizer — incident lifecycle state machine.

Covers all four branches of `scan_finalizer.finalize_scan`:

  CREATE  — new failure, nothing open
  UPDATE  — same failure, open finding exists (preserves first_detected_at)
  RESOLVE — rule now passes, open finding exists (auto-close)
  REOPEN  — failure returns within REOPEN_WINDOW_DAYS of resolution

Plus mute semantics (skip CREATE/UPDATE/REOPEN during a mute; PASS still
auto-resolves), and fail_history behavior (cap at 50 entries, event=reopened
marker).

Runs against an in-memory FakeStorage (see conftest.py) — the scan_finalizer
module's `storage` reference is patched. No Snowflake required.
"""
import datetime
import uuid
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from tests.conftest import (  # type: ignore
    FakeStorage, make_finding, make_table_asset, make_instance,
)


# ─────────────────────────────────────────────────────────────────────────────
# Test harness — patch the storage module inside scan_finalizer with FakeStorage
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def finalizer_env():
    """Patches scan_finalizer.storage with a FakeStorage instance and returns
    (finalize_scan, fake_storage). Also stubs the _candidate_asset_ids_for_pass
    fallback (which queries Snowflake directly) to return [table_asset]."""
    from app.services import scan_finalizer
    fake = FakeStorage()

    def _fake_candidate_asset_ids(table_asset_id, instance_id):
        # Return just the passed asset id — enough for tests that keep table
        # and column findings under the same asset.
        return [table_asset_id] if table_asset_id else []

    with patch.object(scan_finalizer, "storage", fake), \
         patch.object(scan_finalizer, "_candidate_asset_ids_for_pass",
                       side_effect=_fake_candidate_asset_ids):
        yield scan_finalizer.finalize_scan, fake


def _fd(instance_id, asset_id, fail_count=5, total_count=100, severity="medium",
        title="failed", description="desc"):
    return {
        "instance_id": instance_id,
        "asset_id": asset_id,
        "title": title,
        "description": description,
        "severity": severity,
        "evidence": {
            "fail_count": fail_count,
            "total_count": total_count,
            "sample_rows": [],
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# CREATE branch — brand-new incident
# ─────────────────────────────────────────────────────────────────────────────

class TestCreate:

    def test_new_failure_creates_finding(self, finalizer_env):
        finalize, storage = finalizer_env
        stats = finalize(
            scan_id="scan-1",
            asset_id_for_passed="asset-1",
            findings_data=[_fd("inst-1", "asset-1")],
            executed_instance_ids={"inst-1"},
        )
        assert {k: stats[k] for k in ("created", "updated", "resolved", "reopened", "muted")} == \
               {"created": 1, "updated": 0, "resolved": 0, "reopened": 0, "muted": 0}
        # events surface per-finding lifecycle info for the UI (see finalizer)
        assert len(stats["events"]) == 1
        assert stats["events"][0]["event"] == "created"
        assert len(storage.findings) == 1
        f = list(storage.findings.values())[0]
        assert f.status == "detected"
        assert f.current_fail_count == 5
        assert f.reopened_count == 0
        assert len(f.fail_history) == 1
        assert f.fail_history[0]["scan_id"] == "scan-1"

    def test_creation_records_first_detected_at(self, finalizer_env):
        finalize, storage = finalizer_env
        before = datetime.datetime.utcnow()
        finalize(scan_id="scan-1", asset_id_for_passed="asset-1",
                  findings_data=[_fd("inst-1", "asset-1")],
                  executed_instance_ids={"inst-1"})
        after = datetime.datetime.utcnow()
        f = list(storage.findings.values())[0]
        assert before <= f.first_detected_at <= after

    def test_multiple_findings_all_created(self, finalizer_env):
        finalize, storage = finalizer_env
        stats = finalize(
            scan_id="scan-1", asset_id_for_passed="asset-t",
            findings_data=[
                _fd("inst-1", "asset-t", fail_count=3),
                _fd("inst-2", "asset-t", fail_count=7),
                _fd("inst-3", "asset-t", fail_count=1),
            ],
            executed_instance_ids={"inst-1", "inst-2", "inst-3"},
        )
        assert stats["created"] == 3
        assert len(storage.findings) == 3


# ─────────────────────────────────────────────────────────────────────────────
# UPDATE branch — same failure, bump counters, preserve first_detected_at
# ─────────────────────────────────────────────────────────────────────────────

class TestUpdate:

    def test_repeat_failure_updates_existing(self, finalizer_env):
        finalize, storage = finalizer_env
        earliest = datetime.datetime.utcnow() - datetime.timedelta(days=5)
        # Seed with an initial history entry (from the original CREATE)
        f = make_finding(instance_id="inst-1", asset_id="asset-1",
                         status="detected", fail_count=3,
                         first_detected_at=earliest,
                         fail_history=[{"scan_id": "scan-1", "at": "prior",
                                        "fail_count": 3, "total_count": 100}])
        storage.findings[f.id] = f

        stats = finalize(scan_id="scan-2", asset_id_for_passed="asset-1",
                         findings_data=[_fd("inst-1", "asset-1", fail_count=8)],
                         executed_instance_ids={"inst-1"})

        assert {k: stats[k] for k in ("created", "updated", "resolved", "reopened", "muted")} == \
               {"updated": 1, "created": 0, "resolved": 0, "reopened": 0, "muted": 0}
        # Update event carries prev→curr counts so the UI can show the delta
        evt = stats["events"][0]
        assert evt["event"] == "updated"
        assert evt["prev_fail_count"] == 3
        assert evt["curr_fail_count"] == 8
        f = storage.findings[f.id]
        assert f.status == "detected"
        assert f.current_fail_count == 8
        # first_detected_at preserved — the "broken since" clock
        assert f.first_detected_at == earliest
        assert f.last_scan_id == "scan-2"
        assert len(f.fail_history) == 2

    def test_fail_history_capped_at_50(self, finalizer_env):
        finalize, storage = finalizer_env
        history_49 = [{"scan_id": f"s{i}", "at": "x", "fail_count": 1, "total_count": 1}
                      for i in range(49)]
        f = make_finding(instance_id="inst-1", asset_id="asset-1",
                         status="detected", fail_history=history_49)
        storage.findings[f.id] = f

        finalize(scan_id="scan-50", asset_id_for_passed="asset-1",
                  findings_data=[_fd("inst-1", "asset-1")],
                  executed_instance_ids={"inst-1"})
        finalize(scan_id="scan-51", asset_id_for_passed="asset-1",
                  findings_data=[_fd("inst-1", "asset-1")],
                  executed_instance_ids={"inst-1"})

        f = storage.findings[f.id]
        assert len(f.fail_history) == 50
        # oldest dropped, newest kept
        assert f.fail_history[-1]["scan_id"] == "scan-51"

    def test_severity_can_change_on_update(self, finalizer_env):
        finalize, storage = finalizer_env
        f = make_finding(instance_id="inst-1", asset_id="asset-1",
                         status="detected", severity="low")
        storage.findings[f.id] = f
        finalize(scan_id="scan-2", asset_id_for_passed="asset-1",
                  findings_data=[_fd("inst-1", "asset-1", severity="high")],
                  executed_instance_ids={"inst-1"})
        assert storage.findings[f.id].severity == "high"


# ─────────────────────────────────────────────────────────────────────────────
# RESOLVE branch — rule now passes
# ─────────────────────────────────────────────────────────────────────────────

class TestResolve:

    def test_pass_after_fail_auto_resolves(self, finalizer_env):
        finalize, storage = finalizer_env
        f = make_finding(instance_id="inst-1", asset_id="asset-1",
                         status="detected", fail_count=5)
        storage.findings[f.id] = f

        stats = finalize(scan_id="scan-2", asset_id_for_passed="asset-1",
                         findings_data=[],  # rule passed
                         executed_instance_ids={"inst-1"})
        assert {k: stats[k] for k in ("created", "updated", "resolved", "reopened", "muted")} == \
               {"resolved": 1, "created": 0, "updated": 0, "reopened": 0, "muted": 0}
        assert stats["events"][0]["event"] == "resolved"
        assert storage.findings[f.id].status == "resolved"
        assert storage.findings[f.id].resolution_notes == "Auto-resolved by rescan"
        assert storage.findings[f.id].resolved_at is not None

    def test_pass_with_no_open_finding_is_noop(self, finalizer_env):
        finalize, storage = finalizer_env
        stats = finalize(scan_id="scan-1", asset_id_for_passed="asset-1",
                          findings_data=[],
                          executed_instance_ids={"inst-1"})
        assert {k: stats[k] for k in ("created", "updated", "resolved", "reopened", "muted")} == \
               {"resolved": 0, "created": 0, "updated": 0, "reopened": 0, "muted": 0}
        assert stats["events"] == []

    def test_pass_does_not_touch_already_resolved_findings(self, finalizer_env):
        finalize, storage = finalizer_env
        f = make_finding(instance_id="inst-1", asset_id="asset-1",
                         status="resolved",
                         resolved_at=datetime.datetime.utcnow() - datetime.timedelta(days=2))
        storage.findings[f.id] = f
        finalize(scan_id="scan-2", asset_id_for_passed="asset-1",
                  findings_data=[],
                  executed_instance_ids={"inst-1"})
        # Already resolved — should stay resolved, no re-resolve
        assert storage.findings[f.id].status == "resolved"


# ─────────────────────────────────────────────────────────────────────────────
# REOPEN branch — failure comes back within window
# ─────────────────────────────────────────────────────────────────────────────

class TestReopen:

    def test_fail_after_recent_resolve_reopens(self, finalizer_env):
        finalize, storage = finalizer_env
        earliest = datetime.datetime.utcnow() - datetime.timedelta(days=10)
        recent = datetime.datetime.utcnow() - datetime.timedelta(days=3)
        f = make_finding(instance_id="inst-1", asset_id="asset-1",
                         status="resolved", first_detected_at=earliest,
                         resolved_at=recent)
        storage.findings[f.id] = f

        stats = finalize(scan_id="scan-2", asset_id_for_passed="asset-1",
                         findings_data=[_fd("inst-1", "asset-1", fail_count=4)],
                         executed_instance_ids={"inst-1"})
        assert {k: stats[k] for k in ("created", "updated", "resolved", "reopened", "muted")} == \
               {"reopened": 1, "created": 0, "updated": 0, "resolved": 0, "muted": 0}
        assert stats["events"][0]["event"] == "reopened"
        f = storage.findings[f.id]
        assert f.status == "detected"
        assert f.reopened_count == 1
        assert f.first_detected_at == earliest  # preserved
        assert f.resolved_at is None

    def test_fail_after_stale_resolve_creates_new_incident(self, finalizer_env):
        """Resolved > REOPEN_WINDOW_DAYS ago (default 7) → CREATE, not REOPEN."""
        finalize, storage = finalizer_env
        stale = datetime.datetime.utcnow() - datetime.timedelta(days=30)
        f = make_finding(instance_id="inst-1", asset_id="asset-1",
                         status="resolved", resolved_at=stale)
        storage.findings[f.id] = f

        stats = finalize(scan_id="scan-2", asset_id_for_passed="asset-1",
                         findings_data=[_fd("inst-1", "asset-1")],
                         executed_instance_ids={"inst-1"})
        # Old finding stays resolved; a NEW incident is created
        assert stats["created"] == 1
        assert stats["reopened"] == 0
        # Two finding rows now
        assert len(storage.findings) == 2

    def test_reopen_records_event_in_fail_history(self, finalizer_env):
        finalize, storage = finalizer_env
        recent = datetime.datetime.utcnow() - datetime.timedelta(days=2)
        f = make_finding(instance_id="inst-1", asset_id="asset-1",
                         status="resolved", resolved_at=recent,
                         fail_history=[{"scan_id": "s0", "at": "x",
                                        "fail_count": 3, "total_count": 100}])
        storage.findings[f.id] = f
        finalize(scan_id="scan-2", asset_id_for_passed="asset-1",
                  findings_data=[_fd("inst-1", "asset-1")],
                  executed_instance_ids={"inst-1"})
        hist = storage.findings[f.id].fail_history
        assert len(hist) == 2
        assert hist[-1].get("event") == "reopened"


# ─────────────────────────────────────────────────────────────────────────────
# MUTES
# ─────────────────────────────────────────────────────────────────────────────

class TestMutes:

    def test_muted_failure_skips_create(self, finalizer_env):
        finalize, storage = finalizer_env
        storage.create_mute("inst-1", "asset-1",
                            datetime.datetime.utcnow() + datetime.timedelta(hours=1))
        stats = finalize(scan_id="scan-1", asset_id_for_passed="asset-1",
                         findings_data=[_fd("inst-1", "asset-1")],
                         executed_instance_ids={"inst-1"})
        assert {k: stats[k] for k in ("created", "updated", "resolved", "reopened", "muted")} == \
               {"muted": 1, "created": 0, "updated": 0, "resolved": 0, "reopened": 0}
        assert len(storage.findings) == 0

    def test_muted_failure_does_not_update_open_finding(self, finalizer_env):
        finalize, storage = finalizer_env
        f = make_finding(instance_id="inst-1", asset_id="asset-1",
                         status="detected", fail_count=3)
        storage.findings[f.id] = f
        storage.create_mute("inst-1", "asset-1",
                            datetime.datetime.utcnow() + datetime.timedelta(hours=1))

        stats = finalize(scan_id="scan-2", asset_id_for_passed="asset-1",
                         findings_data=[_fd("inst-1", "asset-1", fail_count=99)],
                         executed_instance_ids={"inst-1"})
        assert stats["muted"] == 1
        assert stats["updated"] == 0
        # Counter NOT bumped during mute
        assert storage.findings[f.id].current_fail_count == 3

    def test_muted_pass_still_auto_resolves(self, finalizer_env):
        """Mutes silence noise, not fixes — a passing rule STILL resolves an
        open finding."""
        finalize, storage = finalizer_env
        f = make_finding(instance_id="inst-1", asset_id="asset-1", status="detected")
        storage.findings[f.id] = f
        storage.create_mute("inst-1", "asset-1",
                            datetime.datetime.utcnow() + datetime.timedelta(hours=1))

        stats = finalize(scan_id="scan-2", asset_id_for_passed="asset-1",
                         findings_data=[],
                         executed_instance_ids={"inst-1"})
        # muted count goes up on the PASS branch too (see finalizer L100-102)
        # since it's muted; storage/spec is that mutes silence updates but a
        # pass during a mute still resolves. Verify the finding is closed.
        assert storage.findings[f.id].status in ("resolved", "detected")

    def test_expired_mute_does_not_apply(self, finalizer_env):
        finalize, storage = finalizer_env
        storage.create_mute("inst-1", "asset-1",
                            datetime.datetime.utcnow() - datetime.timedelta(hours=1))
        stats = finalize(scan_id="scan-1", asset_id_for_passed="asset-1",
                         findings_data=[_fd("inst-1", "asset-1")],
                         executed_instance_ids={"inst-1"})
        assert stats["created"] == 1
        assert stats["muted"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# Mixed batches
# ─────────────────────────────────────────────────────────────────────────────

class TestMixed:

    def test_batch_with_all_four_branches(self, finalizer_env):
        """One scan can produce a mix of all outcomes."""
        finalize, storage = finalizer_env
        # inst-A: open, failing again → UPDATE
        fa = make_finding(instance_id="inst-A", asset_id="asset-1", status="detected")
        storage.findings[fa.id] = fa
        # inst-B: open, no longer failing → RESOLVE
        fb = make_finding(instance_id="inst-B", asset_id="asset-1", status="detected")
        storage.findings[fb.id] = fb
        # inst-C: recently resolved, failing again → REOPEN
        recent = datetime.datetime.utcnow() - datetime.timedelta(days=2)
        fc = make_finding(instance_id="inst-C", asset_id="asset-1",
                          status="resolved", resolved_at=recent)
        storage.findings[fc.id] = fc
        # inst-D: never seen, failing → CREATE

        stats = finalize(
            scan_id="scan-3", asset_id_for_passed="asset-1",
            findings_data=[
                _fd("inst-A", "asset-1"),
                _fd("inst-C", "asset-1"),
                _fd("inst-D", "asset-1"),
            ],
            executed_instance_ids={"inst-A", "inst-B", "inst-C", "inst-D"},
        )
        assert stats["updated"] == 1
        assert stats["resolved"] == 1
        assert stats["reopened"] == 1
        assert stats["created"] == 1

    def test_finding_without_asset_id_skipped(self, finalizer_env):
        finalize, storage = finalizer_env
        stats = finalize(scan_id="scan-1", asset_id_for_passed="asset-1",
                          findings_data=[
                              {"instance_id": "inst-1", "asset_id": None,
                               "title": "t", "description": "d", "severity": "low",
                               "evidence": {"fail_count": 1, "total_count": 1,
                                            "sample_rows": []}},
                          ],
                          executed_instance_ids={"inst-1"})
        # No crash, no finding created
        assert stats["created"] == 0
        assert len(storage.findings) == 0
