"""
Connection type enum — extracted from the old SQLAlchemy model
(app/models/connection.py) so it survives the move to the storage layer
without dragging in ORM. Values are the lowercase strings stored in the
CONNECTIONS.TYPE column.
"""
import enum


class ConnectionType(str, enum.Enum):
    SNOWFLAKE = "snowflake"
    POSTGRES = "postgres"
    # MYSQL = "mysql"  # deferred
