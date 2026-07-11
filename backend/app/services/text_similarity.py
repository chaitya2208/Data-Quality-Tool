"""
Word-overlap similarity — the single source of truth for the fuzzy "is this
the same rule/concept?" check used by both the manual rule generator
(api/rules.py) and the Rule Intelligence Agent's definition-dedup path
(agents/rule_intelligence_agent.py).

This used to be copy-pasted in both places with independently-drifting
stopword lists and a hardcoded 0.55 threshold. Keeping one implementation
means a tweak to the stopword set or threshold can't silently make the two
dedup gates disagree (a concept the manual API rejects as duplicate that the
agent then happily recreates).

NOTE: word overlap is a deliberately simple, language/phrasing-sensitive
signal. It is good enough as a cheap backstop, but it is not semantic — an
embedding-based similarity would be more robust and is the intended
longer-term replacement.
"""
from __future__ import annotations

import re

# Default threshold both callers gate on. Two texts count as "the same
# concept" at/above this overlap ratio.
DEFAULT_SIMILARITY_THRESHOLD = 0.55

WORD_STOP = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being", "have",
    "has", "had", "do", "does", "did", "will", "would", "could", "should", "may",
    "might", "must", "shall", "can", "need", "dare", "ought", "used", "to", "of",
    "in", "for", "on", "with", "at", "by", "from", "as", "into", "through", "this",
    "that", "these", "those", "it", "its", "and", "or", "but", "if", "than", "when",
    "where", "which", "who", "how", "all", "each", "every", "both", "rule", "check",
    "column", "table", "snowflake", "data", "quality", "not", "no", "any",
}


def _significant_words(text: str) -> set[str]:
    return {w.lower() for w in re.findall(r"\w+", text) if w.lower() not in WORD_STOP and len(w) > 2}


def word_overlap_score(text1: str, text2: str) -> float:
    """Simple word-overlap similarity between two strings (0.0 – 1.0).
    Catches semantic-ish duplicates regardless of phrasing/language."""
    w1, w2 = _significant_words(text1), _significant_words(text2)
    if not w1 or not w2:
        return 0.0
    return len(w1 & w2) / min(len(w1), len(w2))
