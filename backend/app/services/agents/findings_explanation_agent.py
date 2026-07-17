"""
Findings Explanation Agent — closes the loop end-to-end.

After FindingsAgent fires and detects violations, this agent looks at the
actual failing rows for each firing rule and produces:
  - root_cause: what is actually wrong in the data and why
  - fix_action: concrete recommended remediation step (SQL patch, process fix,
    upstream owner to notify, etc.)
  - affected_scope: how widespread the problem is (spot vs systemic)

This is what makes findings actionable rather than just a list of red flags.
One lightweight Claude call per firing instance (parallel, best-effort).
Results are stored back onto the Finding records and surfaced in the UI.

Called from coordinator._run_findings after FindingsAgent completes.
"""
import json
import logging
import re
from typing import Any, List, Optional

from app.services import storage
from app.services.snowflake_session import session as sf_session
from app.services.claude_client import ask_claude, ask_claude_json
from app.services.datasources import get_source

logger = logging.getLogger(__name__)

_MAX_SAMPLE_ROWS = 10
_MAX_FINDINGS_TO_EXPLAIN = 20  # cap to avoid runaway cost on huge scans

_EXPLAIN_SYSTEM = (
    "You are a senior data engineer performing root cause analysis on data "
    "quality violations. Given a failing rule and sample rows that violate it, "
    "explain WHY the data is wrong, how widespread the problem is, and what "
    "concrete action the data team should take to fix it. "
    "Be specific — reference actual values from the sample rows. "
    "Keep each field under 3 sentences."
)


