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
    OPEN = "open"
    REOPENED = "reopened"
    RESOLVED = "resolved"
    # Future: FALSE_POSITIVE = "false_positive"
    #
    # A finding should be marked false_positive when the rule logic is correct
    # but the specific instance is intentionally allowed (e.g. a table that
    # deliberately has no owner because it's a temp scratch space). Unlike
    # RESOLVED (which the scan engine can auto-set when the data cleans up),
    # FALSE_POSITIVE would be a human-set terminal state that the lifecycle
    # machine skips on rescan — the rule keeps executing and logging executions,
    # but no new incident is opened or reopened for that (instance, asset) pair.
    # Implement by: (1) adding FALSE_POSITIVE here, (2) adding it to
    # _LIFECYCLE_RESOLVED in storage.py so find_recently_resolved_finding
    # does NOT reopen it, (3) treating it as "closed" in all open-status SQL
    # sets, and (4) adding a PATCH endpoint or UI action to set it.


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
    FRESHNESS     = "freshness"
    ANOMALY       = "anomaly"


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
