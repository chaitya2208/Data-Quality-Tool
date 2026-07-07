import uuid
from sqlalchemy import Column, String, Integer, Text, DateTime
from sqlalchemy.sql import func
from app.core.database import Base


class RecommendationCache(Base):
    """
    Persistent cache of Claude-generated fix recommendations keyed by rule pattern.
    Cache key: "{rule_code}::{data_type}" (e.g. "NULLABLE_ID_COLUMN::NUMBER")

    SQL and explanation are stored as templates with placeholders:
      {{fqn}}         → fully qualified table name
      {{table_name}}  → bare table name
      {{column_name}} → column name
      {{data_type}}   → column data type
      {{schema_name}} → schema name
      {{database_name}} → database name

    On cache hit, placeholders are substituted with actual finding context,
    giving a contextual response without a new Claude call.
    """
    __tablename__ = "recommendation_cache"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    cache_key = Column(String(255), nullable=False, unique=True, index=True)
    rule_code = Column(String(100), nullable=False, index=True)
    data_type = Column(String(100), nullable=False, default="")
    explanation_template = Column(Text, nullable=False)
    sql_template = Column(Text, nullable=False)
    confidence = Column(Integer, default=75)
    impact = Column(Text, nullable=True)
    hit_count = Column(Integer, default=0)  # how many times this cache entry has been reused
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
