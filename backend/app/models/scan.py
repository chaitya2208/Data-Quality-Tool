from sqlalchemy import Column, String, DateTime, JSON, Integer, ForeignKey, Enum
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.core.database import Base
import uuid
import enum


class ScanStatus(str, enum.Enum):
    """Scan execution status"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ScanType(str, enum.Enum):
    """Type of scan being performed"""
    METADATA = "metadata"
    SCHEMA = "schema"
    DATA_PROFILE = "data_profile"
    FULL = "full"


class Scan(Base):
    """
    Represents a scan execution against an asset.
    Each scan checks rules and generates findings.
    """
    __tablename__ = "scans"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

    # What was scanned
    asset_id = Column(String(36), ForeignKey("assets.id", ondelete="CASCADE"), nullable=False, index=True)
    connection_id = Column(String(36), nullable=True, index=True)  # data source this scan targeted

    # Scan configuration
    scan_type = Column(Enum(ScanType), nullable=False, default=ScanType.METADATA)

    # Execution tracking
    status = Column(Enum(ScanStatus), nullable=False, default=ScanStatus.PENDING, index=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    # Results summary
    rules_checked = Column(Integer, default=0)
    findings_count = Column(Integer, default=0)

    # Error tracking
    error_message = Column(String(1024), nullable=True)

    # Scan metadata
    scan_config = Column(JSON, nullable=True)  # Configuration used for this scan
    scan_results = Column(JSON, nullable=True)  # Raw results from scan

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Relationships
    asset = relationship("Asset", back_populates="scans")
    findings = relationship("Finding", back_populates="scan", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Scan(id={self.id}, asset_id={self.asset_id}, status={self.status})>"
