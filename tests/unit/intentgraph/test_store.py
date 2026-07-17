"""Tests for the intent graph SQLite index.

Covers:
- Materialize applies entries; nodes/edges become queryable
- Replay idempotence: same journal twice → second pass all-skipped, same state
- Rebuild-from-journal equivalence (the NFR-R4 recovery guarantee)
- UPDATE_NODE patches (and forbidden-field stripping), SUPERSEDE_NODE
- Bad entries (unknown node) degrade to errors without stopping replay
- CONTAINS traversal (children/parents), edge retraction
- nodes_for_path exact + directory-prefix anchors, search, counts, level filter
- Schema from a newer tool version is refused
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aiosqlite
import pytest

from codebase_intel.core.exceptions import StorageMigrationError
from codebase_intel.core.types import CodeAnchor
from codebase_intel.intentgraph.journal import IntentJournal
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
from codebase_intel.intentgraph.store import IntentGraphStore

if TYPE_CHECKING:
    from pathlib import Path


def _node(title: str, level: IntentLevel = IntentLevel.DECISION, **overrides: object) -> IntentNode:
    defaults: dict = {
        "node_id": IntentNode.make_id(level, title),
        "level": level,
        "title": title,
        "why": f"why for {title}",
        "trust": TrustTier.ASSERTED,
    }
    defaults.update(overrides)
    return IntentNode(**defaults)


def _add(journal: IntentJournal, node: IntentNode) -> None:
    journal.append(JournalEntry.make(JournalOp.ADD_NODE, node.model_dump(mode="json")))


def _add_edge(journal: IntentJournal, source: IntentNode, target: IntentNode) -> None:
    edge = IntentEdge(
        source_id=source.node_id, target_id=target.node_id, kind=IntentEdgeKind.CONTAINS
    )
    journal.append(JournalEntry.make(JournalOp.ADD_EDGE, edge.model_dump(mode="json")))


@pytest.fixture
def journal(tmp_path: Path) -> IntentJournal:
    return IntentJournal(tmp_path / "journal")


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "intent-graph.db"


class TestMaterialize:
    async def test_nodes_queryable_after_materialize(
        self, journal: IntentJournal, db_path: Path
    ) -> None:
        node = _node("Retry policy")
        _add(journal, node)

        async with IntentGraphStore.open(db_path) as store:
            result = await store.materialize(journal)
            assert result.applied == 1
            assert result.errors == []
            loaded = await store.get_node(node.node_id)

        assert loaded is not None
        assert loaded.why == "why for Retry policy"
        assert loaded.trust == TrustTier.ASSERTED

    async def test_replay_is_idempotent(self, journal: IntentJournal, db_path: Path) -> None:
        _add(journal, _node("A"))
        _add(journal, _node("B"))

        async with IntentGraphStore.open(db_path) as store:
            first = await store.materialize(journal)
            second = await store.materialize(journal)
            counts = await store.counts()

        assert first.applied == 2
        assert second.applied == 0
        assert second.skipped == 2
        assert counts == {"active": 2}

    async def test_rebuild_equals_materialize(self, journal: IntentJournal, db_path: Path) -> None:
        parent = _node("Auth service", IntentLevel.SERVICE)
        child = _node("Token families")
        _add(journal, parent)
        _add(journal, child)
        _add_edge(journal, parent, child)

        async with IntentGraphStore.open(db_path) as store:
            await store.materialize(journal)
            before_nodes = {n.node_id: n for n in await store.nodes_at_level(IntentLevel.SERVICE)}
            before_children = await store.children(parent.node_id)

            rebuilt = await store.rebuild(journal)
            after_nodes = {n.node_id: n for n in await store.nodes_at_level(IntentLevel.SERVICE)}
            after_children = await store.children(parent.node_id)

        assert rebuilt.applied == 3
        assert before_nodes == after_nodes
        assert [c.node_id for c in before_children] == [c.node_id for c in after_children]

    async def test_bad_entry_degrades_not_fails(
        self, journal: IntentJournal, db_path: Path
    ) -> None:
        journal.append(
            JournalEntry.make(JournalOp.UPDATE_NODE, {"node_id": "ghost", "changes": {}})
        )
        good = _node("Survivor")
        _add(journal, good)

        async with IntentGraphStore.open(db_path) as store:
            result = await store.materialize(journal)
            loaded = await store.get_node(good.node_id)

        assert result.applied == 1
        assert len(result.errors) == 1
        assert "ghost" in result.errors[0]
        assert loaded is not None


class TestNodeChanges:
    async def test_update_node_patches_status(self, journal: IntentJournal, db_path: Path) -> None:
        node = _node("Retry policy")
        _add(journal, node)
        journal.append(
            JournalEntry.make(
                JournalOp.UPDATE_NODE,
                {"node_id": node.node_id, "changes": {"status": "stale"}},
            )
        )

        async with IntentGraphStore.open(db_path) as store:
            await store.materialize(journal)
            loaded = await store.get_node(node.node_id)

        assert loaded is not None
        assert loaded.status == IntentNodeStatus.STALE

    async def test_update_cannot_patch_identity(
        self, journal: IntentJournal, db_path: Path
    ) -> None:
        node = _node("Retry policy")
        _add(journal, node)
        journal.append(
            JournalEntry.make(
                JournalOp.UPDATE_NODE,
                {"node_id": node.node_id, "changes": {"node_id": "hijacked", "why": "new why"}},
            )
        )

        async with IntentGraphStore.open(db_path) as store:
            await store.materialize(journal)
            loaded = await store.get_node(node.node_id)
            hijacked = await store.get_node("hijacked")

        assert loaded is not None
        assert loaded.why == "new why"
        assert hijacked is None

    async def test_supersede_node(self, journal: IntentJournal, db_path: Path) -> None:
        old = _node("Single tokens")
        new = _node("Token families")
        _add(journal, old)
        _add(journal, new)
        journal.append(
            JournalEntry.make(
                JournalOp.SUPERSEDE_NODE,
                {
                    "node_id": old.node_id,
                    "superseded_by": new.node_id,
                    "reason": "Refresh-token rotation needs families",
                },
            )
        )

        async with IntentGraphStore.open(db_path) as store:
            await store.materialize(journal)
            loaded = await store.get_node(old.node_id)
            active = await store.nodes_at_level(IntentLevel.DECISION)
            everything = await store.nodes_at_level(IntentLevel.DECISION, include_superseded=True)

        assert loaded is not None
        assert loaded.status == IntentNodeStatus.SUPERSEDED
        assert loaded.superseded_by == new.node_id
        assert "families" in (loaded.supersede_reason or "")
        assert {n.node_id for n in active} == {new.node_id}
        assert len(everything) == 2


class TestTraversalAndQueries:
    async def test_children_and_parents(self, journal: IntentJournal, db_path: Path) -> None:
        parent = _node("Auth service", IntentLevel.SERVICE)
        child = _node("Token families")
        _add(journal, parent)
        _add(journal, child)
        _add_edge(journal, parent, child)

        async with IntentGraphStore.open(db_path) as store:
            await store.materialize(journal)
            children = await store.children(parent.node_id)
            parents = await store.parents(child.node_id)

        assert [c.node_id for c in children] == [child.node_id]
        assert [p.node_id for p in parents] == [parent.node_id]

    async def test_retracted_edge_hidden_from_traversal(
        self, journal: IntentJournal, db_path: Path
    ) -> None:
        parent = _node("Auth service", IntentLevel.SERVICE)
        child = _node("Token families")
        _add(journal, parent)
        _add(journal, child)
        _add_edge(journal, parent, child)
        journal.append(
            JournalEntry.make(
                JournalOp.RETRACT_EDGE,
                {
                    "source_id": parent.node_id,
                    "target_id": child.node_id,
                    "kind": IntentEdgeKind.CONTAINS.value,
                },
            )
        )

        async with IntentGraphStore.open(db_path) as store:
            await store.materialize(journal)
            children = await store.children(parent.node_id)
            edges = await store.edges_from(parent.node_id)

        assert children == []
        assert edges == []

    async def test_nodes_for_path_exact_and_directory(
        self, journal: IntentJournal, db_path: Path, tmp_path: Path
    ) -> None:
        file_anchor = CodeAnchor(file_path=tmp_path / "src" / "auth" / "session.py")
        dir_anchor = CodeAnchor(file_path=tmp_path / "src" / "auth")
        file_node = _node("Session hashing", anchors=[file_anchor])
        dir_node = _node("Auth owns sessions", IntentLevel.SERVICE, anchors=[dir_anchor])
        unrelated = _node("Billing retries")
        for n in (file_node, dir_node, unrelated):
            _add(journal, n)

        target = (tmp_path / "src" / "auth" / "session.py").resolve().as_posix()
        async with IntentGraphStore.open(db_path) as store:
            await store.materialize(journal)
            matches = await store.nodes_for_path(target)

        # Directory anchor answers for files under it; ordered top-level first
        assert [n.node_id for n in matches] == [dir_node.node_id, file_node.node_id]

    async def test_search_and_counts(self, journal: IntentJournal, db_path: Path) -> None:
        _add(journal, _node("Retry policy", why="exponential backoff because of rate limits"))
        _add(journal, _node("Caching", why="Redis for session reads"))

        async with IntentGraphStore.open(db_path) as store:
            await store.materialize(journal)
            hits = await store.search("backoff")
            counts = await store.counts()

        assert len(hits) == 1
        assert hits[0].title == "Retry policy"
        assert counts == {"active": 2}


class TestSchemaVersioning:
    async def test_future_schema_refused(self, db_path: Path) -> None:
        async with IntentGraphStore.open(db_path):
            pass
        async with aiosqlite.connect(db_path) as db:
            await db.execute("INSERT INTO schema_version (version) VALUES (999)")
            await db.commit()

        with pytest.raises(StorageMigrationError, match="newer"):
            async with IntentGraphStore.open(db_path):
                pass
