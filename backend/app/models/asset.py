from sqlalchemy import Column, String, DateTime, JSON, Text, Integer
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.core.database import Base
import uuid


class Asset(Base):
    """
    Represents a Snowflake asset (database, schema, table, or column).
    This is the entity being monitored for quality issues.
    """
    __tablename__ = "assets"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

    # Asset identification
    asset_type = Column(String(50), nullable=False)  # database, schema, table, column
    database_name = Column(String(255), nullable=False, index=True)
    schema_name = Column(String(255), nullable=True, index=True)
    table_name = Column(String(255), nullable=True, index=True)
    column_name = Column(String(255), nullable=True)

    # Fully qualified name for easy lookup
    fqn = Column(String(1024), nullable=False, unique=True, index=True)

    # Metadata from Snowflake
    owner = Column(String(255), nullable=True)
    comment = Column(Text, nullable=True)
    row_count = Column(Integer, nullable=True)
    size_bytes = Column(Integer, nullable=True)

    # Raw metadata JSON from Snowflake
    raw_metadata = Column(JSON, nullable=True)

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), server_default=func.now(), nullable=False)
    last_scanned_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    scans = relationship("Scan", back_populates="asset", cascade="all, delete-orphan")
    findings = relationship("Finding", back_populates="asset", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Asset(id={self.id}, fqn={self.fqn}, type={self.asset_type})>"
