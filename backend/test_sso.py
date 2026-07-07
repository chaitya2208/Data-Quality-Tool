"""
Quick test script for Snowflake SSO authentication.
This will open your browser for SSO login.
"""
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(__file__))

from app.services.snowflake_connector import SnowflakeConnector
from app.core.config import settings


def test_sso_connection():
    """Test SSO connection to Snowflake"""
    print("=" * 60)
    print("Testing Snowflake SSO Connection")
    print("=" * 60)
    print()
    print(f"Account: {settings.SNOWFLAKE_ACCOUNT}")
    print(f"User: {settings.SNOWFLAKE_USER}")
    print(f"Warehouse: {settings.SNOWFLAKE_WAREHOUSE}")
    print(f"Auth Method: {settings.SNOWFLAKE_AUTH_METHOD}")
    print()
    print("⚠️  Your browser will open for SSO authentication...")
    print("    Please log in when prompted.")
    print()

    try:
        print("Connecting to Snowflake...")
        with SnowflakeConnector() as sf:
            print("✓ Connection successful!")
            print()

            # Test basic query
            print("Testing basic query...")
            result = sf.execute_query("SELECT CURRENT_VERSION() as version, CURRENT_USER() as user, CURRENT_ROLE() as role")
            if result:
                print("✓ Query successful!")
                print(f"  Snowflake Version: {result[0].get('VERSION')}")
                print(f"  Current User: {result[0].get('USER')}")
                print(f"  Current Role: {result[0].get('ROLE')}")
                print()

            # List databases
            print("Listing databases...")
            databases = sf.list_databases()
            print(f"✓ Found {len(databases)} databases")

            # Show first 5 databases
            for i, db in enumerate(databases[:5]):
                db_name = db.get('name') or db.get('NAME')
                print(f"  {i+1}. {db_name}")

            if len(databases) > 5:
                print(f"  ... and {len(databases) - 5} more")
            print()

            print("=" * 60)
            print("✓ All tests passed!")
            print("=" * 60)
            print()
            print("You can now:")
            print("  1. Run full setup: python setup_db.py")
            print("  2. Start API: uvicorn app.main:app --reload")
            print("  3. Try scanning a table via API")
            print()
            return True

    except Exception as e:
        print()
        print("=" * 60)
        print("✗ Connection failed!")
        print("=" * 60)
        print()
        print(f"Error: {str(e)}")
        print()
        print("Troubleshooting:")
        print("  1. Check your .env file has correct values:")
        print(f"     SNOWFLAKE_ACCOUNT={settings.SNOWFLAKE_ACCOUNT}")
        print(f"     SNOWFLAKE_USER={settings.SNOWFLAKE_USER}")
        print(f"     SNOWFLAKE_WAREHOUSE={settings.SNOWFLAKE_WAREHOUSE}")
        print("  2. Make sure you can access Snowflake web UI")
        print("  3. Verify your SSO is working in browser")
        print("  4. Check if your account identifier is correct")
        print("     (Should be like: TT-CORE_INT or xy12345.us-east-1)")
        print()
        return False


if __name__ == "__main__":
    success = test_sso_connection()
    sys.exit(0 if success else 1)
