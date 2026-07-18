"""Tests for shared term-extraction utility."""

from __future__ import annotations

from codebase_intel.core.text import extract_terms


class TestExtractTerms:
    def test_strips_stopwords_and_short_tokens(self) -> None:
        terms = extract_terms("Fix the bug in a new file")
        assert terms == {"bug"}

    def test_casefolds(self) -> None:
        assert extract_terms("Redis") == extract_terms("redis") == {"redis"}

    def test_empty_string(self) -> None:
        assert extract_terms("") == set()

    def test_numbers_and_underscores_kept(self) -> None:
        terms = extract_terms("rate_limit uses xxh64 hashing")
        assert "rate_limit" in terms
        assert "xxh64" in terms
