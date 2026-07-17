"""
One-off: list tables in PLAYGROUND_DB.SILVER with row counts, so we can pick
a real-data target for the RuleIntelligence regression/adversarial test round.
"""
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

from app.services.snowflake_session import session as sf_session


def main() -> int:
    rows = sf_session.query("SHOW TABLES IN SCHEMA PLAYGROUND_DB.SILVER")
    names = [r.get("name") or r.get("NAME") for r in rows]
    if not names:
        print("No tables found in PLAYGROUND_DB.SILVER")
        return 1

    print(f"Found {len(names)} tables in PLAYGROUND_DB.SILVER:\n")
    for n in names:
        try:
            cnt = sf_session.query(f"SELECT COUNT(*) AS N FROM PLAYGROUND_DB.SILVER.{n}")
            print(f"  {n:40s} rows={cnt[0].get('N')}")
        except Exception as e:
            print(f"  {n:40s} (count failed: {e})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
