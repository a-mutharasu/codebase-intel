"""v1 → v2 migration: decision journal records become L3 intent nodes.

Trust is derived from v1 provenance: a hand-written YAML decision is a human
artifact (confirmed); git-mined records stay low-trust (mined); AI-suggested
records are asserted. The v1 record keeps existing — the node carries a
decision_id back-reference, not a copy of ownership.

Edge cases:
- Re-running migration: nodes already in the store are skipped (idempotent),
  so no duplicate journal lines are appended
- Two decisions with the same title: node IDs are discriminated by decision ID
- supersedes chains: the OLD decision's node is marked superseded_by the new
  node — after all ADD_NODE entries exist, so order never breaks the reference
- related_decisions pointing at a missing record: edge skipped with a warning
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from codebase_intel.core.types import DecisionStatus
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

if TYPE_CHECKING:
    from codebase_intel.decisions.models import DecisionRecord
    from codebase_intel.decisions.store import DecisionStore
    from codebase_intel.intentgraph.journal import IntentJournal
    from codebase_intel.intentgraph.store import IntentGraphStore

logger = logging.getLogger(__name__)

MIGRATION_ACTOR = "v1-migration"

_TRUST_BY_SOURCE = {
    "manual": TrustTier.CONFIRMED,
    "git-mined": TrustTier.MINED,
    "ai-suggested": TrustTier.ASSERTED,
}

_STATUS_MAP = {
    DecisionStatus.DRAFT: IntentNodeStatus.ACTIVE,
    DecisionStatus.ACTIVE: IntentNodeStatus.ACTIVE,
    DecisionStatus.SUPERSEDED: IntentNodeStatus.ACTIVE,  # flipped by SUPERSEDE entry below
    DecisionStatus.DEPRECATED: IntentNodeStatus.STALE,
    DecisionStatus.EXPIRED: IntentNodeStatus.STALE,
}


class MigrationResult:
    """Counts for reporting what the migration did."""

    def __init__(self) -> None:
        self.migrated = 0
        self.skipped = 0
        self.edges = 0
        self.superseded = 0
        self.warnings: list[str] = []


def decision_node_id(decision_id: str, title: str) -> str:
    """Deterministic node ID for a migrated decision."""
    return IntentNode.make_id(IntentLevel.DECISION, title, discriminator=decision_id)


def decision_to_node(record: DecisionRecord) -> IntentNode:
    """Map one v1 DecisionRecord onto an L3 intent node."""
    why = record.context.strip()
    if record.decision.strip():
        why = f"{why}\n\nDecision: {record.decision.strip()}" if why else record.decision.strip()

    return IntentNode(
        node_id=decision_node_id(record.id, record.title),
        level=IntentLevel.DECISION,
        title=record.title,
        why=why or record.title,
        status=_STATUS_MAP.get(record.status, IntentNodeStatus.ACTIVE),
        trust=_TRUST_BY_SOURCE.get(record.source, TrustTier.ASSERTED),
        confidence=record.confidence,
        anchors=record.code_anchors,
        decision_id=record.id,
        tags=record.tags,
        author=record.author,
        source="migration",
        created_at=record.created_at,
        updated_at=record.created_at,
    )


async def migrate_v1_decisions(
    decision_store: DecisionStore,
    journal: IntentJournal,
    graph_store: IntentGraphStore,
) -> MigrationResult:
    """Migrate all v1 decisions into the intent graph. Safe to re-run."""
    result = MigrationResult()
    records = await decision_store.load_all()
    node_id_by_decision = {r.id: decision_node_id(r.id, r.title) for r in records}

    for record in records:
        node = decision_to_node(record)
        if await graph_store.get_node(node.node_id) is not None:
            result.skipped += 1
            continue
        entry = JournalEntry.make(
            JournalOp.ADD_NODE,
            node.model_dump(mode="json"),
            actor=MIGRATION_ACTOR,
            ts=record.created_at,
        )
        journal.append(entry)
        result.migrated += 1

    # Materialize before pass 2: supersession/relation checks read node state
    await graph_store.materialize(journal)

    seen_edge_keys: set[str] = set()
    for record in records:
        source_node_id = node_id_by_decision[record.id]

        if record.supersedes:
            old_node_id = node_id_by_decision.get(record.supersedes)
            if old_node_id is None:
                result.warnings.append(
                    f"{record.id} supersedes unknown decision {record.supersedes}"
                )
            else:
                old_node = await graph_store.get_node(old_node_id)
                if old_node is not None and old_node.status != IntentNodeStatus.SUPERSEDED:
                    journal.append(
                        JournalEntry.make(
                            JournalOp.SUPERSEDE_NODE,
                            {
                                "node_id": old_node_id,
                                "superseded_by": source_node_id,
                                "reason": f"Superseded by {record.id}: {record.title}",
                            },
                            actor=MIGRATION_ACTOR,
                            ts=record.created_at,
                        )
                    )
                    result.superseded += 1

        existing_keys = {e.edge_key for e in await graph_store.edges_from(source_node_id)}
        for related_id in record.related_decisions:
            target_node_id = node_id_by_decision.get(related_id)
            if target_node_id is None:
                result.warnings.append(f"{record.id} relates to unknown decision {related_id}")
                continue
            if target_node_id == source_node_id:
                continue
            edge = IntentEdge(
                source_id=source_node_id,
                target_id=target_node_id,
                kind=IntentEdgeKind.RELATES_TO,
                created_at=record.created_at,
            )
            if edge.edge_key in existing_keys or edge.edge_key in seen_edge_keys:
                continue
            seen_edge_keys.add(edge.edge_key)
            journal.append(
                JournalEntry.make(
                    JournalOp.ADD_EDGE,
                    edge.model_dump(mode="json"),
                    actor=MIGRATION_ACTOR,
                    ts=record.created_at,
                )
            )
            result.edges += 1

    await graph_store.materialize(journal)
    if result.warnings:
        logger.warning("Migration completed with %d warnings", len(result.warnings))
    return result
