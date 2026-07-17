"""Anchor honesty — validation, repair, divergence, conflicts, coverage.

"Every claim stays anchored to code": this module is what keeps the map from
becoming the Lucidchart rot it exists to replace. Anchors are validated by
content hash; moved files are re-anchored automatically when the evidence is
unambiguous, everything else is queued for review — never silently dropped.

Edge cases:
- Renamed/moved file: exact content-hash match at exactly one new path →
  auto-repair (high confidence)
- Duplicate content at several paths: ambiguous — queued, not guessed
- Split file (content hash gone entirely): queued for review; the node is
  marked stale + tagged so bootstrap surfaces the doubt
- Content changed in place: node marked stale (why may no longer hold)
- Anchor without a stored hash (pre-honesty captures): hash is backfilled on
  first validation instead of guessing at divergence
- Directory anchors: validated for existence only — directories have no hash
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import xxhash
from pydantic import BaseModel, ConfigDict, Field

from codebase_intel.core.types import CodeAnchor
from codebase_intel.intentgraph.models import (
    IntentNode,
    IntentNodeStatus,
    JournalEntry,
    JournalOp,
)

if TYPE_CHECKING:
    from codebase_intel.intentgraph.journal import IntentJournal
    from codebase_intel.intentgraph.store import IntentGraphStore

logger = logging.getLogger(__name__)

ORPHAN_TAG = "anchor-orphaned"

_IGNORE_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".codebase-intel",
    "dist",
    "build",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
}


def hash_file(path: Path) -> str | None:
    """xxhash of file bytes; None for missing files or directories."""
    try:
        if not path.is_file():
            return None
        return xxhash.xxh64(path.read_bytes()).hexdigest()
    except OSError:
        return None


def build_content_index(project_root: Path) -> dict[str, list[str]]:
    """Map content hash → resolved POSIX paths for every project file.

    Duplicate content maps to multiple paths — the repairer treats that as
    ambiguous rather than picking one.
    """
    index: dict[str, list[str]] = {}
    for path in project_root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in _IGNORE_DIRS for part in path.parts):
            continue
        digest = hash_file(path)
        if digest is not None:
            index.setdefault(digest, []).append(path.resolve().as_posix())
    return index


class AnchorFinding(BaseModel):
    """What happened to one anchor during validation."""

    model_config = ConfigDict(frozen=True)

    node_id: str
    anchor_path: str
    outcome: str  # valid | backfilled | repaired | diverged | queued
    detail: str = ""
    new_path: str | None = None


class RepairReport(BaseModel):
    """Full accounting of a validation run — nothing is silently dropped."""

    model_config = ConfigDict(frozen=True)

    nodes_checked: int = 0
    anchors_checked: int = 0
    valid: int = 0
    backfilled: int = 0
    repaired: int = 0
    diverged: int = 0
    queued: int = 0
    findings: list[AnchorFinding] = Field(default_factory=list)

    @property
    def auto_repair_rate(self) -> float:
        """repaired / (repaired + queued) — the ≥90% verify metric."""
        moved = self.repaired + self.queued
        return 1.0 if moved == 0 else self.repaired / moved


class AnchorRepairer:
    """Validates and repairs anchors for every non-superseded node."""

    def __init__(
        self, journal: IntentJournal, store: IntentGraphStore, actor: str = "honesty"
    ) -> None:
        self._journal = journal
        self._store = store
        self._actor = actor

    async def validate_and_repair(self, project_root: Path) -> RepairReport:
        """One full pass. Appends repair/staleness entries to the journal."""
        content_index = build_content_index(project_root)

        nodes_checked = 0
        counters = {"valid": 0, "backfilled": 0, "repaired": 0, "diverged": 0, "queued": 0}
        findings: list[AnchorFinding] = []
        anchors_checked = 0

        for node in await self._all_active_nodes():
            nodes_checked += 1
            if not node.anchors:
                continue

            new_anchors: list[CodeAnchor] = []
            node_diverged = False
            node_orphaned = False

            for anchor in node.anchors:
                anchors_checked += 1
                outcome = self._check_anchor(node, anchor, content_index, findings)
                counters[outcome.kind] += 1
                new_anchors.append(outcome.anchor)
                node_diverged = node_diverged or outcome.kind == "diverged"
                node_orphaned = node_orphaned or outcome.kind == "queued"

            await self._write_back(node, new_anchors, node_diverged, node_orphaned)

        await self._store.materialize(self._journal)
        return RepairReport(
            nodes_checked=nodes_checked,
            anchors_checked=anchors_checked,
            valid=counters["valid"],
            backfilled=counters["backfilled"],
            repaired=counters["repaired"],
            diverged=counters["diverged"],
            queued=counters["queued"],
            findings=findings,
        )

    async def _all_active_nodes(self) -> list[IntentNode]:
        from codebase_intel.intentgraph.models import IntentLevel

        nodes: list[IntentNode] = []
        for level in IntentLevel:
            nodes.extend(await self._store.nodes_at_level(level))
        return nodes

    class _Outcome:
        def __init__(self, kind: str, anchor: CodeAnchor) -> None:
            self.kind = kind
            self.anchor = anchor

    def _check_anchor(
        self,
        node: IntentNode,
        anchor: CodeAnchor,
        content_index: dict[str, list[str]],
        findings: list[AnchorFinding],
    ) -> AnchorRepairer._Outcome:
        path = anchor.file_path
        posix = path.as_posix()

        if path.is_dir():
            findings.append(
                AnchorFinding(
                    node_id=node.node_id,
                    anchor_path=posix,
                    outcome="valid",
                    detail="directory anchor — existence only",
                )
            )
            return self._Outcome("valid", anchor)

        current_hash = hash_file(path)

        if current_hash is not None:
            if anchor.content_hash is None:
                findings.append(
                    AnchorFinding(
                        node_id=node.node_id,
                        anchor_path=posix,
                        outcome="backfilled",
                        detail="hash recorded for future divergence checks",
                    )
                )
                return self._Outcome(
                    "backfilled",
                    CodeAnchor(
                        file_path=path,
                        line_range=anchor.line_range,
                        symbol_name=anchor.symbol_name,
                        content_hash=current_hash,
                    ),
                )
            if anchor.content_hash == current_hash:
                findings.append(
                    AnchorFinding(node_id=node.node_id, anchor_path=posix, outcome="valid")
                )
                return self._Outcome("valid", anchor)
            findings.append(
                AnchorFinding(
                    node_id=node.node_id,
                    anchor_path=posix,
                    outcome="diverged",
                    detail="content changed under an intact why — node marked stale",
                )
            )
            return self._Outcome(
                "diverged",
                CodeAnchor(
                    file_path=path,
                    line_range=anchor.line_range,
                    symbol_name=anchor.symbol_name,
                    content_hash=current_hash,
                ),
            )

        # File is gone — look for its content elsewhere
        candidates = content_index.get(anchor.content_hash or "", [])
        if len(candidates) == 1:
            new_path = candidates[0]
            findings.append(
                AnchorFinding(
                    node_id=node.node_id,
                    anchor_path=posix,
                    outcome="repaired",
                    detail="exact content match at new location",
                    new_path=new_path,
                )
            )
            return self._Outcome(
                "repaired",
                CodeAnchor(
                    file_path=Path(new_path),
                    line_range=anchor.line_range,
                    symbol_name=anchor.symbol_name,
                    content_hash=anchor.content_hash,
                ),
            )

        detail = (
            f"{len(candidates)} content matches — ambiguous, review needed"
            if candidates
            else "file gone, content not found (deleted or split) — review needed"
        )
        findings.append(
            AnchorFinding(node_id=node.node_id, anchor_path=posix, outcome="queued", detail=detail)
        )
        return self._Outcome("queued", anchor)  # keep the dead anchor — never silently drop

    async def _write_back(
        self,
        node: IntentNode,
        new_anchors: list[CodeAnchor],
        diverged: bool,
        orphaned: bool,
    ) -> None:
        anchors_changed = [a.model_dump(mode="json") for a in new_anchors] != [
            a.model_dump(mode="json") for a in node.anchors
        ]
        needs_stale = (diverged or orphaned) and node.status == IntentNodeStatus.ACTIVE
        needs_tag = orphaned and ORPHAN_TAG not in node.tags
        if not (anchors_changed or needs_stale or needs_tag):
            return

        changes: dict[str, object] = {}
        if anchors_changed:
            changes["anchors"] = [a.model_dump(mode="json") for a in new_anchors]
        if needs_stale:
            changes["status"] = IntentNodeStatus.STALE.value
        if needs_tag:
            changes["tags"] = [*node.tags, ORPHAN_TAG]

        self._journal.append(
            JournalEntry.make(
                JournalOp.UPDATE_NODE,
                {"node_id": node.node_id, "changes": changes},
                actor=self._actor,
            )
        )


async def find_conflicts(store: IntentGraphStore) -> list[tuple[str, str, str]]:
    """Potential conflicts: two ACTIVE same-level nodes anchoring the same file.

    Report-only heuristic — same file can host two orthogonal concerns, so
    humans/agents resolve; we just refuse to average the contradiction away.
    """
    from codebase_intel.intentgraph.models import IntentLevel

    conflicts: list[tuple[str, str, str]] = []
    for level in IntentLevel:
        nodes = await store.nodes_at_level(level)
        by_path: dict[str, list[IntentNode]] = {}
        for node in nodes:
            for anchor in node.anchors:
                by_path.setdefault(anchor.file_path.as_posix(), []).append(node)
        for path, sharing in by_path.items():
            unique = {n.node_id: n for n in sharing}.values()
            pairs = sorted(unique, key=lambda n: n.node_id)
            for i, a in enumerate(pairs):
                for b in list(pairs)[i + 1 :]:
                    conflicts.append(
                        (
                            a.node_id,
                            b.node_id,
                            f"both anchor {path} at L{int(level)} — check they address "
                            "different concerns",
                        )
                    )
    return conflicts


async def coverage_report(store: IntentGraphStore) -> dict[str, object]:
    """Health metrics: status counts, trust mix, backlog and orphan totals."""
    from codebase_intel.intentgraph.capture import MISSING_WHY_TAG
    from codebase_intel.intentgraph.models import IntentLevel

    by_status: dict[str, int] = await store.counts()
    by_trust: dict[str, int] = {}
    missing_why = 0
    orphaned = 0
    total = 0
    for level in IntentLevel:
        for node in await store.nodes_at_level(level, include_superseded=True):
            total += 1
            by_trust[node.trust.value] = by_trust.get(node.trust.value, 0) + 1
            if MISSING_WHY_TAG in node.tags:
                missing_why += 1
            if ORPHAN_TAG in node.tags:
                orphaned += 1

    stale = by_status.get("stale", 0)
    return {
        "total_nodes": total,
        "by_status": by_status,
        "by_trust": by_trust,
        "missing_why_backlog": missing_why,
        "orphaned_anchors": orphaned,
        "stale_pct": round(100 * stale / total, 1) if total else 0.0,
    }