class FindingsExplanationAgent:
    """
    Runs one lightweight Claude call per firing instance to explain why each
    violation happened and what to do about it.  All calls are best-effort —
    a failure on one instance never blocks the others or the pipeline.
    """

    def run(
        self,
        findings: List[Any],
        table_asset: Any,
        run_id: Optional[str] = None,
        connection_id: Optional[str] = None,
    ) -> None:
        """
        Mutates finding records in-place by updating their resolution_notes
        with AI-generated explanation JSON.  Returns nothing — callers don't
        need to wait on this; it's a value-add, not a gate.

        findings: list of Finding objects returned by FindingsAgent.run()
        connection_id: connection whose DataSource should be used to fetch
        sample violating rows. Falls back to sf_session ONLY when unresolved,
        so a Postgres-backed run does not silently query Snowflake.
        """
        # Group findings by instance so we make one call per rule, not per row
        by_instance: dict = {}
        for f in findings:
            iid = getattr(f, "instance_id", None)
            if not iid:
                continue
            by_instance.setdefault(iid, []).append(f)

        if not by_instance:
            return

        # Resolve the source once; safe to fail — the explain path degrades
        # to sf_session, which is correct for Snowflake connections and
        # gracefully-empty for anything else.
        source = None
        if connection_id:
            try:
                source = get_source(connection_id)
            except Exception as e:
                logger.warning(
                    f"[FindingsExplanation] Could not resolve source for "
                    f"connection {connection_id}: {e} — falling back to sf_session"
                )

        # Cap total calls to avoid runaway cost on tables with many rules firing
        instance_ids = list(by_instance.keys())[:_MAX_FINDINGS_TO_EXPLAIN]
        logger.info(
            f"[FindingsExplanation] Explaining {len(instance_ids)} firing instance(s) "
            f"on {table_asset.fqn} (source={'multi' if source else 'sf_session_fallback'})"
        )

        for instance_id in instance_ids:
            instance_findings = by_instance[instance_id]
            try:
                self._explain_instance(instance_id, instance_findings, table_asset, source)
            except Exception as e:
                logger.warning(
                    f"[FindingsExplanation] Failed to explain instance {instance_id}: {e}"
                )

        # Findings-list API caches results for 30s. Background evidence writes
        # would otherwise stay invisible to the UI until the TTL rolled — flush
        # the cache once explanations finish so the next page load sees the
        # AI analysis and sample rows immediately.
        try:
            from app.api.findings import _invalidate_findings_cache
            _invalidate_findings_cache()
        except Exception as e:
            logger.debug(f"[FindingsExplanation] Cache invalidation skipped: {e}")

    def _explain_instance(
        self,
        instance_id: str,
        instance_findings: List[Any],
        table_asset: Any,
        source: Optional[Any] = None,
    ) -> None:
        instance = storage.get_instance(instance_id)
        if not instance:
            return
        definition = storage.get_definition(instance.definition_id) if instance.definition_id else None

        rule_name = definition.name if definition else instance_id
        rule_description = definition.description if definition else ""
        rule_sql = instance.rule_sql or ""
        target_col = (instance.target_config or {}).get("column", "")
        finding_count = len(instance_findings)
        first_finding = instance_findings[0]

        # Fetch violating rows for concrete evidence — through the run's own
        # DataSource so Postgres findings get real sample rows too, not empty.
        sample_text = self._fetch_violating_rows(
            rule_sql=rule_sql,
            table_asset=table_asset,
            target_col=target_col,
            source=source,
            finding_count=finding_count,
        )

        # Build any context from the existing finding evidence field
        evidence_snippets = list({
            getattr(f, "description", "") for f in instance_findings[:5]
            if getattr(f, "description", "")
        })
        evidence_text = "\n".join(f"  - {e}" for e in evidence_snippets[:3])

        prompt = (
            f"Table: {table_asset.fqn}\n"
            f"Row count: {getattr(table_asset, 'row_count', 'unknown')}\n"
            f"Rule: {rule_name}\n"
            f"Description: {rule_description}\n"
            f"Target column: {target_col or 'table-level'}\n"
            f"Check SQL: {rule_sql[:400] if rule_sql else '(python handler)'}\n"
            f"Findings count: {finding_count}\n"
            f"Finding descriptions:\n{evidence_text or '  (none)'}\n\n"
            f"Sample violating rows:\n{sample_text}\n\n"
            "Respond with JSON only:\n"
            '{\n'
            '  "root_cause": "Why is this data wrong? Be specific — cite actual values.",\n'
            '  "affected_scope": "spot|systemic|unknown — and a one-sentence characterisation",\n'
            '  "fix_action": "Concrete step: SQL patch, upstream process to fix, team to notify, etc.",\n'
            '  "confidence": "high|medium|low — how confident are you in this analysis"\n'
            '}'
        )

        explanation = ask_claude_json(
            prompt, system=_EXPLAIN_SYSTEM, max_tokens=1000, label="findings_explanation",
        )
        if explanation is None:
            logger.debug(f"[FindingsExplanation] No parseable explanation for {instance_id}")
            return

        # Parse sample_text into structured rows for the UI
        sample_rows = self._parse_sample_text(sample_text)

        # Store structured evidence on every finding for this instance.
        # evidence holds the AI analysis + sample rows so the UI can render
        # them properly. resolution_notes is left for human notes only.
        evidence_payload = {
            "ai_explanation": {
                "root_cause":      explanation.get("root_cause", ""),
                "affected_scope":  explanation.get("affected_scope", ""),
                "fix_action":      explanation.get("fix_action", ""),
                "confidence":      explanation.get("confidence", ""),
            },
            "sample_rows": sample_rows,
        }

        for finding in instance_findings:
            finding_id = getattr(finding, "id", None)
            if finding_id:
                try:
                    # Merge with any existing evidence (e.g. rule execution metadata)
                    existing = getattr(finding, "evidence", None) or {}
                    existing.update(evidence_payload)
                    storage.update_finding_evidence(finding_id, existing)
                except Exception as e:
                    logger.debug(f"[FindingsExplanation] Could not update finding {finding_id}: {e}")

        logger.info(
            f"[FindingsExplanation] Explained instance {instance_id} "
            f"({finding_count} finding(s), scope={explanation.get('affected_scope', '?')}, "
            f"confidence={explanation.get('confidence', '?')})"
        )

    def _parse_sample_text(self, sample_text: str) -> list:
        """Convert the pipe-delimited sample text into a list of dicts for the UI."""
        if not sample_text or sample_text.startswith("("):
            return []
        lines = [l for l in sample_text.strip().splitlines() if l and not l.startswith("-")]
        if len(lines) < 2:
            return []
        headers = [h.strip() for h in lines[0].split("|")]
        rows = []
        for line in lines[1:]:
            values = [v.strip() for v in line.split("|")]
            rows.append(dict(zip(headers, values)))
        return rows

    def _fetch_violating_rows(
        self,
        rule_sql: str,
        table_asset: Any,
        target_col: str,
        source: Optional[Any] = None,
        finding_count: int = 0,
    ) -> str:
        """Run the rule's SQL to find rows that actually fail, then fetch
        those specific rows for concrete evidence.

        Runs against the run's own DataSource when one is passed in (Postgres
        or Snowflake); falls back to sf_session only for legacy call sites.
        Without this, Postgres runs would silently query Snowflake here and
        return "(no violating rows found)" or an error — findings would
        surface with ai_explanation but no sample_rows on the UI."""
        if not rule_sql:
            return "(no rule SQL available — python_handler check)"

        fqn = table_asset.fqn

        # Try to extract a WHERE predicate from the rule SQL — most of our
        # templates are SELECT COUNT(*) ... WHERE <predicate>, so the predicate
        # is useful for fetching the actual bad rows.
        where_match = re.search(r'\bWHERE\b(.+?)(?:\bGROUP\b|\bLIMIT\b|\bHAVING\b|$)',
                                rule_sql, re.IGNORECASE | re.DOTALL)
        predicate = where_match.group(1).strip() if where_match else None

        try:
            if predicate and target_col:
                cols = f"{target_col}, *" if target_col else "*"
                sample_sql = f"SELECT {cols} FROM {fqn} WHERE {predicate} LIMIT {_MAX_SAMPLE_ROWS}"
            elif predicate:
                sample_sql = f"SELECT * FROM {fqn} WHERE {predicate} LIMIT {_MAX_SAMPLE_ROWS}"
            else:
                # No extractable WHERE predicate — this is a table-level
                # aggregate check (e.g. freshness: MAX(col) < threshold).
                # There are no individual "failing rows"; the whole table fails
                # as a unit. Showing arbitrary rows is misleading, so skip.
                return "(no individual failing rows — table-level aggregate check)"

            querier = source if source is not None else sf_session
            rows = querier.query(sample_sql)
            if not rows:
                return "(no violating rows found)"

            headers = list(rows[0].keys())[:10]
            lines = [" | ".join(headers), "-" * 60]
            for row in rows:
                vals = [str(row.get(h, ""))[:25] for h in headers]
                lines.append(" | ".join(vals))
            return "\n".join(lines)
        except Exception as e:
            logger.debug(f"[FindingsExplanation] Could not fetch violating rows: {e}")
            return f"(could not fetch sample rows: {e})"
