"""
Persistent recommendation cache service.

On first encounter of a rule_code + data_type pair:
  1. Call Claude with the full finding context
  2. Templatize the response (replace actual names with {{placeholders}})
  3. Store to DB

On subsequent encounters:
  1. Load template from DB (instant)
  2. Substitute placeholders with actual finding context
  3. Return immediately — no Claude call

Placeholder set: {{fqn}}, {{table_name}}, {{column_name}}, {{schema_name}}, {{database_name}}, {{data_type}}
"""
import json
import re
import logging
from typing import Optional
from sqlalchemy.orm import Session

from app.models.recommendation_cache import RecommendationCache

logger = logging.getLogger(__name__)

# The placeholders used in stored templates
PLACEHOLDERS = [
    "fqn",
    "table_name",
    "column_name",
    "schema_name",
    "database_name",
    "data_type",
]


def build_cache_key(rule_code: str, data_type: str) -> str:
    return f"{rule_code}::{(data_type or '').upper()}"


def get_cached(db: Session, cache_key: str, context: dict) -> Optional[dict]:
    """
    Look up cache key. If found, substitute placeholders with context values
    and increment hit_count. Returns dict with explanation/sql_query/confidence/impact,
    or None if no cache entry exists.
    """
    entry = db.query(RecommendationCache).filter(
        RecommendationCache.cache_key == cache_key
    ).first()

    if not entry:
        return None

    # Substitute placeholders
    explanation = _substitute(entry.explanation_template, context)
    sql_query = _substitute(entry.sql_template, context)

    # Track usage
    entry.hit_count = (entry.hit_count or 0) + 1
    db.commit()

    logger.info(f"[RecCache] Cache hit for {cache_key} (hit #{entry.hit_count})")
    return {
        "explanation": explanation,
        "sql_query": sql_query,
        "confidence": entry.confidence,
        "impact": entry.impact or "",
        "from_cache": True,
    }


def store(
    db: Session,
    cache_key: str,
    rule_code: str,
    data_type: str,
    context: dict,
    explanation: str,
    sql_query: str,
    confidence: int,
    impact: str,
) -> None:
    """
    Templatize Claude's response (replace actual names with {{placeholders}})
    and persist to DB. Safe to call even if the key already exists (no-op if duplicate).
    """
    # Check for existing entry first (another request may have stored it concurrently)
    existing = db.query(RecommendationCache).filter(
        RecommendationCache.cache_key == cache_key
    ).first()
    if existing:
        return

    explanation_tmpl = _templatize(explanation, context)
    sql_tmpl = _templatize(sql_query, context)

    entry = RecommendationCache(
        cache_key=cache_key,
        rule_code=rule_code,
        data_type=data_type or "",
        explanation_template=explanation_tmpl,
        sql_template=sql_tmpl,
        confidence=confidence,
        impact=impact,
        hit_count=0,
    )
    db.add(entry)
    try:
        db.commit()
        logger.info(f"[RecCache] Stored new cache entry for {cache_key}")
    except Exception as e:
        db.rollback()
        logger.warning(f"[RecCache] Failed to store cache entry for {cache_key}: {e}")


def _templatize(text: str, context: dict) -> str:
    """
    Replace actual context values with {{placeholder}} tokens in the text.
    Longer/more specific values are replaced first to avoid partial matches.
    """
    replacements = []
    for key in PLACEHOLDERS:
        value = context.get(key, "")
        if value and len(value) > 2:  # skip very short values to avoid false matches
            replacements.append((value, f"{{{{{key}}}}}"))

    # Sort by value length descending — replace longer strings first
    # so "MY_DB.MY_SCHEMA.MY_TABLE" is replaced before "MY_TABLE"
    replacements.sort(key=lambda r: len(r[0]), reverse=True)

    result = text
    for actual, placeholder in replacements:
        result = result.replace(actual, placeholder)
    return result


def _substitute(template: str, context: dict) -> str:
    """
    Replace {{placeholder}} tokens with actual context values.
    Unknown placeholders are left as-is.
    """
    result = template
    for key in PLACEHOLDERS:
        value = context.get(key, "")
        result = result.replace(f"{{{{{key}}}}}", value)
    return result
