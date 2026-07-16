"""One-off: describe columns of the 3 PLAYGROUND_DB.SILVER tables to pick a scan target."""
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

from app.services.snowflake_session import session as sf_session

TABLES = [
    "REPLAY_SILVER_MINUTE_RECAP_TBL",
    "REPLAY_SILVER_PARSED_TBL",
    "REPLAY_SILVER_TRANSFORMED_TBL",
]


def main() -> int:
    for t in TABLES:
        print(f"\n=== PLAYGROUND_DB.SILVER.{t} ===")
        rows = sf_session.query(f"DESCRIBE TABLE PLAYGROUND_DB.SILVER.{t}")
        for r in rows:
            name = r.get("name") or r.get("NAME")
            dtype = r.get("type") or r.get("TYPE")
            nullable = r.get("null?") or r.get("NULL?")
            print(f"  {name:30s} {dtype:25s} nullable={nullable}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
