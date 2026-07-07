from sqlalchemy import Column, String, DateTime, JSON, Text, Boolean, Enum, Integer
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.core.database import Base
import uuid
import enum


class RuleSeverity(str, enum.Enum):
    CRITICAL = "critical"
    HIGH     = "high"
    MEDIUM   = "medium"
    LOW      = "low"
    INFO     = "info"


class RuleCategory(str, enum.Enum):
    NAMING        = "naming"
    DOCUMENTATION = "documentation"
    OWNERSHIP     = "ownership"
    SCHEMA        = "schema"
    DATA_QUALITY  = "data_quality"
    SECURITY      = "security"
    PERFORMANCE   = "performance"


class RuleStatus(str, enum.Enum):
    PENDING  = "pending"   # submitted, awaiting approval
    ACTIVE   = "active"    # approved and running on scans
    DISABLED = "disabled"  # was active, manually turned off
    REJECTED = "rejected"  # reviewed and rejected


class Rule(Base):
    __tablename__ = "rules"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

    # Identification
    code        = Column(String(50),  nullable=False, unique=True, index=True)
    name        = Column(String(255), nullable=False)
    description = Column(Text,        nullable=False)

    # Classification
    category  = Column(Enum(RuleCategory), nullable=False, index=True)
    severity  = Column(Enum(RuleSeverity), nullable=False, index=True)
    applies_to = Column(JSON, nullable=False)   # ["table", "column"]
    rule_config = Column(JSON, nullable=True)

    # Approval workflow
    status      = Column(Enum(RuleStatus), nullable=False,
                         default=RuleStatus.ACTIVE, index=True)
    jira_ticket = Column(String(100), nullable=True)   # e.g. "DQ-123"
    rejection_reason = Column(Text, nullable=True)

    # Ownership & audit
    owner      = Column(String(255), nullable=False)   # required — team or person
    created_by = Column(String(255), nullable=True)    # who submitted the rule
    version    = Column(Integer,     default=1, nullable=False)

    # is_active kept for backward-compat with scan engine
    # True when status == ACTIVE, False otherwise
    is_active  = Column(Boolean, default=True, nullable=False, index=True)

    # Timestamps
    created_at  = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at  = Column(DateTime(timezone=True), onupdate=func.now(),
                         server_default=func.now(), nullable=False)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    rejected_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    findings = relationship("Finding", back_populates="rule")

    def __repr__(self):
        return f"<Rule(code={self.code}, status={self.status}, owner={self.owner})>"
