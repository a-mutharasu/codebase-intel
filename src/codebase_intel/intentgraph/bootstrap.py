"""Session bootstrap — start an agent session from the intent graph, not file reads.

Given a task description and a token budget, returns a packed context of the
whys that matter: system orientation (L0/L1) first, then task-relevant nodes
scored by term overlap x trust x freshness x status. This is the "graph is the
interface" principle made concrete.

Degradation ladder (NFR-R2): intent graph → code graph → raw files. This module
covers the first rung and *reports* coverage; the MCP handler uses the coverage
signal to decide whether to fall through to the v1 code-graph assembler. Weak
intent coverage is a labeled answer, never an error.

Edge cases:
- Empty graph (day 1): empty items + explicit warning naming the next rung
- Budget too small for even the overview: highest-scored single node + truncated flag
- Superseded nodes: still surfaced when directly relevant (they are precedent),
  but heavily down-weighted so they never crowd out active intent
- Deleted index: store recreates schema and journal replays — self-healing
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from codebase_intel.core.tokens import estimate_tokens
from codebase_intel.intentgraph.models import (
    IntentLevel,
    IntentNode,
    IntentNodeStatus,
)
from codebase_intel.intentgraph.store import IntentGraphStore

if TYPE_CHECKING:
    from pathlib import Path

    from codebase_intel.intentgraph.journal import IntentJournal

_WORD_RE = re.compile(r"[a-z0-9_]+")

# Terms too generic to signal relevance on their own
_STOPWORDS = {
    "the",
    "a",
    "an",
    "to",
    "for",
    "of",
    "in",
    "on",
    "and",
    "or",
    "is",
    "are",
    "add",
    "fix",
    "make",
    "update",
    "change",
    "new",
    "with",
    "this",
    "that",
    "code",
    "file",
    "files",
    "implement",
    "why",
    "how",
    "what",
    "does",
    "do",
}

_STATUS_WEIGHT = {
    IntentNodeStatus.ACTIVE: 1.0,
    IntentNodeStatus.STALE: 0.6,
    IntentNodeStatus.SUPERSEDED: 0.35,
}


class BootstrapItem(BaseModel):
    """One packed context entry."""

    model_config = ConfigDict(frozen=True)

    node_id: str
    level: int
    role: str  # "orientation" | "relevant"
    score: float
    tokens: int
    content: str


class BootstrapResult(BaseModel):
    """Budget-packed intent context for a session start."""

    model_config = ConfigDict(frozen=True)

    items: list[BootstrapItem] = Field(default_factory=list)
    total_tokens: int = 0
    budget_tokens: int = 0
    truncated: bool = False
    dropped_count: int = 0
    coverage: str = "none"  # none | weak | good — drives the ladder fall-through
    warnings: list[str] = Field(default_factory=list)


def _terms(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(text.casefold()) if w not in _STOPWORDS and len(w) > 2}


def score_node(node: IntentNode, task_terms: set[str], now: datetime) -> float:
    """Relevance x trust x status x freshness. Zero when no term overlaps."""
    if not task_terms:
        return 0.0
    node_terms = _terms(f"{node.title} {node.why} {' '.join(node.tags)}")
    overlap = len(task_terms & node_terms) / len(task_terms)
    if overlap == 0.0:
        return 0.0

    trust_weight = 0.7 + 0.1 * node.trust.rank  # mined 0.7 … confirmed 1.0
    status_weight = _STATUS_WEIGHT[node.status]
    age_days = max(0.0, (now - node.updated_at).total_seconds() / 86400)
    # Fresh for a month, then linear decay to a floor — old intent is still intent
    freshness = 1.0 if age_days <= 30 else max(0.5, 1.0 - (age_days - 30) / 365)
    return overlap * trust_weight * status_weight * freshness


class IntentBootstrapper:
    """Packs the most relevant slice of the intent graph into a token budget."""

    def __init__(self, journal: IntentJournal, db_path: Path) -> None:
        self._journal = journal
        self._db_path = db_path

    async def bootstrap(self, task: str, budget_tokens: int = 4000) -> BootstrapResult:
        task_terms = _terms(task)
        now = datetime.now(UTC)
        warnings: list[str] = []

        async with IntentGraphStore.open(self._db_path) as store:
            materialize = await store.materialize(self._journal)
            if materialize.errors:
                warnings.append(f"{len(materialize.errors)} journal entries failed to apply")

            orientation = [
                *(await store.nodes_at_level(IntentLevel.SYSTEM)),
                *(await store.nodes_at_level(IntentLevel.SERVICE)),
            ]
            detail = [
                *(await store.nodes_at_level(IntentLevel.COMPONENT, include_superseded=True)),
                *(await store.nodes_at_level(IntentLevel.DECISION, include_superseded=True)),
            ]

        if not orientation and not detail:
            return BootstrapResult(
                budget_tokens=budget_tokens,
                coverage="none",
                warnings=[
                    *warnings,
                    "intent graph is empty — fall back to the code graph "
                    "(get_context) or raw file reads",
                ],
            )

        candidates: list[tuple[float, str, IntentNode]] = []
        for node in orientation:
            # Orientation is always eligible: sessions need the system shape
            candidates.append((score_node(node, task_terms, now) + 1.0, "orientation", node))
        for node in detail:
            score = score_node(node, task_terms, now)
            if score > 0:
                candidates.append((score, "relevant", node))

        candidates.sort(key=lambda c: -c[0])

        items: list[BootstrapItem] = []
        total = 0
        dropped = 0
        for score, role, node in candidates:
            content = node.to_context_string(verbose=(role == "relevant"))
            tokens = estimate_tokens(content)
            if total + tokens > budget_tokens:
                dropped += 1
                continue
            items.append(
                BootstrapItem(
                    node_id=node.node_id,
                    level=int(node.level),
                    role=role,
                    score=round(score, 4),
                    tokens=tokens,
                    content=content,
                )
            )
            total += tokens

        relevant_count = sum(1 for i in items if i.role == "relevant")
        coverage = "good" if relevant_count >= 2 else ("weak" if items else "none")
        if coverage != "good":
            warnings.append(
                "intent coverage is "
                + coverage
                + " for this task — corroborate with the code graph (get_context)"
            )

        return BootstrapResult(
            items=items,
            total_tokens=total,
            budget_tokens=budget_tokens,
            truncated=dropped > 0,
            dropped_count=dropped,
            coverage=coverage,
            warnings=warnings,
        )
