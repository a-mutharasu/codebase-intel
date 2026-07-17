"""Tests for intent graph models.

Covers:
- IntentNode.make_id determinism, casefolding, discriminator
- Supersession invariants (self-supersede, status/superseded_by coupling)
- TrustTier ranking
- to_context_string (trust always visible, superseded/stale warnings, verbose)
- IntentEdge self-edge rejection and edge_key
- JournalEntry.make content-addressed IDs and UTC normalization
"""

from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from codebase_intel.core.types import CodeAnchor, LineRange
from codebase_intel.intentgraph.models import (
    IntentEdge,
    IntentEdgeKind,
    IntentLevel,
    IntentNode,
    IntentNodeStatus,
    JournalEntry,
    JournalOp,
    TrustTier,
)


def _node(**overrides: object) -> IntentNode:
    defaults: dict = {
        "node_id": IntentNode.make_id(IntentLevel.DECISION, "Retry policy"),
        "level": IntentLevel.DECISION,
        "title": "Retry policy",
        "why": "Sarvam drops the first chunk without a ready frame, so we retry 3x.",
        "trust": TrustTier.ASSERTED,
    }
    defaults.update(overrides)
    return IntentNode(**defaults)


class TestMakeId:
    def test_deterministic(self) -> None:
        a = IntentNode.make_id(IntentLevel.DECISION, "Retry policy")
        b = IntentNode.make_id(IntentLevel.DECISION, "Retry policy")
        assert a == b

    def test_casefold_and_whitespace_insensitive(self) -> None:
        a = IntentNode.make_id(IntentLevel.DECISION, "Retry Policy")
        b = IntentNode.make_id(IntentLevel.DECISION, "  retry policy ")
        assert a == b

    def test_level_changes_id(self) -> None:
        a = IntentNode.make_id(IntentLevel.DECISION, "Retry policy")
        b = IntentNode.make_id(IntentLevel.COMPONENT, "Retry policy")
        assert a != b

    def test_discriminator_prevents_title_collision(self) -> None:
        a = IntentNode.make_id(IntentLevel.DECISION, "Retry policy", discriminator="DEC-001")
        b = IntentNode.make_id(IntentLevel.DECISION, "Retry policy", discriminator="DEC-002")
        assert a != b


class TestIntentNodeValidation:
    def test_why_required_non_empty(self) -> None:
        with pytest.raises(ValidationError):
            _node(why="")

    def test_cannot_supersede_itself(self) -> None:
        node_id = IntentNode.make_id(IntentLevel.DECISION, "Retry policy")
        with pytest.raises(ValidationError, match="cannot supersede itself"):
            _node(
                node_id=node_id,
                status=IntentNodeStatus.SUPERSEDED,
                superseded_by=node_id,
            )

    def test_superseded_status_requires_pointer(self) -> None:
        with pytest.raises(ValidationError, match="must be set together"):
            _node(status=IntentNodeStatus.SUPERSEDED)

    def test_pointer_requires_superseded_status(self) -> None:
        with pytest.raises(ValidationError, match="must be set together"):
            _node(superseded_by="abc123")

    def test_naive_datetime_coerced_to_utc(self) -> None:
        node = _node(created_at=datetime(2026, 1, 1, 12, 0, 0))
        assert node.created_at.tzinfo is not None

    def test_frozen(self) -> None:
        node = _node()
        with pytest.raises(ValidationError):
            node.title = "changed"  # type: ignore[misc]


class TestTrustTier:
    def test_rank_ordering(self) -> None:
        assert (
            TrustTier.MINED.rank
            < TrustTier.DERIVED.rank
            < TrustTier.ASSERTED.rank
            < TrustTier.CONFIRMED.rank
        )


class TestContextString:
    def test_trust_and_status_always_visible(self) -> None:
        text = _node().to_context_string()
        assert "asserted" in text
        assert "active" in text
        assert "L3" in text

    def test_superseded_warning(self) -> None:
        node = _node(
            status=IntentNodeStatus.SUPERSEDED,
            superseded_by="def456",
            supersede_reason="Replaced by circuit breaker",
        )
        text = node.to_context_string()
        assert "SUPERSEDED by def456" in text
        assert "circuit breaker" in text

    def test_stale_warning(self) -> None:
        text = _node(status=IntentNodeStatus.STALE).to_context_string()
        assert "anchored code changed" in text

    def test_verbose_includes_anchors_and_decision(self, tmp_path: object) -> None:
        anchor = CodeAnchor(
            file_path="src/retry.py",  # type: ignore[arg-type]
            line_range=LineRange(start=10, end=20),
        )
        node = _node(anchors=[anchor], decision_id="DEC-042")
        text = node.to_context_string(verbose=True)
        assert "retry.py" in text
        assert ":10-20" in text
        assert "DEC-042" in text

    def test_compact_omits_anchors(self) -> None:
        anchor = CodeAnchor(file_path="src/retry.py")  # type: ignore[arg-type]
        node = _node(anchors=[anchor])
        assert "Anchors" not in node.to_context_string(verbose=False)


class TestIntentEdge:
    def test_self_edge_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Self-edge"):
            IntentEdge(source_id="a", target_id="a", kind=IntentEdgeKind.CONTAINS)

    def test_edge_key(self) -> None:
        edge = IntentEdge(source_id="a", target_id="b", kind=IntentEdgeKind.CONTAINS)
        assert edge.edge_key == "a:b:contains"


class TestJournalEntry:
    def test_content_addressed_id_deterministic(self) -> None:
        ts = datetime(2026, 1, 1, 12, 0, 0)
        a = JournalEntry.make(JournalOp.ADD_NODE, {"x": 1}, actor="t", ts=ts)
        b = JournalEntry.make(JournalOp.ADD_NODE, {"x": 1}, actor="t", ts=ts)
        assert a.entry_id == b.entry_id

    def test_different_payload_different_id(self) -> None:
        ts = datetime(2026, 1, 1, 12, 0, 0)
        a = JournalEntry.make(JournalOp.ADD_NODE, {"x": 1}, ts=ts)
        b = JournalEntry.make(JournalOp.ADD_NODE, {"x": 2}, ts=ts)
        assert a.entry_id != b.entry_id

    def test_naive_ts_coerced_to_utc(self) -> None:
        entry = JournalEntry.make(JournalOp.ADD_NODE, {}, ts=datetime(2026, 1, 1))
        assert entry.ts.tzinfo is not None
