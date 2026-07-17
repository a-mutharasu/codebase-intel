"""Capture service — the write path of the intent graph.

This is the core loop of v2: record a why at decision time, check precedent
before re-making a decision. Capture must be fast (journal append is the
commit point) and must degrade rather than fail — a broken supersede target
or a failed index refresh never loses the captured why.

Edge cases:
- Supersede target doesn't exist: the why is still recorded; the supersession
  is skipped with a warning instead of failing the capture
- Parent link to unknown node: same — record the node, warn, skip the edge
- Index refresh failure (locked DB, corrupt index): journal append already
  succeeded, so nothing is lost; rebuild recovers
- Same title re-captured: deterministic IDs make it an upsert (latest why wins)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from codebase_intel.core.types import CodeAnchor
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
    from codebase_intel.intentgraph.journal import IntentJournal

logger = logging.getLogger(__name__)

MISSING_WHY_TAG = "missing-why"


def _resolve_posix(raw: str) -> str:
    """Sync path resolution — anchors are stored as resolved POSIX strings."""
    return Path(raw).resolve().as_posix()


class CaptureResult:
    """A recorded node plus any degradations that happened along the way."""

    def __init__(self, node: IntentNode, warnings: list[str] | None = None) -> None:
        self.node = node
        self.warnings = warnings or []


class CaptureService:
    """Records intent and answers precedent queries.

    Opens a short-lived store connection per operation: capture callers (MCP
    handler, git hook, CLI) are different processes, so a persistent handle
    would only add lock contention.
    """

    def __init__(self, journal: IntentJournal, db_path: Path, actor: str = "agent") -> None:
        self._journal = journal
        self._db_path = db_path
        self._actor = actor

    async def record_why(
        self,
        title: str,
        why: str,
        level: IntentLevel = IntentLevel.DECISION,
        file_paths: list[str] | None = None,
        tags: list[str] | None = None,
        author: str = "unknown",
        session: str | None = None,
        trust: TrustTier = TrustTier.ASSERTED,
        supersedes: str | None = None,
        parent_id: str | None = None,
    ) -> CaptureResult:
        """Record one why. The journal append is the durability point."""
        from codebase_intel.intentgraph.anchors import hash_file

        # Hash at capture time so divergence is detectable later (honesty machinery)
        anchors = [
            CodeAnchor(file_path=Path(p), content_hash=hash_file(Path(p)))
            for p in (file_paths or [])
        ]
        node = IntentNode(
            node_id=IntentNode.make_id(level, title),
            level=level,
            title=title,
            why=why,
            trust=trust,
            anchors=anchors,
            tags=tags or [],
            author=author,
            session=session,
            source="capture",
        )
        warnings: list[str] = []
        self._journal.append(
            JournalEntry.make(JournalOp.ADD_NODE, node.model_dump(mode="json"), actor=self._actor)
        )

        async with IntentGraphStore.open(self._db_path) as store:
            await store.materialize(self._journal)

            if supersedes:
                target = await store.get_node(supersedes)
                if target is None:
                    warnings.append(f"supersedes target {supersedes} not found — skipped")
                elif target.node_id == node.node_id:
                    warnings.append("node cannot supersede itself — skipped")
                else:
                    self._journal.append(
                        JournalEntry.make(
                            JournalOp.SUPERSEDE_NODE,
                            {
                                "node_id": supersedes,
                                "superseded_by": node.node_id,
                                "reason": f"Superseded by '{title}'",
                            },
                            actor=self._actor,
                        )
                    )

            if parent_id:
                parent = await store.get_node(parent_id)
                if parent is None:
                    warnings.append(f"parent {parent_id} not found — CONTAINS edge skipped")
                elif parent.node_id == node.node_id:
                    warnings.append("node cannot contain itself — edge skipped")
                else:
                    edge = IntentEdge(
                        source_id=parent.node_id,
                        target_id=node.node_id,
                        kind=IntentEdgeKind.CONTAINS,
                    )
                    self._journal.append(
                        JournalEntry.make(
                            JournalOp.ADD_EDGE, edge.model_dump(mode="json"), actor=self._actor
                        )
                    )

            await store.materialize(self._journal)
        return CaptureResult(node, warnings)

    async def record_missing_why(
        self,
        file_paths: list[str],
        description: str,
    ) -> CaptureResult:
        """Record a structural change that shipped without a rationale.

        The backlog node is the honest gap-marker the soft gate leaves behind:
        confidence 0, mined tier, tagged for later backfill.
        """
        title = f"MISSING WHY: {description}"[:120]
        node = IntentNode(
            node_id=IntentNode.make_id(IntentLevel.DECISION, title),
            level=IntentLevel.DECISION,
            title=title,
            why=(
                "Structural change committed without a recorded rationale. "
                f"Files: {', '.join(file_paths[:10])}. Backfill the why or supersede this node."
            ),
            trust=TrustTier.MINED,
            confidence=0.0,
            anchors=[CodeAnchor(file_path=Path(p)) for p in file_paths],
            tags=[MISSING_WHY_TAG],
            source="gate",
        )
        self._journal.append(
            JournalEntry.make(JournalOp.ADD_NODE, node.model_dump(mode="json"), actor="intent-gate")
        )
        async with IntentGraphStore.open(self._db_path) as store:
            await store.materialize(self._journal)
        return CaptureResult(node)

    async def check_precedent(
        self,
        query: str | None = None,
        file_paths: list[str] | None = None,
        limit: int = 10,
    ) -> list[IntentNode]:
        """Find prior decisions relevant to a question or set of files.

        Superseded nodes are deliberately included — "we tried that and
        reversed it" is the highest-value precedent an agent can get.
        """
        matches: dict[str, IntentNode] = {}
        async with IntentGraphStore.open(self._db_path) as store:
            await store.materialize(self._journal)
            if query:
                for node in await store.search(query, limit=limit * 2):
                    matches[node.node_id] = node
            for raw in file_paths or []:
                for node in await store.nodes_for_path(_resolve_posix(raw)):
                    matches[node.node_id] = node

        def sort_key(node: IntentNode) -> tuple[int, int, int]:
            active_first = 0 if node.status == IntentNodeStatus.ACTIVE else 1
            return (active_first, -node.trust.rank, int(node.level))

        return sorted(matches.values(), key=sort_key)[:limit]
