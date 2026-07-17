"""Tests for v1 decision → intent graph migration.

Covers:
- Decisions become L3 nodes with provenance-derived trust and back-reference
- supersedes chains become SUPERSEDE entries (old node marked, pointer set)
- related_decisions become RELATES_TO edges
- Re-running migration is a no-op (idempotent, no duplicate journal lines)
- Unknown supersedes/related targets produce warnings, not failures
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from codebase_intel.core.config import DecisionConfig
from codebase_intel.core.types import DecisionStatus
from codebase_intel.decisions.models import DecisionRecord
from codebase_intel.decisions.store import DecisionStore
from codebase_intel.intentgraph.journal import IntentJournal
from codebase_intel.intentgraph.migrate import (
    decision_node_id,
    decision_to_node,
    migrate_v1_decisions,
)
from codebase_intel.intentgraph.models import (
    IntentEdgeKind,
    IntentLevel,
    IntentNodeStatus,
    TrustTier,
)
from codebase_intel.intentgraph.store import IntentGraphStore

if TYPE_CHECKING:
    from pathlib import Path


def _decision(dec_id: str, **overrides: object) -> DecisionRecord:
    defaults: dict = {
        "id": dec_id,
        "title": f"Decision {dec_id}",
        "context": "We needed rate limiting",
        "decision": "Token bucket with Redis",
    }
    defaults.update(overrides)
    return DecisionRecord(**defaults)


@pytest.fixture
def decision_store(tmp_path: Path) -> DecisionStore:
    config = DecisionConfig(decisions_dir=tmp_path / "decisions")
    return DecisionStore(config, tmp_path)


@pytest.fixture
def journal(tmp_path: Path) -> IntentJournal:
    return IntentJournal(tmp_path / "journal")


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "intent-graph.db"


class TestDecisionToNode:
    def test_maps_core_fields(self) -> None:
        node = decision_to_node(_decision("DEC-001", author="mutharasu", tags=["security"]))
        assert node.level == IntentLevel.DECISION
        assert node.decision_id == "DEC-001"
        assert "rate limiting" in node.why
        assert "Token bucket" in node.why
        assert node.author == "mutharasu"
        assert node.tags == ["security"]
        assert node.source == "migration"

    def test_trust_from_provenance(self) -> None:
        assert decision_to_node(_decision("D1", source="manual")).trust == TrustTier.CONFIRMED
        assert decision_to_node(_decision("D2", source="git-mined")).trust == TrustTier.MINED
        assert decision_to_node(_decision("D3", source="ai-suggested")).trust == TrustTier.ASSERTED
        assert decision_to_node(_decision("D4", source="whatever")).trust == TrustTier.ASSERTED

    def test_deprecated_maps_to_stale(self) -> None:
        node = decision_to_node(_decision("D1", status=DecisionStatus.DEPRECATED))
        assert node.status == IntentNodeStatus.STALE

    def test_same_title_different_decisions_do_not_collide(self) -> None:
        a = decision_to_node(_decision("DEC-001", title="Use Redis"))
        b = decision_to_node(_decision("DEC-002", title="Use Redis"))
        assert a.node_id != b.node_id


class TestMigration:
    async def test_full_migration(
        self, decision_store: DecisionStore, journal: IntentJournal, db_path: Path
    ) -> None:
        await decision_store.save(_decision("DEC-001", related_decisions=["DEC-002"]))
        await decision_store.save(_decision("DEC-002"))
        await decision_store.save(
            _decision("DEC-003", supersedes="DEC-001", status=DecisionStatus.ACTIVE)
        )

        async with IntentGraphStore.open(db_path) as store:
            result = await migrate_v1_decisions(decision_store, journal, store)

            assert result.migrated == 3
            assert result.superseded == 1
            assert result.edges == 1
            assert result.warnings == []

            old = await store.get_node(decision_node_id("DEC-001", "Decision DEC-001"))
            assert old is not None
            assert old.status == IntentNodeStatus.SUPERSEDED
            assert old.superseded_by == decision_node_id("DEC-003", "Decision DEC-003")

            edges = await store.edges_from(old.node_id)
            assert [e.kind for e in edges] == [IntentEdgeKind.RELATES_TO]

    async def test_rerun_is_idempotent(
        self, decision_store: DecisionStore, journal: IntentJournal, db_path: Path
    ) -> None:
        await decision_store.save(_decision("DEC-001"))
        await decision_store.save(_decision("DEC-002", supersedes="DEC-001"))

        async with IntentGraphStore.open(db_path) as store:
            await migrate_v1_decisions(decision_store, journal, store)
            lines_after_first = journal.active_path.read_text().count("\n")

            second = await migrate_v1_decisions(decision_store, journal, store)
            lines_after_second = journal.active_path.read_text().count("\n")

        assert second.migrated == 0
        assert second.skipped == 2
        assert second.superseded == 0
        assert lines_after_first == lines_after_second

    async def test_unknown_supersedes_warns(
        self, decision_store: DecisionStore, journal: IntentJournal, db_path: Path
    ) -> None:
        await decision_store.save(_decision("DEC-002", supersedes="DEC-999"))

        async with IntentGraphStore.open(db_path) as store:
            result = await migrate_v1_decisions(decision_store, journal, store)

        assert result.migrated == 1
        assert any("DEC-999" in w for w in result.warnings)

    async def test_unknown_related_warns(
        self, decision_store: DecisionStore, journal: IntentJournal, db_path: Path
    ) -> None:
        await decision_store.save(_decision("DEC-001", related_decisions=["DEC-404"]))

        async with IntentGraphStore.open(db_path) as store:
            result = await migrate_v1_decisions(decision_store, journal, store)

        assert result.edges == 0
        assert any("DEC-404" in w for w in result.warnings)
