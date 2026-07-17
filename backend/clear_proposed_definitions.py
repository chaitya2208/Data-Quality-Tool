"""
One-shot cleanup: delete every RULE_DEFINITIONS row with status='proposed'
(and their RULE_INSTANCES), plus their FINDINGS/RULE_EXECUTIONS.

Context: an extended RuleIntelligence test round (regression + adversarial +
real-data scans against test tables) left ~19 unreviewed 'proposed'
definitions behind, several of them exact/near duplicates of the same
concept (a bug fixed the same session in rule_intelligence_agent.py /
coordinator.py — see _find_similar_definition exact-name check and the
new_definition_key same-run collapsing). None of these were ever reviewed
or approved, so they're safe to wipe — the next real scan will re-propose
whichever ones still apply, now deduped correctly.

Leaves ACTIVE/DISABLED/REJECTED definitions untouched.

Safe to re-run (no-op if nothing is 'proposed').
"""
import logging
import sys

from app.services.snowflake_session import session as sf_session

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> int:
    rows = sf_session.query(
        "SELECT ID, NAME FROM RULE_DEFINITIONS WHERE STATUS = 'proposed'"
    )
    if not rows:
        logger.info("No 'proposed' definitions found — nothing to do.")
        return 0

    ids = [r["ID"] for r in rows]
    logger.info(f"Found {len(ids)} 'proposed' definitions:")
    for r in rows:
        logger.info(f"  - {r['NAME']}")

    id_params = {f"i{i}": _id for i, _id in enumerate(ids)}
    id_placeholders = ", ".join([f"%(i{i})s" for i in range(len(ids))])

    inst_rows = sf_session.query(
        f"SELECT ID FROM RULE_INSTANCES WHERE DEFINITION_ID IN ({id_placeholders})",
        id_params,
    )
    inst_ids = [r["ID"] for r in inst_rows]
    logger.info(f"Deleting {len(inst_ids)} RULE_INSTANCES tied to these definitions")

    if inst_ids:
        inst_params = {f"n{i}": _id for i, _id in enumerate(inst_ids)}
        inst_placeholders = ", ".join([f"%(n{i})s" for i in range(len(inst_ids))])
        sf_session.execute(
            f"DELETE FROM RULE_EXECUTIONS WHERE INSTANCE_ID IN ({inst_placeholders})",
            inst_params,
        )
        sf_session.execute(
            f"DELETE FROM FINDINGS WHERE INSTANCE_ID IN ({inst_placeholders})",
            inst_params,
        )
        sf_session.execute(
            f"DELETE FROM RULE_INSTANCES WHERE ID IN ({inst_placeholders})",
            inst_params,
        )

    sf_session.execute(
        f"DELETE FROM RULE_DEFINITIONS WHERE ID IN ({id_placeholders})",
        id_params,
    )
    logger.info(f"Deleted {len(ids)} RULE_DEFINITIONS")

    remaining = sf_session.query("SELECT COUNT(*) AS N FROM RULE_DEFINITIONS")
    logger.info(f"RULE_DEFINITIONS remaining: {remaining[0].get('N')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
