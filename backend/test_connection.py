"""
Quick test script to verify Snowflake connection and basic functionality.
Run this after setup to ensure everything is configured correctly.
"""
from app.services.snowflake_connector import SnowflakeConnector
from app.core.config import settings
import sys


def test_snowflake_connection():
    """Test Snowflake connection and basic queries"""
    print("=" * 60)
    print("Testing Snowflake Connection")
    print("=" * 60)

    print(f"\nAccount: {settings.SNOWFLAKE_ACCOUNT}")
    print(f"User: {settings.SNOWFLAKE_USER}")
    print(f"Warehouse: {settings.SNOWFLAKE_WAREHOUSE}")

    try:
        print("\n1. Connecting to Snowflake...")
        with SnowflakeConnector() as sf:
            print("   ✓ Connection successful!")

            print("\n2. Testing basic query...")
            result = sf.execute_query("SELECT CURRENT_VERSION()")
            print(f"   ✓ Snowflake version: {result[0]['CURRENT_VERSION()']}")

            print("\n3. Listing databases...")
            databases = sf.list_databases()
            print(f"   ✓ Found {len(databases)} databases")
            for db in databases[:5]:  # Show first 5
                print(f"      - {db['name']}")
            if len(databases) > 5:
                print(f"      ... and {len(databases) - 5} more")

            if settings.SNOWFLAKE_DATABASE:
                print(f"\n4. Listing schemas in {settings.SNOWFLAKE_DATABASE}...")
                try:
                    schemas = sf.list_schemas(settings.SNOWFLAKE_DATABASE)
                    print(f"   ✓ Found {len(schemas)} schemas")
                    for schema in schemas[:5]:
                        print(f"      - {schema['name']}")
                    if len(schemas) > 5:
                        print(f"      ... and {len(schemas) - 5} more")
                except Exception as e:
                    print(f"   ⚠ Could not list schemas: {str(e)}")

                if settings.SNOWFLAKE_SCHEMA:
                    print(f"\n5. Listing tables in {settings.SNOWFLAKE_DATABASE}.{settings.SNOWFLAKE_SCHEMA}...")
                    try:
                        tables = sf.list_tables(settings.SNOWFLAKE_DATABASE, settings.SNOWFLAKE_SCHEMA)
                        print(f"   ✓ Found {len(tables)} tables")
                        for table in tables[:5]:
                            print(f"      - {table['name']}")
                        if len(tables) > 5:
                            print(f"      ... and {len(tables) - 5} more")
                    except Exception as e:
                        print(f"   ⚠ Could not list tables: {str(e)}")

        print("\n" + "=" * 60)
        print("✓ All tests passed!")
        print("=" * 60)
        return True

    except Exception as e:
        print("\n" + "=" * 60)
        print("✗ Test failed!")
        print("=" * 60)
        print(f"\nError: {str(e)}")
        print("\nPlease check:")
        print("  1. Your .env file has correct Snowflake credentials")
        print("  2. Your network can reach Snowflake")
        print("  3. Your user has necessary permissions")
        return False


def test_database_connection():
    """Test PostgreSQL connection"""
    print("\n" + "=" * 60)
    print("Testing Database Connection")
    print("=" * 60)

    try:
        from app.core.database import engine
        from sqlalchemy import text

        print(f"\nDatabase URL: {settings.DATABASE_URL.split('@')[1]}")  # Hide password

        with engine.connect() as conn:
            result = conn.execute(text("SELECT version()"))
            version = result.fetchone()[0]
            print(f"\n✓ PostgreSQL connection successful!")
            print(f"  Version: {version.split(',')[0]}")

        # Check tables
        from app.core.database import Base
        print("\n✓ Checking tables...")
        tables = Base.metadata.tables.keys()
        print(f"  Found {len(tables)} tables: {', '.join(tables)}")

        return True

    except Exception as e:
        print(f"\n✗ Database connection failed: {str(e)}")
        print("\nPlease check:")
        print("  1. PostgreSQL is running (docker-compose up -d)")
        print("  2. DATABASE_URL in .env is correct")
        print("  3. You ran 'python setup_db.py' to create tables")
        return False


def main():
    """Run all tests"""
    print("\n" + "=" * 60)
    print("DATA QUALITY PLATFORM - CONNECTION TESTS")
    print("=" * 60)

    # Test database first
    db_ok = test_database_connection()

    # Test Snowflake
    sf_ok = test_snowflake_connection()

    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)
    print(f"Database:  {'✓ PASS' if db_ok else '✗ FAIL'}")
    print(f"Snowflake: {'✓ PASS' if sf_ok else '✗ FAIL'}")
    print("=" * 60)

    if db_ok and sf_ok:
        print("\n✓ All systems operational! You can now start the API server:")
        print("  uvicorn app.main:app --reload")
        sys.exit(0)
    else:
        print("\n⚠ Some tests failed. Please fix the issues above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
