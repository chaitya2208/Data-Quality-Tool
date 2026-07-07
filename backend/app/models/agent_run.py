import uuid
import enum
from sqlalchemy import Column, String, DateTime, JSON, Integer, Text, ForeignKey, Enum
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.core.database import Base


class AgentRunStatus(str, enum.Enum):
    PENDING              = "pending"
    RUNNING              = "running"
    AWAITING_RULE_REVIEW = "awaiting_rule_review"  # paused for user to review/edit rules
    AWAITING_FIXES       = "awaiting_fixes"         # pipeline complete, developer fixing issues
    COMPLETED            = "completed"
    FAILED               = "failed"


class AgentTaskStatus(str, enum.Enum):
    PENDING   = "pending"
    RUNNING   = "running"
    COMPLETED = "completed"
    FAILED    = "failed"
    SKIPPED   = "skipped"


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id                    = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    database              = Column(String(255), nullable=False)
    schema_name           = Column(String(255), nullable=False)
    table                 = Column(String(255), nullable=False)
    status                = Column(Enum(AgentRunStatus), nullable=False, default=AgentRunStatus.PENDING, index=True)
    scan_id               = Column(String(36), ForeignKey("scans.id", ondelete="SET NULL"), nullable=True)
    started_at            = Column(DateTime(timezone=True), nullable=True)
    completed_at          = Column(DateTime(timezone=True), nullable=True)
    findings_count        = Column(Integer, default=0)
    ai_rules_count        = Column(Integer, default=0)   # AI-generated rules from Rule Intelligence
    rule_review_state     = Column(JSON, nullable=True)  # {active: [...], skipped: [...]} — user's review decisions
    error_message         = Column(String(1024), nullable=True)
    created_at            = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    tasks = relationship(
        "AgentTask", back_populates="run",
        cascade="all, delete-orphan",
        order_by="AgentTask.created_at",
    )


class AgentTask(Base):
    __tablename__ = "agent_tasks"

    id            = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id        = Column(String(36), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    agent_name    = Column(String(100), nullable=False)
    status        = Column(Enum(AgentTaskStatus), nullable=False, default=AgentTaskStatus.PENDING, index=True)
    started_at    = Column(DateTime(timezone=True), nullable=True)
    completed_at  = Column(DateTime(timezone=True), nullable=True)
    output        = Column(JSON, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at    = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    run = relationship("AgentRun", back_populates="tasks")
