import snowflake.connector
from snowflake.connector import DictCursor
from typing import List, Dict, Any, Optional
from app.core.config import settings
import logging
import threading

logger = logging.getLogger(__name__)


class SnowflakeConnectionPool:
    """
    Singleton connection pool to reuse Snowflake connections.
    Avoids multiple SSO prompts by reusing authenticated connections.
    """
    _instance = None
    _lock = threading.Lock()
    _connection = None

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def get_connection(self):
        """Get or create a reusable Snowflake connection"""
        if self._connection is None or not self._is_connection_alive():
            logger.info("Creating new Snowflake connection...")
            self._connection = self._create_connection()
        return self._connection

    def _create_connection(self):
        """Create a new Snowflake connection"""
        conn_params = {
            "account": settings.SNOWFLAKE_ACCOUNT,
            "user": settings.SNOWFLAKE_USER,
            "warehouse": settings.SNOWFLAKE_WAREHOUSE,
        }

        if settings.SNOWFLAKE_DATABASE:
            conn_params["database"] = settings.SNOWFLAKE_DATABASE
        if settings.SNOWFLAKE_SCHEMA:
            conn_params["schema"] = settings.SNOWFLAKE_SCHEMA
        if settings.SNOWFLAKE_ROLE:
            conn_params["role"] = settings.SNOWFLAKE_ROLE

        auth_method = getattr(settings, 'SNOWFLAKE_AUTH_METHOD', 'externalbrowser')

        if auth_method.lower() == 'externalbrowser':
            conn_params["authenticator"] = "externalbrowser"
            logger.info("Using SSO authentication with external browser...")
        elif auth_method.lower() == 'password':
            if not settings.SNOWFLAKE_PASSWORD:
                raise ValueError("Password required for password authentication")
            conn_params["password"] = settings.SNOWFLAKE_PASSWORD
            logger.info("Using username/password authentication...")
        else:
            raise ValueError(f"Unsupported authentication method: {auth_method}")

        return snowflake.connector.connect(**conn_params, insecure_mode=True)

    def _is_connection_alive(self):
        """Check if connection is still alive"""
        if self._connection is None:
            return False
        try:
            cursor = self._connection.cursor()
            cursor.execute("SELECT 1")
            cursor.close()
            return True
        except Exception:
            return False

    def create_execution_connection(self, role: str, warehouse: str):
        """
        Create a fresh short-lived connection for SQL execution with a
        specific role and warehouse. This is separate from the pool
        connection so USE ROLE / USE WAREHOUSE never mutate the shared
        session that all read queries depend on.
        The caller is responsible for closing it.
        """
        conn_params = {
            "account": settings.SNOWFLAKE_ACCOUNT,
            "user": settings.SNOWFLAKE_USER,
            "warehouse": warehouse,
            "role": role,
        }
        if settings.SNOWFLAKE_DATABASE:
            conn_params["database"] = settings.SNOWFLAKE_DATABASE

        auth_method = getattr(settings, 'SNOWFLAKE_AUTH_METHOD', 'externalbrowser')
        if auth_method.lower() == 'externalbrowser':
            # Reuse the existing token from the already-authenticated pool
            # connection so no new browser prompt is triggered.
            conn_params["authenticator"] = "externalbrowser"
        else:
            conn_params["password"] = settings.SNOWFLAKE_PASSWORD

        return snowflake.connector.connect(**conn_params, insecure_mode=True)

    def close(self):
        """Close the pool connection"""
        if self._connection:
            self._connection.close()
            self._connection = None


class SnowflakeConnector:
    """
    Abstraction layer for Snowflake connections.
    Uses connection pooling to avoid multiple SSO prompts.
    """

    def __init__(self):
        self.pool = SnowflakeConnectionPool()
        self.connection = None

    def connect(self) -> None:
        """Get connection from pool (reuses existing authenticated connection)"""
        try:
            self.connection = self.pool.get_connection()
            logger.info("Using pooled Snowflake connection")
        except Exception as e:
            logger.error(f"Failed to get Snowflake connection: {str(e)}")
            raise

    def disconnect(self) -> None:
        """Don't close connection - keep it in pool for reuse"""
        # Connection stays in pool for reuse
        pass

    def __enter__(self):
        """Context manager entry"""
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit"""
        self.disconnect()

    def execute_query(self, query: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """Execute a query and return results as list of dicts"""
        if not self.connection:
            raise RuntimeError("Not connected to Snowflake")

        try:
            cursor = self.connection.cursor(DictCursor)
            cursor.execute(query, params or {})
            results = cursor.fetchall()
            cursor.close()
            return results
        except Exception as e:
            logger.error(f"Query execution failed: {str(e)}")
            raise

    def list_databases(self) -> List[Dict[str, Any]]:
        """List all databases"""
        query = "SHOW DATABASES"
        return self.execute_query(query)

    def list_schemas(self, database: str) -> List[Dict[str, Any]]:
        """List all schemas in a database"""
        query = f"SHOW SCHEMAS IN DATABASE {database}"
        return self.execute_query(query)

    def list_tables(self, database: str, schema: str) -> List[Dict[str, Any]]:
        """List all tables in a schema"""
        query = f"SHOW TABLES IN {database}.{schema}"
        return self.execute_query(query)

    def get_table_metadata(self, database: str, schema: str, table: str) -> Dict[str, Any]:
        """
        Get detailed metadata for a specific table.
        Returns information about the table including row count, size, owner, etc.
        """
        query = f"""
        SELECT
            table_catalog as database_name,
            table_schema as schema_name,
            table_name,
            table_owner as owner,
            comment,
            row_count,
            bytes as size_bytes,
            created as created_at,
            last_altered as last_altered_at
        FROM {database}.INFORMATION_SCHEMA.TABLES
        WHERE table_schema = '{schema}'
        AND table_name = '{table}'
        """
        results = self.execute_query(query)
        return results[0] if results else {}

    def get_table_columns(self, database: str, schema: str, table: str) -> List[Dict[str, Any]]:
        """Get column information for a table"""
        query = f"""
        SELECT
            column_name,
            ordinal_position,
            data_type,
            is_nullable,
            column_default,
            comment
        FROM {database}.INFORMATION_SCHEMA.COLUMNS
        WHERE table_schema = '{schema}'
        AND table_name = '{table}'
        ORDER BY ordinal_position
        """
        return self.execute_query(query)

    def get_table_primary_keys(self, database: str, schema: str, table: str) -> List[Dict[str, Any]]:
        """Get primary key constraints for a table"""
        query = f"""
        SHOW PRIMARY KEYS IN {database}.{schema}.{table}
        """
        try:
            return self.execute_query(query)
        except Exception as e:
            logger.warning(f"Could not retrieve primary keys for {database}.{schema}.{table}: {str(e)}")
            return []

    def get_table_foreign_keys(self, database: str, schema: str, table: str) -> List[Dict[str, Any]]:
        """Get foreign key constraints for a table"""
        query = f"""
        SHOW IMPORTED KEYS IN {database}.{schema}.{table}
        """
        try:
            return self.execute_query(query)
        except Exception as e:
            logger.warning(f"Could not retrieve foreign keys for {database}.{schema}.{table}: {str(e)}")
            return []

    def check_connection(self) -> bool:
        """Test if connection is alive"""
        try:
            self.execute_query("SELECT 1")
            return True
        except Exception:
            return False
