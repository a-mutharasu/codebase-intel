"""Intent graph models — the multi-level "why" layer over the code graph.

v2 core schema: every node carries a rationale captured at decision time, a
trust tier, and anchors into the code. Nodes are never hard-deleted —
supersession preserves the "why we changed our mind" trail.

Edge cases addressed in schema design:
- Node without anchors: valid for L0/L1 (system-level intent has no single code location)
- Same title captured twice: deterministic IDs collide → replay treats it as an
  upsert of one node, not two duplicate nodes
- Superseded node still referenced by CONTAINS edges: stays traversable, labeled
- Trust must be visible in every render: to_context_string always prints the tier
- Migrated v1 decisions: carry a decision_id back-reference and provenance-based trust
- Journal replay: entries are content-addressed so replaying twice is a no-op
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import IntEnum, StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from codebase_intel.core.types import CodeAnchor


class IntentLevel(IntEnum):
    """Abstraction level of an intent node.

    IntEnum (not str) because traversal needs ordering: drill_down moves to
    level+1, roll_up to level-1.
    """

    SYSTEM = 0  # L0 — system topology: "why 5 services?"
    SERVICE = 1  # L1 — service/module: "why does auth own session storage?"
    COMPONENT = 2  # L2 — component/flow: "why token families?"
    DECISION = 3  # L3 — function/decision: "why 3 retries with backoff?"


class TrustTier(StrEnum):
    """How much an agent should trust a recorded why.

    Surfaced in every rendered answer — a mined why must never read as fact.
    """

    MINED = "mined"  # Inferred from git/PR history — lowest trust
    DERIVED = "derived"  # Auto-summarized from child nodes — regenerable, low trust
    ASSERTED = "asserted"  # Written by an agent at decision time — unreviewed
    CONFIRMED = "confirmed"  # Reviewed/authored by a human

    @property
    def rank(self) -> int:
        """Numeric rank for relevance scoring (higher = more trustworthy)."""
        order = {
            TrustTier.MINED: 0,
            TrustTier.DERIVED: 1,
            TrustTier.ASSERTED: 2,
            TrustTier.CONFIRMED: 3,
        }
        return order[self]


class IntentNodeStatus(StrEnum):
    """Lifecycle of an intent node."""

    ACTIVE = "active"
    STALE = "stale"  # Anchored code changed under an intact why — needs re-affirmation
    SUPERSEDED = "superseded"  # Replaced by another node — kept for the trail


class IntentEdgeKind(StrEnum):
    """Typed relationships between intent nodes."""

    CONTAINS = "contains"  # Level N → level N+1 (downward hierarchy)
    DEPENDS_ON = "depends_on"  # This intent relies on another holding
    CONFLICTS_WITH = "conflicts_with"  # Contradictory constraints — flagged, not averaged
    RELATES_TO = "relates_to"  # Weak association (migrated related_decisions)


def _short_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class IntentNode(BaseModel):
    """A single node in the intent graph: one why, at one abstraction level.

    Edge cases:
    - why is required and non-empty — a node without a rationale is exactly the
      artifact this system exists to prevent
    - superseded_by must be set iff status is SUPERSEDED (validated both ways)
    - anchors may be empty for L0/L1; L3 nodes should anchor but it's not enforced
      at the model layer (capture must never fail on a missing anchor — it degrades)
    """

    model_config = ConfigDict(frozen=True)

    # --- Identity ---
    node_id: str = Field(description="Deterministic ID: hash of (level, title[, discriminator])")
    level: IntentLevel
    title: str = Field(min_length=1, description="Short name, e.g. 'Retry policy for Sarvam WS'")

    # --- The why ---
    why: str = Field(min_length=1, description="The rationale, captured at decision time")
    status: IntentNodeStatus = IntentNodeStatus.ACTIVE
    trust: TrustTier
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    # --- Code linking ---
    anchors: list[CodeAnchor] = Field(default_factory=list)

    # --- Relationships (non-edge) ---
    decision_id: str | None = Field(
        default=None,
        description="Back-reference to a v1 DecisionRecord (e.g. 'DEC-042')",
    )
    superseded_by: str | None = None
    supersede_reason: str | None = None
    tags: list[str] = Field(default_factory=list)

    # --- Provenance ---
    author: str = "unknown"
    source: str = Field(
        default="capture",
        description="How this node was created: capture, migration, mined, derived",
    )
    session: str | None = Field(
        default=None,
        description="Capture session tag — lets a future session trace who asserted this",
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("created_at", "updated_at")
    @classmethod
    def ensure_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            return v.replace(tzinfo=UTC)
        return v.astimezone(UTC)

    @model_validator(mode="after")
    def validate_supersession(self) -> Self:
        if self.superseded_by == self.node_id:
            msg = f"Intent node {self.node_id} cannot supersede itself"
            raise ValueError(msg)
        if (self.status == IntentNodeStatus.SUPERSEDED) != (self.superseded_by is not None):
            msg = (
                f"Node {self.node_id}: status SUPERSEDED and superseded_by must be set together "
                f"(status={self.status.value}, superseded_by={self.superseded_by})"
            )
            raise ValueError(msg)
        return self

    @staticmethod
    def make_id(level: IntentLevel, title: str, discriminator: str | None = None) -> str:
        """Deterministic node ID.

        Title is casefolded so re-capture with different casing upserts the same
        node. Migration passes the v1 decision ID as discriminator so two
        decisions with identical titles never collide.
        """
        raw = f"{int(level)}:{title.strip().casefold()}"
        if discriminator:
            raw = f"{raw}:{discriminator}"
        return _short_hash(raw)

    def to_context_string(self, verbose: bool = False) -> str:
        """Serialize for agent context. Trust tier and status are always shown."""
        level_name = f"L{int(self.level)}"
        lines = [
            f"## Intent: {self.title} [{level_name} · {self.node_id}]",
            f"Trust: {self.trust.value} · Status: {self.status.value}"
            f" · Confidence: {self.confidence:.0%}",
            "",
            f"**Why:** {self.why}",
        ]
        if self.status == IntentNodeStatus.SUPERSEDED:
            lines.append(
                f"**SUPERSEDED by {self.superseded_by}**"
                + (f": {self.supersede_reason}" if self.supersede_reason else "")
            )
        if self.status == IntentNodeStatus.STALE:
            lines.append("**WARNING: anchored code changed since this why was recorded**")
        if verbose and self.anchors:
            lines.append("")
            lines.append("**Anchors:**")
            for anchor in self.anchors:
                loc = str(anchor.file_path)
                if anchor.line_range:
                    loc += f":{anchor.line_range.start}-{anchor.line_range.end}"
                lines.append(f"- {loc}")
        if verbose and self.decision_id:
            lines.append(f"**Decision record:** {self.decision_id}")
        return "\n".join(lines)


class IntentEdge(BaseModel):
    """A directed, typed edge between two intent nodes.

    Edge case: self-edges are meaningless in every kind we support — rejected.
    """

    model_config = ConfigDict(frozen=True)

    source_id: str
    target_id: str
    kind: IntentEdgeKind
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("created_at")
    @classmethod
    def ensure_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            return v.replace(tzinfo=UTC)
        return v.astimezone(UTC)

    @model_validator(mode="after")
    def no_self_edges(self) -> Self:
        if self.source_id == self.target_id:
            msg = f"Self-edge not allowed: {self.source_id} -{self.kind.value}-> itself"
            raise ValueError(msg)
        return self

    @property
    def edge_key(self) -> str:
        """Natural key — one edge per (source, target, kind) triple."""
        return f"{self.source_id}:{self.target_id}:{self.kind.value}"


class JournalOp(StrEnum):
    """Operations recorded in the append-only intent journal.

    There is deliberately no DELETE — reversal is supersession or retraction,
    both of which keep the trail.
    """

    ADD_NODE = "add_node"
    UPDATE_NODE = "update_node"  # Status/trust/why changes on an existing node
    SUPERSEDE_NODE = "supersede_node"
    ADD_EDGE = "add_edge"
    RETRACT_EDGE = "retract_edge"  # Edge marked retracted, never removed


class JournalEntry(BaseModel):
    """One line in the append-only journal — the unit of truth.

    entry_id is content-addressed over (op, payload, ts, actor) so replaying the
    same journal into the index twice is a no-op (idempotent materialization).
    """

    model_config = ConfigDict(frozen=True)

    entry_id: str
    op: JournalOp
    payload: dict[str, Any]
    ts: datetime
    actor: str = "unknown"

    @field_validator("ts")
    @classmethod
    def ensure_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            return v.replace(tzinfo=UTC)
        return v.astimezone(UTC)

    @classmethod
    def make(
        cls,
        op: JournalOp,
        payload: dict[str, Any],
        actor: str = "unknown",
        ts: datetime | None = None,
    ) -> JournalEntry:
        """Build an entry with a content-addressed ID."""
        stamp = ts or datetime.now(UTC)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        canonical = json.dumps(
            {"op": op.value, "payload": payload, "ts": stamp.isoformat(), "actor": actor},
            sort_keys=True,
            default=str,
        )
        return cls(
            entry_id=_short_hash(canonical),
            op=op,
            payload=payload,
            ts=stamp,
            actor=actor,
        )
