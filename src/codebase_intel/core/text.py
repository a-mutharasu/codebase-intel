"""Shared lightweight text utilities — term extraction for relevance scoring.

Hoisted out of intentgraph/bootstrap.py so the eval harness (analytics/eval.py)
can build queries with the exact same tokenization the scorer uses, instead of
duplicating the stopword list and regex.
"""

from __future__ import annotations

import re

_WORD_RE = re.compile(r"[a-z0-9_]+")

# Terms too generic to signal relevance on their own
STOPWORDS = {
    "the",
    "a",
    "an",
    "to",
    "for",
    "of",
    "in",
    "on",
    "and",
    "or",
    "is",
    "are",
    "add",
    "fix",
    "make",
    "update",
    "change",
    "new",
    "with",
    "this",
    "that",
    "code",
    "file",
    "files",
    "implement",
    "why",
    "how",
    "what",
    "does",
    "do",
}


def extract_terms(text: str) -> set[str]:
    """Lowercase word set with stopwords and short tokens dropped."""
    return {w for w in _WORD_RE.findall(text.casefold()) if w not in STOPWORDS and len(w) > 2}
