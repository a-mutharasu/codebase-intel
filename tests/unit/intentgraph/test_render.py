"""Tests for the human projection and the review promotion flow.

Covers:
- Mermaid: level subgraphs, CONTAINS edges, superseded styling, empty shell
- HTML: standalone (no external resources), escaped content, trust badges,
  child nesting, superseded note
- Promotion round-trip (T-007 verify bar): asserted why → intent-confirm →
  confirmed trust with reviewer/owner recorded, visible in a fresh render
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from codebase_intel.intentgraph.capture import CaptureService
from codebase_intel.intentgraph.journal import IntentJournal
from codebase_intel.intentgraph.models import IntentLevel, TrustTier
from codebase_intel.intentgraph.render import IntentRenderer

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


@pytest.fixture
def renderer(journal: IntentJournal, db_path: Path) -> IntentRenderer:
    return IntentRenderer(journal, db_path)


class TestMermaid:
    async def test_empty_graph_shell(self, renderer: IntentRenderer) -> None:
        text = await renderer.to_mermaid()
        assert text.startswith("flowchart TD")
        assert "No intent recorded yet" in text

    async def test_levels_edges_and_supersession(
        self, capture: CaptureService, renderer: IntentRenderer
    ) -> None:
        parent = await capture.record_why(
            title="Auth service", why="owns sessions", level=IntentLevel.SERVICE
        )
        child = await capture.record_why(
            title="Token families", why="rotation", parent_id=parent.node.node_id
        )
        old = await capture.record_why(title="Single tokens", why="v1 approach")
        await capture.record_why(title="Family tokens", why="reversal", supersedes=old.node.node_id)

        text = await renderer.to_mermaid()

        assert 'subgraph SG1["L1 · Services & modules"]' in text
        assert f"{parent.node.node_id} --> {child.node.node_id}" in text
        assert "superseded by" in text
        assert f"style {old.node.node_id} stroke-dasharray" in text

    async def test_hostile_title_sanitized(
        self, capture: CaptureService, renderer: IntentRenderer
    ) -> None:
        await capture.record_why(title='Weird "quoted" [bracketed] title', why="w")
        text = await renderer.to_mermaid()
        assert '"quoted"' not in text.split("\n", 1)[1]
        assert "(bracketed)" in text


class TestHtml:
    async def test_standalone_and_escaped(
        self, capture: CaptureService, renderer: IntentRenderer
    ) -> None:
        await capture.record_why(title="XSS <script> attempt", why="because <b>reasons</b> & more")
        html = await renderer.to_html(project_name="demo")

        assert html.startswith("<!doctype html>")
        assert "<script>" not in html  # escaped, and no JS at all
        assert "http" not in html.split("<body>")[0]  # no external resources
        assert "&lt;b&gt;reasons&lt;/b&gt; &amp; more" in html

    async def test_nesting_and_badges(
        self, capture: CaptureService, renderer: IntentRenderer
    ) -> None:
        parent = await capture.record_why(
            title="Auth service", why="owns sessions", level=IntentLevel.SERVICE
        )
        await capture.record_why(
            title="Token families", why="rotation", parent_id=parent.node.node_id
        )
        html = await renderer.to_html()

        auth_pos = html.find("Auth service")
        token_pos = html.find("Token families")
        assert 0 < auth_pos < token_pos
        assert 'class="badge asserted"' in html

    async def test_empty_graph_shell(self, renderer: IntentRenderer) -> None:
        html = await renderer.to_html()
        assert "No intent recorded yet" in html


class TestPromotionRoundTrip:
    async def test_asserted_to_confirmed(
        self, capture: CaptureService, renderer: IntentRenderer
    ) -> None:
        recorded = await capture.record_why(title="Token bucket", why="O(1)")
        assert recorded.node.trust == TrustTier.ASSERTED

        promoted = await capture.promote(
            recorded.node.node_id, reviewer="mutharasu", owner="platform-team"
        )

        assert promoted is not None
        assert promoted.trust == TrustTier.CONFIRMED
        assert promoted.metadata["reviewed_by"] == "mutharasu"
        assert promoted.metadata["owner"] == "platform-team"
        assert promoted.author == recorded.node.author  # original author preserved

        html = await renderer.to_html()
        assert 'class="badge confirmed"' in html

    async def test_promote_unknown_returns_none(self, capture: CaptureService) -> None:
        assert await capture.promote("ghost", reviewer="x") is None
