from sqlalchemy import Column, String, DateTime, JSON, Text, ForeignKey, Enum
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.core.database import Base
import uuid
import enum


class FindingStatus(str, enum.Enum):
    """Finding lifecycle states"""
    DETECTED = "detected"          # Just discovered
    VALIDATED = "validated"        # Confirmed as real issue
    ASSIGNED = "assigned"          # Assigned to someone
    IN_PROGRESS = "in_progress"    # Being worked on
    RESOLVED = "resolved"          # Fixed
    CLOSED = "closed"              # Closed/archived
    FALSE_POSITIVE = "false_positive"  # Not actually an issue
    WONT_FIX = "wont_fix"         # Acknowledged but won't fix


class Finding(Base):
    """
    Represents a data quality issue found during a scan.
    This is the central entity in the finding-centric architecture.
    """
    __tablename__ = "findings"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

    # What was found
    asset_id = Column(String(36), ForeignKey("assets.id", ondelete="CASCADE"), nullable=False, index=True)
    scan_id = Column(String(36), ForeignKey("scans.id", ondelete="CASCADE"), nullable=False, index=True)
    rule_id = Column(String(36), ForeignKey("rules.id", ondelete="SET NULL"), nullable=True, index=True)

    # Finding details
    title = Column(String(512), nullable=False)
    description = Column(Text, nullable=False)

    # Status and severity (inherited from rule but can be overridden)
    status = Column(Enum(FindingStatus), nullable=False, default=FindingStatus.DETECTED, index=True)
    severity = Column(String(20), nullable=False, index=True)  # critical, high, medium, low, info

    # Context and evidence
    context = Column(JSON, nullable=True)  # Additional context about the finding
    evidence = Column(JSON, nullable=True)  # Evidence/data that triggered the finding

    # Assignment and resolution
    assigned_to = Column(String(255), nullable=True)
    resolution_notes = Column(Text, nullable=True)

    # Timestamps
    detected_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    validated_at = Column(DateTime(timezone=True), nullable=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), server_default=func.now(), nullable=False)

    # Relationships
    asset = relationship("Asset", back_populates="findings")
    scan = relationship("Scan", back_populates="findings")
    rule = relationship("Rule", back_populates="findings")

    def __repr__(self):
        return f"<Finding(id={self.id}, title={self.title}, status={self.status}, severity={self.severity})>"
