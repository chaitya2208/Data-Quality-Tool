"""
AppSetting — a simple key/value store for tunable app preferences that must
affect backend behavior (profiling thresholds, auto-verify interval, etc.).

Values are stored as JSON so a setting can be a number, string, or bool. The
settings_service layer applies typed defaults, so an unset key transparently
uses the code default.
"""
from sqlalchemy import Column, String, JSON, DateTime
from sqlalchemy.sql import func
from app.core.database import Base


class AppSetting(Base):
    __tablename__ = "app_settings"

    key        = Column(String(128), primary_key=True)
    value      = Column(JSON, nullable=True)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), server_default=func.now(), nullable=False)

    def __repr__(self):
        return f"<AppSetting(key={self.key}, value={self.value})>"
