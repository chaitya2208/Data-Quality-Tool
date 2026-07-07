"""
Demo data generator for testing without Snowflake.
Creates sample assets and findings for development/testing.
"""
from app.core.database import SessionLocal
from app.models import Asset, Scan, Finding, Rule
from app.models.scan import ScanStatus, ScanType
from app.models.finding import FindingStatus
from app.models.rule import RuleSeverity, RuleCategory
from app.services.rule_engine import initialize_default_rules
from datetime import datetime, timedelta
import random


def create_demo_data():
    """Create demo assets, scans, and findings"""
    db = SessionLocal()

    try:
        print("Creating demo data...\n")

        # Initialize rules first
        print("1. Initializing rules...")
        initialize_default_rules(db)
        rules = db.query(Rule).all()
        print(f"   ✓ Created {len(rules)} rules")

        # Sample data
        databases = ["PRODUCTION", "STAGING", "ANALYTICS"]
        schemas = ["PUBLIC", "RAW", "TRANSFORMED", "MART"]
        tables = [
            "users", "orders", "products", "transactions",
            "customers", "inventory", "shipments", "returns",
            "payments", "reviews"
        ]

        # Create sample assets
        print("\n2. Creating sample assets...")
        created_assets = []

        for db_name in databases[:2]:  # Use 2 databases
            for schema_name in schemas[:2]:  # Use 2 schemas per database
                for table_name in random.sample(tables, 3):  # 3 random tables per schema
                    # Create table asset
                    fqn = f"{db_name}.{schema_name}.{table_name}"

                    # Randomly decide if table has comment/owner (to create findings)
                    has_comment = random.random() > 0.4  # 60% missing
                    has_owner = random.random() > 0.3    # 70% missing

                    asset = Asset(
                        fqn=fqn,
                        asset_type="table",
                        database_name=db_name,
                        schema_name=schema_name,
                        table_name=table_name,
                        owner="data_team" if has_owner else None,
                        comment=f"Table storing {table_name} data" if has_comment else None,
                        row_count=random.randint(1000, 1000000),
                        size_bytes=random.randint(1024 * 1024, 1024 * 1024 * 1024),
                        metadata={
                            "created_at": str(datetime.utcnow() - timedelta(days=random.randint(30, 365))),
                        }
                    )
                    db.add(asset)
                    db.flush()
                    created_assets.append(asset)

                    # Create 3 sample columns per table
                    for col_name in ["id", "name", "created_at"]:
                        has_col_comment = random.random() > 0.6  # 40% missing

                        col_asset = Asset(
                            fqn=f"{fqn}.{col_name}",
                            asset_type="column",
                            database_name=db_name,
                            schema_name=schema_name,
                            table_name=table_name,
                            column_name=col_name,
                            comment=f"{col_name} column" if has_col_comment else None,
                            metadata={
                                "data_type": "VARCHAR" if col_name == "name" else "NUMBER",
                                "is_nullable": "YES",
                            }
                        )
                        db.add(col_asset)
                        db.flush()
                        created_assets.append(col_asset)

        db.commit()
        print(f"   ✓ Created {len(created_assets)} assets")

        # Create scans and findings
        print("\n3. Creating scans and findings...")
        table_assets = [a for a in created_assets if a.asset_type == "table"]
        total_findings = 0

        for asset in table_assets:
            # Create a scan for this asset
            scan = Scan(
                asset_id=asset.id,
                scan_type=ScanType.METADATA,
                status=ScanStatus.COMPLETED,
                started_at=datetime.utcnow() - timedelta(minutes=random.randint(10, 120)),
                completed_at=datetime.utcnow() - timedelta(minutes=random.randint(1, 10)),
                rules_checked=len(rules),
            )
            db.add(scan)
            db.flush()

            # Create findings for missing comment
            if not asset.comment:
                finding = Finding(
                    asset_id=asset.id,
                    scan_id=scan.id,
                    rule_id=rules[0].id,  # MISSING_TABLE_COMMENT
                    title=f"Table {asset.table_name} is missing a comment",
                    description=f"The table {asset.fqn} does not have a description/comment. "
                               f"All tables should be documented with meaningful comments.",
                    severity=RuleSeverity.MEDIUM.value,
                    status=random.choice([
                        FindingStatus.DETECTED,
                        FindingStatus.VALIDATED,
                        FindingStatus.ASSIGNED,
                    ]),
                    context={"table_name": asset.table_name, "fqn": asset.fqn},
                    evidence={"current_comment": None},
                    detected_at=scan.completed_at,
                )
                db.add(finding)
                total_findings += 1

            # Create findings for missing owner
            if not asset.owner:
                finding = Finding(
                    asset_id=asset.id,
                    scan_id=scan.id,
                    rule_id=rules[1].id,  # MISSING_TABLE_OWNER
                    title=f"Table {asset.table_name} is missing an owner",
                    description=f"The table {asset.fqn} does not have an assigned owner. "
                               f"All tables should have a designated owner for accountability.",
                    severity=RuleSeverity.HIGH.value,
                    status=random.choice([
                        FindingStatus.DETECTED,
                        FindingStatus.VALIDATED,
                    ]),
                    context={"table_name": asset.table_name, "fqn": asset.fqn},
                    evidence={"current_owner": None},
                    detected_at=scan.completed_at,
                )
                db.add(finding)
                total_findings += 1

            # Update scan findings count
            scan.findings_count = total_findings
            asset.last_scanned_at = scan.completed_at

        db.commit()
        print(f"   ✓ Created {len(table_assets)} scans")
        print(f"   ✓ Created {total_findings} findings")

        # Print summary
        print("\n" + "=" * 60)
        print("Demo Data Summary")
        print("=" * 60)
        print(f"Databases: {len(databases)}")
        print(f"Schemas: {len(databases) * 2}")
        print(f"Tables: {len(table_assets)}")
        print(f"Columns: {len([a for a in created_assets if a.asset_type == 'column'])}")
        print(f"Scans: {len(table_assets)}")
        print(f"Findings: {total_findings}")
        print("=" * 60)

        print("\n✓ Demo data created successfully!")
        print("\nYou can now:")
        print("  1. Start the API: uvicorn app.main:app --reload")
        print("  2. Visit: http://localhost:8000/api/v1/docs")
        print("  3. Try these endpoints:")
        print("     - GET /api/v1/assets")
        print("     - GET /api/v1/findings")
        print("     - GET /api/v1/findings/stats/summary")
        print("     - GET /api/v1/scans")

    except Exception as e:
        print(f"\n✗ Error creating demo data: {str(e)}")
        db.rollback()
        raise
    finally:
        db.close()


def clear_demo_data():
    """Clear all demo data from database"""
    db = SessionLocal()

    try:
        print("Clearing demo data...")
        db.query(Finding).delete()
        db.query(Scan).delete()
        db.query(Asset).delete()
        db.query(Rule).delete()
        db.commit()
        print("✓ Demo data cleared")
    except Exception as e:
        print(f"✗ Error clearing data: {str(e)}")
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "clear":
        clear_demo_data()
    else:
        create_demo_data()
