"""Tests for the traversal API.

Covers:
- node_view: full node + parents/children/edges; unknown ID degrades to a warning
- overview by level, why_for_file, coverage counts
- Self-healing: deleting the SQLite index does not break traversal (journal replays)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from codebase_intel.intentgraph.capture import CaptureService
from codebase_intel.intentgraph.journal import IntentJournal
from codebase_intel.intentgraph.models import IntentLevel
from codebase_intel.intentgraph.traversal import TraversalService

if TYPE_CHECKING:
    from pathlib import Path


def _delete(path: Path) -> None:
    path.unlink()


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
def traversal(journal: IntentJournal, db_path: Path) -> TraversalService:
    return TraversalService(journal, db_path)


class TestNodeView:
    async def test_view_with_neighborhood(
        self, capture: CaptureService, traversal: TraversalService
    ) -> None:
        parent = await capture.record_why(
            title="Auth service", why="auth owns session state", level=IntentLevel.SERVICE
        )
        child = await capture.record_why(
            title="Token families", why="rotation detection", parent_id=parent.node.node_id
        )

        view = await traversal.node_view(parent.node.node_id)

        assert view["found"]
        assert view["node"]["title"] == "Auth service"
        assert "auth owns session state" in view["node"]["content"]
        assert [c["node_id"] for c in view["children"]] == [child.node.node_id]

        child_view = await traversal.node_view(child.node.node_id)
        assert [p["node_id"] for p in child_view["parents"]] == [parent.node.node_id]

    async def test_unknown_node_degrades(self, traversal: TraversalService) -> None:
        view = await traversal.node_view("does-not-exist")
        assert not view["found"]
        assert any("not found" in w for w in view["warnings"])


class TestQueries:
    async def test_overview_by_level(
        self, capture: CaptureService, traversal: TraversalService
    ) -> None:
        await capture.record_why(
            title="Monolith first", why="team of one", level=IntentLevel.SYSTEM
        )
        await capture.record_why(title="Retry policy", why="flaky upstream")

        system = await traversal.overview(IntentLevel.SYSTEM)
        assert [n.title for n in system] == ["Monolith first"]

    async def test_why_for_file(
        self, capture: CaptureService, traversal: TraversalService, tmp_path: Path
    ) -> None:
        target = tmp_path / "src" / "auth" / "session.py"
        await capture.record_why(
            title="Session hashing", why="tokens are hashed at rest", file_paths=[str(target)]
        )
        nodes = await traversal.why_for_file(target.resolve().as_posix())
        assert len(nodes) == 1

    async def test_coverage_counts(
        self, capture: CaptureService, traversal: TraversalService
    ) -> None:
        await capture.record_why(title="A", why="a")
        await capture.record_why(title="B", why="b")
        assert await traversal.coverage() == {"active": 2}


class TestSelfHealing:
    async def test_deleted_index_replays_from_journal(
        self,
        capture: CaptureService,
        traversal: TraversalService,
        db_path: Path,
    ) -> None:
        recorded = await capture.record_why(title="Journal first", why="index is derived")

        _delete(db_path)  # simulate corruption/loss of the derived index

        view = await traversal.node_view(recorded.node.node_id)
        assert view["found"]
        assert view["node"]["title"] == "Journal first"
