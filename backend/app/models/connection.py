"""
Connection — a saved data-source the tool can profile/scan/fix against.

Decouples the app from a single hardcoded Snowflake account: each Connection
row describes one source (Snowflake, Postgres/RDS, …) and is resolved at
request time into a live DataSource adapter (see services/datasources/).

Credentials are stored in the app's own SQLite metadata DB. For now the secret
is stored as-is (plaintext) — DB-at-rest encryption / a secrets manager is a
documented hardening follow-up, deliberately out of scope for this pass.
"""
import uuid
import enum
from sqlalchemy import Column, String, Integer, Boolean, DateTime, JSON, Text, Enum
from sqlalchemy.sql import func
from app.core.database import Base


class ConnectionType(str, enum.Enum):
    SNOWFLAKE = "snowflake"
    POSTGRES  = "postgres"
    # MYSQL   = "mysql"   # deferred


class Connection(Base):
    __tablename__ = "connections"

    id          = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name        = Column(String(255), nullable=False)          # user-facing label
    type        = Column(Enum(ConnectionType), nullable=False, index=True)

    # Location — meaning varies slightly per type:
    #   snowflake: host = account identifier; warehouse/role in `extra`
    #   postgres:  host + port + database
    host        = Column(String(512), nullable=True)
    port        = Column(Integer, nullable=True)
    database    = Column(String(255), nullable=True)
    schema_     = Column("schema", String(255), nullable=True)  # 'schema' is reserved on the class

    username    = Column(String(255), nullable=True)
    secret      = Column(Text, nullable=True)                   # password / token (plaintext for now)
    auth_method = Column(String(50), nullable=True)             # snowflake: externalbrowser|password

    # Per-type extras (snowflake warehouse/role, postgres sslmode, etc.)
    extra       = Column(JSON, nullable=True)

    is_active   = Column(Boolean, default=True)
    created_at  = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at  = Column(DateTime(timezone=True), onupdate=func.now(), server_default=func.now(), nullable=False)

    def __repr__(self):
        return f"<Connection(id={self.id}, name={self.name}, type={self.type})>"
