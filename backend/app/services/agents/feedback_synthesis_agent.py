"""
FeedbackSynthesisAgent — cross-run pattern synthesis.

After each review round, raw approve/reject lessons accumulate in
RULE_REVIEW_LESSONS. This agent reads the last N lessons for a given
table_type + bare table name and calls Claude once to distill them into a
reusable memo:

  {
    "always_approve": ["non-negative check on amount columns", ...],
    "always_reject":  ["column comment checks on staging tables", ...],
    "column_advice":  {"STATUS": "accepted_values always approved here", ...},
    "table_type_notes": "one paragraph of nuance for this table type",
    "confidence": 0-100
  }

The memo is upserted into RULE_FEEDBACK_MEMOS keyed by
(bare_table_name, table_type). Next time _format_past_context runs on a
similar table it finds the memo and injects it BEFORE raw lessons — so
Claude gets high-signal synthesised guidance first, raw evidence second.

The agent runs best-effort in the background (non-blocking). A failure
here never affects the current run.
"""
import json
import logging
import re
from typing import Optional

from app.services.claude_client import ask_claude
from app.services import storage

logger = logging.getLogger(__name__)

_MIN_LESSONS_TO_SYNTHESISE = 3   # don't bother if fewer than 3 data points
_MAX_LESSONS_FOR_SYNTHESIS  = 40  # cap prompt size

_SYSTEM = (
    "You are a senior data quality architect reviewing accumulated human "
    "feedback on auto-generated data quality rule proposals. Your job is to "
    "extract reusable patterns from this feedback so future rule proposals are "
    "better targeted. Be specific and concrete — reference column names, check "
    "types, and table characteristics that appear repeatedly. Avoid generic advice."
)


class FeedbackSynthesisAgent:
    """
    Synthesises raw approve/reject lessons into a structured memo and
    persists it for future runs.
    """

    def run(
        self,
        table_fqn: str,
        table_type: str,
    ) -> Optional[dict]:
        """
        Read recent lessons for this table/type, synthesise a memo, upsert it.
        Returns the memo dict, or None if synthesis was skipped/failed.
        """
        bare_table = table_fqn.upper().split(".")[-1]

        # Fetch raw lessons
        try:
            lessons = storage.get_lessons_for_synthesis(
                table_fqn=table_fqn,
                table_type=table_type,
                limit=_MAX_LESSONS_FOR_SYNTHESIS,
            )
        except Exception as e:
            logger.warning(f"[FeedbackSynthesis] Could not load lessons: {e}")
            return None

        if len(lessons) < _MIN_LESSONS_TO_SYNTHESISE:
            logger.debug(
                f"[FeedbackSynthesis] Only {len(lessons)} lessons for "
                f"{bare_table}/{table_type} — skipping synthesis (need {_MIN_LESSONS_TO_SYNTHESISE})"
            )
            return None

        approved = [l for l in lessons if l["verdict"] == "approved"]
        rejected = [l for l in lessons if l["verdict"] == "rejected"]

        prompt = self._build_prompt(bare_table, table_type, approved, rejected)
        try:
            raw = ask_claude(prompt, system=_SYSTEM, max_tokens=4000)
            memo = self._parse_memo(raw)
        except Exception as e:
            logger.warning(f"[FeedbackSynthesis] Claude call failed: {e}")
            return None

        if not memo:
            logger.warning(f"[FeedbackSynthesis] Could not parse memo for {bare_table}/{table_type}")
            return None

        try:
            storage.upsert_feedback_memo(
                bare_table_name=bare_table,
                table_type=table_type,
                memo=memo,
                lesson_count=len(lessons),
            )
            logger.info(
                f"[FeedbackSynthesis] Memo upserted for {bare_table}/{table_type} "
                f"({len(approved)} approved, {len(rejected)} rejected → "
                f"{len(memo.get('always_approve', []))} always-approve patterns, "
                f"{len(memo.get('always_reject', []))} always-reject patterns)"
            )
        except Exception as e:
            logger.warning(f"[FeedbackSynthesis] Could not persist memo: {e}")

        return memo

    # ── Prompt builder ────────────────────────────────────────────────────

    def _build_prompt(
        self,
        bare_table: str,
        table_type: str,
        approved: list,
        rejected: list,
    ) -> str:
        def fmt_lesson(l: dict) -> str:
            col = f"  column={l['column']}" if l.get("column") else ""
            sev = f"  severity={l['severity']}" if l.get("severity") else ""
            reason = f"\n    reason: {l['reason']}" if l.get("reason") else ""
            return f"  - {l['check_concept']}{col}{sev}{reason}"

        approved_lines = "\n".join(fmt_lesson(l) for l in approved) or "  (none)"
        rejected_lines = "\n".join(fmt_lesson(l) for l in rejected) or "  (none)"

        return (
            f"Table pattern: {bare_table}  (classified as: {table_type})\n\n"
            f"APPROVED rule proposals ({len(approved)} total):\n{approved_lines}\n\n"
            f"REJECTED rule proposals ({len(rejected)} total):\n{rejected_lines}\n\n"
            "Based on this feedback history, produce a synthesis memo. "
            "Identify patterns that are ALWAYS worth proposing for this table type, "
            "patterns that are NEVER worth proposing, per-column advice where a "
            "column name recurs, and any nuance specific to this table type.\n\n"
            "Respond with valid JSON only — no markdown, no prose outside the JSON:\n"
            "{\n"
            '  "always_approve": ["concise pattern description", ...],\n'
            '  "always_reject":  ["concise pattern description", ...],\n'
            '  "column_advice":  {"COLUMN_NAME": "one sentence", ...},\n'
            '  "table_type_notes": "one paragraph of nuance for this table type",\n'
            '  "confidence": <0-100 reflecting how many consistent data points exist>\n'
            "}"
        )

    # ── Response parser ───────────────────────────────────────────────────

    def _parse_memo(self, raw: str) -> Optional[dict]:
        raw = raw.strip()
        raw = re.sub(r"^```[a-z]*\n?", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\n?```$", "", raw).strip()
        try:
            memo = json.loads(raw)
            if not isinstance(memo, dict):
                return None
            # Normalise — ensure expected keys exist
            memo.setdefault("always_approve", [])
            memo.setdefault("always_reject", [])
            memo.setdefault("column_advice", {})
            memo.setdefault("table_type_notes", "")
            memo.setdefault("confidence", 50)
            return memo
        except Exception:
            # Try extracting a JSON object
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(0))
                except Exception:
                    pass
        return None
