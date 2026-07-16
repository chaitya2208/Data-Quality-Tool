"""
Summarize a completed RuleIntelligence run for quick comparison against the
TEST_FINDINGS.md baseline: active/skipped/unused counts, novel vs reuse split,
tool-call usage, and definition library growth.

Usage (from backend/):
    python summarize_run.py <run_id>
"""
import json
import sys

from app.services import storage


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python summarize_run.py <run_id>")
        return 1

    run_id = sys.argv[1]
    run = storage.get_agent_run(run_id)
    if not run:
        print(f"No run found with id={run_id}")
        return 1

    print(f"=== Run {run_id} ===")
    print(f"status: {run.status}")
    print(f"table:  {run.database}.{run.schema_name}.{run.table}")

    state = run.instance_review_state or {}
    active = state.get("active", [])
    skipped = state.get("skipped", [])
    unused = state.get("unused_library", [])
    signals_missed = state.get("signals_missed", [])

    novel = [a for a in active if a.get("is_new_definition")]
    reuse_new_instance = [a for a in active if a.get("is_new_instance") and not a.get("is_new_definition")]
    pre_existing = [a for a in active if not a.get("is_new_instance")]

    print(f"\nactive: {len(active)}  (novel_definition={len(novel)}, reuse_new_instance={len(reuse_new_instance)}, pre_existing={len(pre_existing)})")
    print(f"skipped: {len(skipped)}")
    print(f"unused_library: {len(unused)}")
    print(f"signals_missed: {signals_missed}")
    print(f"parse_failed: {state.get('parse_failed')}")

    if novel:
        print("\n-- novel definitions proposed --")
        for a in novel:
            print(f"  [{a['name']}] scope={a.get('scope')} target={a.get('target_config')}")
            print(f"    reason: {(a.get('reason') or '')[:200]}")

    if skipped:
        print("\n-- skipped (Claude actively rejected) --")
        for s in skipped:
            print(f"  [{s['name']}] reason: {(s.get('reason') or '')[:200]}")

    # Pull the raw intelligence log for this run for tool-call / signal detail
    log = storage.get_intelligence_log_for_run(run_id)
    if log:
        print("\n-- intelligence log --")
        print(f"table_type={log.table_type} confidence={log.table_type_confidence}")
        print(f"proposals={log.proposals_count} suppressed={log.suppressed_count} "
              f"approved={log.approved_count} rejected={log.rejected_count} model={log.model_used}")
        signals = log.signals_used or {}
        tool_calls = signals.get("sample_tool_calls") or signals.get("tool_calls")
        if tool_calls:
            print(f"sample_tool_calls: {json.dumps(tool_calls, indent=2, default=str)[:1500]}")
        tail_present = "tail_values" in json.dumps(signals, default=str)
        print(f"tail_values referenced in signals_used: {tail_present}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
