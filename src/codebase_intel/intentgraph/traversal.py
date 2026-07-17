"""Traversal API — enter the intent graph at any level and drill.

The read path mirrors how an engineer onboards: orientation at the top,
progressively narrower questions downward. Every answer carries trust and
status labels — the consumer always knows how much to believe.

Edge cases:
- Unknown node_id: empty view with a warning, never an exception (a stale ID
  from a previous session must not break the session)
- Deleted index file: opening the store recreates the schema and the journal
  replays — traversal self-heals (the ladder's first rung)
- Node with no parents/children: valid — L0 has no parents, leaves have no children
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from codebase_intel.intentgraph.store import IntentGraphStore

if TYPE_CHECKING:
    from pathlib import Path

    from codebase_intel.intentgraph.journal import IntentJournal
    from codebase_intel.intentgraph.models import IntentLevel, IntentNode


def _node_summary(node: IntentNode) -> dict[str, Any]:
    return {
        "node_id": node.node_id,
        "level": int(node.level),
        "title": node.title,
        "status": node.status.value,
        "trust": node.trust.value,
    }


class TraversalService:
    """Read-only queries over the intent graph."""

    def __init__(self, journal: IntentJournal, db_path: Path) -> None:
        self._journal = journal
        self._db_path = db_path

    async def node_view(self, node_id: str) -> dict[str, Any]:
        """One node in full, with its neighborhood summarized for drilling."""
        async with IntentGraphStore.open(self._db_path) as store:
            await store.materialize(self._journal)
            node = await store.get_node(node_id)
            if node is None:
                return {
                    "found": False,
                    "warnings": [f"intent node {node_id} not found — it may predate a rebuild"],
                }
            parents = await store.parents(node_id)
            children = await store.children(node_id)
            edges = await store.edges_from(node_id)
        return {
            "found": True,
            "node": _node_summary(node) | {"content": node.to_context_string(verbose=True)},
            "parents": [_node_summary(p) for p in parents],
            "children": [_node_summary(c) for c in children],
            "edges": [
                {"target": e.target_id, "kind": e.kind.value}
                for e in edges
                if e.kind.value != "contains"
            ],
            "warnings": [],
        }

    async def overview(self, level: IntentLevel) -> list[IntentNode]:
        """All active nodes at one abstraction level — the entry point."""
        async with IntentGraphStore.open(self._db_path) as store:
            await store.materialize(self._journal)
            return await store.nodes_at_level(level)

    async def why_for_file(self, file_path: str) -> list[IntentNode]:
        """Every why that anchors this file (or a directory above it)."""
        async with IntentGraphStore.open(self._db_path) as store:
            await store.materialize(self._journal)
            return await store.nodes_for_path(file_path)

    async def coverage(self) -> dict[str, int]:
        """Status counts — the decay metric primitive."""
        async with IntentGraphStore.open(self._db_path) as store:
            await store.materialize(self._journal)
            return await store.counts()
