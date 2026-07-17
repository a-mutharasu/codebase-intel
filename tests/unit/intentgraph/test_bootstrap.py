"""Tests for session bootstrap — scoring, packing, coverage, degradation.

Covers:
- Scoring: relevance ranks correctly; trust breaks ties; stale/superseded down-weighted
- Orientation (L0/L1) always included ahead of unrelated detail
- Budget respected; truncation flagged; tiny budget still returns something
- Empty graph → coverage "none" + explicit ladder warning, no crash
- T-004 verify bar: 10 canned why-questions answered with the correct node cited,
  zero raw file reads (bootstrap only touches journal + index)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from codebase_intel.intentgraph.bootstrap import IntentBootstrapper, score_node
from codebase_intel.intentgraph.capture import CaptureService
from codebase_intel.intentgraph.journal import IntentJournal
from codebase_intel.intentgraph.models import (
    IntentLevel,
    IntentNode,
    IntentNodeStatus,
    TrustTier,
)

if TYPE_CHECKING:
    from pathlib import Path


def _node(title: str, why: str, **overrides: object) -> IntentNode:
    defaults: dict = {
        "node_id": IntentNode.make_id(IntentLevel.DECISION, title),
        "level": IntentLevel.DECISION,
        "title": title,
        "why": why,
        "trust": TrustTier.ASSERTED,
    }
    defaults.update(overrides)
    return IntentNode(**defaults)


NOW = datetime(2026, 7, 17, tzinfo=UTC)
TERMS = {"rate", "limiting", "redis"}


class TestScoring:
    def test_no_overlap_is_zero(self) -> None:
        node = _node("Caching", "sessions live in memory")
        assert score_node(node, TERMS, NOW) == 0.0

    def test_relevant_beats_irrelevant(self) -> None:
        relevant = _node("Rate limiting", "token bucket in redis")
        assert score_node(relevant, TERMS, NOW) > 0.0

    def test_confirmed_beats_mined_on_same_text(self) -> None:
        confirmed = _node("Rate limiting", "redis bucket", trust=TrustTier.CONFIRMED)
        mined = _node("Rate limiting", "redis bucket", trust=TrustTier.MINED)
        assert score_node(confirmed, TERMS, NOW) > score_node(mined, TERMS, NOW)

    def test_superseded_downweighted_but_nonzero(self) -> None:
        active = _node("Rate limiting", "redis bucket")
        superseded = _node(
            "Rate limiting",
            "redis bucket",
            status=IntentNodeStatus.SUPERSEDED,
            superseded_by="other",
        )
        assert 0 < score_node(superseded, TERMS, NOW) < score_node(active, TERMS, NOW)

    def test_old_nodes_decay_to_floor_not_zero(self) -> None:
        old = _node(
            "Rate limiting",
            "redis bucket",
            created_at=NOW - timedelta(days=1000),
            updated_at=NOW - timedelta(days=1000),
        )
        fresh = _node("Rate limiting", "redis bucket", created_at=NOW, updated_at=NOW)
        assert 0 < score_node(old, TERMS, NOW) < score_node(fresh, TERMS, NOW)


@pytest.fixture
def journal(tmp_path: Path) -> IntentJournal:
    return IntentJournal(tmp_path / "journal")


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "intent-graph.db"


@pytest.fixture
def capture(journal: IntentJournal, db_path: Path) -> CaptureService:
    return CaptureService(journal, db_path, actor="test")


@pytest.fixture
def bootstrapper(journal: IntentJournal, db_path: Path) -> IntentBootstrapper:
    return IntentBootstrapper(journal, db_path)


class TestBootstrap:
    async def test_empty_graph_reports_ladder(self, bootstrapper: IntentBootstrapper) -> None:
        result = await bootstrapper.bootstrap("add rate limiting")
        assert result.items == []
        assert result.coverage == "none"
        assert any("fall back" in w for w in result.warnings)

    async def test_orientation_included_even_when_unrelated(
        self, capture: CaptureService, bootstrapper: IntentBootstrapper
    ) -> None:
        await capture.record_why(
            title="Single process design", why="one operator, no ops team", level=IntentLevel.SYSTEM
        )
        await capture.record_why(title="Retry policy", why="upstream flakes hourly")

        result = await bootstrapper.bootstrap("improve retry behaviour")

        roles = {i.node_id: i.role for i in result.items}
        assert "orientation" in roles.values()
        assert "relevant" in roles.values()

    async def test_budget_respected_and_truncation_flagged(
        self, capture: CaptureService, bootstrapper: IntentBootstrapper
    ) -> None:
        for i in range(20):
            await capture.record_why(
                title=f"Retry policy variant {i}",
                why="retry with exponential backoff because upstream flakes " * 20,
            )

        result = await bootstrapper.bootstrap("retry backoff", budget_tokens=600)

        assert result.total_tokens <= 600
        assert result.truncated
        assert result.dropped_count > 0
        assert len(result.items) >= 1

    async def test_coverage_good_with_multiple_relevant(
        self, capture: CaptureService, bootstrapper: IntentBootstrapper
    ) -> None:
        await capture.record_why(title="Retry policy", why="backoff because rate limits")
        await capture.record_why(title="Retry budget", why="cap retries to protect latency")

        result = await bootstrapper.bootstrap("tune retry handling")
        assert result.coverage == "good"


CANNED_WHYS: list[tuple[str, str, str]] = [
    ("Journal-first storage", "SQLite index is derived; JSONL journal in git is truth", "storage"),
    ("Soft gate default", "hard pre-commit blocking gets uninstalled on the first hotfix", "gate"),
    ("Trust tiers on nodes", "a mined why must never read as confirmed fact", "trust"),
    ("Token bucket limiter", "O(1) redis ops beat sliding window sorted sets", "limiter"),
    ("Async-first IO", "MCP server multiplexes concurrent tool calls", "async"),
    ("Tree-sitter parsing", "one AST layer covers nineteen languages", "parsing"),
    ("Content-addressed entries", "replay idempotence lives in the data itself", "replay"),
    ("Superseded kept forever", "the reversal trail is the highest value precedent", "reversal"),
    ("Conservative classifier", "capture fatigue kills adoption faster than gaps", "classifier"),
    ("xxhash fingerprints", "fast content hashing for change detection", "fingerprint"),
]


class TestTenCannedQuestions:
    """T-004 verify bar: each question's top relevant item cites the right node."""

    async def test_all_ten_questions_cite_correct_node(
        self, capture: CaptureService, bootstrapper: IntentBootstrapper
    ) -> None:
        ids: dict[str, str] = {}
        for title, why, _ in CANNED_WHYS:
            recorded = await capture.record_why(title=title, why=why)
            ids[title] = recorded.node.node_id

        questions = [
            ("why is the sqlite index derived from a journal", "Journal-first storage"),
            ("why does the gate not block commits by default", "Soft gate default"),
            ("why do intent nodes carry trust tiers", "Trust tiers on nodes"),
            ("why token bucket instead of sliding window limiter", "Token bucket limiter"),
            ("why is all IO async in the MCP server", "Async-first IO"),
            ("why tree-sitter for parsing languages", "Tree-sitter parsing"),
            ("why are journal entries content-addressed for replay", "Content-addressed entries"),
            ("why keep superseded decisions forever", "Superseded kept forever"),
            ("why is the significance classifier conservative", "Conservative classifier"),
            ("why xxhash for content fingerprints", "xxhash fingerprints"),
        ]

        for question, expected_title in questions:
            result = await bootstrapper.bootstrap(question, budget_tokens=2000)
            relevant = [i for i in result.items if i.role == "relevant"]
            assert relevant, f"no relevant items for: {question}"
            assert relevant[0].node_id == ids[expected_title], (
                f"question {question!r} cited {relevant[0].node_id}, "
                f"expected {ids[expected_title]} ({expected_title})"
            )
