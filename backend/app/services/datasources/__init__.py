"""Data-source abstraction: dialect-agnostic access to Snowflake, Postgres, etc."""
from app.services.datasources.base import DataSource
from app.services.datasources.registry import get_source, clear_cached_source

__all__ = ["DataSource", "get_source", "clear_cached_source"]
