"""
Enum types shared by pydantic schemas (app/schemas/*.py) and the storage
layer (app/services/storage.py). Values are plain strings in Snowflake
(no native enum type there) — these enums exist purely for FastAPI request/
response validation, not for any DB-level constraint. All values are
lowercase, matching the original SQLAlchemy model enums this replaces.
"""
import enum


class ScanStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ScanType(str, enum.Enum):
    METADATA = "metadata"
    SCHEMA = "schema"
    DATA_PROFILE = "data_profile"
    FULL = "full"


class FindingStatus(str, enum.Enum):
    DETECTED = "detected"
    VALIDATED = "validated"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    CLOSED = "closed"
    FALSE_POSITIVE = "false_positive"
    WONT_FIX = "wont_fix"


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
    PENDING  = "pending"
    ACTIVE   = "active"
    DISABLED = "disabled"
    REJECTED = "rejected"


class AgentRunStatus(str, enum.Enum):
    PENDING              = "pending"
    RUNNING              = "running"
    AWAITING_RULE_REVIEW = "awaiting_rule_review"
    AWAITING_FIXES       = "awaiting_fixes"
    COMPLETED            = "completed"
    FAILED               = "failed"


class AgentTaskStatus(str, enum.Enum):
    PENDING   = "pending"
    RUNNING   = "running"
    COMPLETED = "completed"
    FAILED    = "failed"
    SKIPPED   = "skipped"
