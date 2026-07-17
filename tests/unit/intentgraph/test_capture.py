"""Tests for the capture service — record_why, missing-why backlog, precedent.

Covers:
- record_why persists to journal AND materializes into the index
- supersedes: known target flipped to superseded; unknown target degrades to a warning
- parent link: CONTAINS edge created; unknown parent degrades to a warning
- record_missing_why: tagged backlog node with zero confidence
- check_precedent: text match, file match, includes superseded, active-first ordering
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from codebase_intel.intentgraph.capture import MISSING_WHY_TAG, CaptureService
from codebase_intel.intentgraph.journal import IntentJournal
from codebase_intel.intentgraph.models import (
    IntentLevel,
    IntentNodeStatus,
    TrustTier,
)
from codebase_intel.intentgraph.store import IntentGraphStore

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def journal(tmp_path: Path) -> IntentJournal:
    return IntentJournal(tmp_path / "journal")


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "intent-graph.db"


@pytest.fixture
def capture(journal: IntentJournal, db_path: Path) -> CaptureService:
    return CaptureService(journal, db_path, actor="test")


class TestRecordWhy:
    async def test_persists_and_materializes(
        self, capture: CaptureService, db_path: Path, journal: IntentJournal
    ) -> None:
        result = await capture.record_why(
            title="Token bucket for rate limiting",
            why="Sliding window needs per-key sorted sets; bucket is O(1) in Redis",
            file_paths=["src/middleware/rate_limiter.py"],
            tags=["performance"],
        )

        assert result.warnings == []
        assert journal.active_path.exists()
        async with IntentGraphStore.open(db_path) as store:
            loaded = await store.get_node(result.node.node_id)
        assert loaded is not None
        assert loaded.trust == TrustTier.ASSERTED
        assert loaded.source == "capture"

    async def test_supersedes_known_target(self, capture: CaptureService, db_path: Path) -> None:
        old = await capture.record_why(title="Sliding window", why="initial choice")
        new = await capture.record_why(
            title="Token bucket", why="simpler and O(1)", supersedes=old.node.node_id
        )

        assert new.warnings == []
        async with IntentGraphStore.open(db_path) as store:
            old_loaded = await store.get_node(old.node.node_id)
        assert old_loaded is not None
        assert old_loaded.status == IntentNodeStatus.SUPERSEDED
        assert old_loaded.superseded_by == new.node.node_id

    async def test_supersedes_unknown_target_degrades(self, capture: CaptureService) -> None:
        result = await capture.record_why(
            title="Token bucket", why="why", supersedes="does-not-exist"
        )
        assert any("not found" in w for w in result.warnings)

    async def test_parent_link_creates_contains_edge(
        self, capture: CaptureService, db_path: Path
    ) -> None:
        parent = await capture.record_why(
            title="Auth service boundaries",
            why="auth owns session state",
            level=IntentLevel.SERVICE,
        )
        child = await capture.record_why(
            title="Token families", why="rotation detection", parent_id=parent.node.node_id
        )

        assert child.warnings == []
        async with IntentGraphStore.open(db_path) as store:
            children = await store.children(parent.node.node_id)
        assert [c.node_id for c in children] == [child.node.node_id]

    async def test_unknown_parent_degrades(self, capture: CaptureService) -> None:
        result = await capture.record_why(title="X", why="y", parent_id="ghost")
        assert any("parent" in w for w in result.warnings)


class TestMissingWhy:
    async def test_backlog_node_shape(self, capture: CaptureService, db_path: Path) -> None:
        result = await capture.record_missing_why(
            ["src/billing/refunds.py"], "new source file: src/billing/refunds.py"
        )

        node = result.node
        assert MISSING_WHY_TAG in node.tags
        assert node.confidence == 0.0
        assert node.trust == TrustTier.MINED
        assert node.title.startswith("MISSING WHY")
        async with IntentGraphStore.open(db_path) as store:
            assert await store.get_node(node.node_id) is not None


class TestCheckPrecedent:
    async def test_finds_by_text(self, capture: CaptureService) -> None:
        await capture.record_why(title="Token bucket", why="rate limiting choice")
        matches = await capture.check_precedent(query="rate limiting")
        assert len(matches) == 1

    async def test_finds_by_file(self, capture: CaptureService, tmp_path: Path) -> None:
        target = tmp_path / "src" / "limiter.py"
        await capture.record_why(title="Token bucket", why="w", file_paths=[str(target)])
        matches = await capture.check_precedent(file_paths=[str(target)])
        assert len(matches) == 1

    async def test_includes_superseded_and_orders_active_first(
        self, capture: CaptureService
    ) -> None:
        old = await capture.record_why(title="Sliding window limiter", why="first attempt")
        await capture.record_why(
            title="Token bucket limiter", why="reversed course", supersedes=old.node.node_id
        )

        matches = await capture.check_precedent(query="limiter")
        assert len(matches) == 2
        assert matches[0].status == IntentNodeStatus.ACTIVE
        assert matches[1].status == IntentNodeStatus.SUPERSEDED

    async def test_no_matches(self, capture: CaptureService) -> None:
        assert await capture.check_precedent(query="nothing recorded") == []
