"""Pre-commit intent gate — the deterministic layer of the capture net.

MCP is pull-only: nothing can force an agent to record a why. This gate is the
agent-independent backstop. Soft by default (warn + leave a missing-why backlog
node); hard mode (block the commit) is a per-repo opt-in — a gate that blocks
hotfixes by default gets uninstalled (V2-PLAN.md D1).

Edge cases:
- Not a git repo / git unavailable: gate passes silently — it must never make
  committing impossible
- Why already recorded: satisfied either by journal entries staged in this
  commit, or by an existing ACTIVE intent node anchored to a structural file
- Empty staged diff (e.g. `git commit --amend` of message only): silent pass
- The backlog node itself is journal-appended but not staged — it rides along
  with the next commit, which is acceptable for a soft gate
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from codebase_intel.intentgraph.capture import CaptureService
from codebase_intel.intentgraph.classifier import classify_diff
from codebase_intel.intentgraph.journal import IntentJournal
from codebase_intel.intentgraph.models import IntentNodeStatus
from codebase_intel.intentgraph.store import IntentGraphStore

if TYPE_CHECKING:
    from pathlib import Path

    from codebase_intel.core.config import IntentGraphConfig


def _resolve_under(root: Path, rel_path: str) -> str:
    """Sync path resolution — kept out of the async flow."""
    return (root / rel_path).resolve().as_posix()


class GateResult(BaseModel):
    """Outcome of one gate run."""

    model_config = ConfigDict(frozen=True)

    passed: bool  # False only in hard mode with an unexplained structural change
    significant: bool = False
    covered: bool = False  # a why exists (staged or already anchored)
    reasons: list[str] = Field(default_factory=list)
    messages: list[str] = Field(default_factory=list)
    backlog_node_id: str | None = None


def _staged_diff(repo_root: Path) -> tuple[str, list[str]] | None:
    """Return (diff text, staged paths), or None when git isn't usable."""
    try:
        from git import Repo

        repo = Repo(repo_root, search_parent_directories=True)
        diff = repo.git.diff("--cached")
        names = repo.git.diff("--cached", "--name-only").splitlines()
        return diff, [n for n in names if n.strip()]
    except Exception:
        return None


async def run_gate(
    project_root: Path,
    config: IntentGraphConfig,
    *,
    hard: bool = False,
) -> GateResult:
    """Run the pre-commit intent gate for the staged changes."""
    staged = _staged_diff(project_root)
    if staged is None:
        return GateResult(passed=True, messages=["git unavailable — gate skipped"])

    diff_text, staged_paths = staged
    verdict = classify_diff(diff_text)
    if not verdict.significant:
        return GateResult(passed=True, significant=False)

    journal = IntentJournal(config.journal_dir)

    # Coverage check 1: a why-entry is part of this very commit
    journal_rel_markers = ("intent-journal/",)
    journal_staged = any(
        any(marker in path for marker in journal_rel_markers) for path in staged_paths
    )

    # Coverage check 2: an ACTIVE non-backlog why already anchors a structural file
    anchored = False
    if not journal_staged:
        async with IntentGraphStore.open(config.db_path) as store:
            await store.materialize(journal)
            for rel_path in verdict.structural_files:
                nodes = await store.nodes_for_path(_resolve_under(project_root, rel_path))
                if any(
                    n.status == IntentNodeStatus.ACTIVE and "missing-why" not in n.tags
                    for n in nodes
                ):
                    anchored = True
                    break

    if journal_staged or anchored:
        return GateResult(passed=True, significant=True, covered=True, reasons=verdict.reasons)

    # Unexplained structural change — leave the honest gap-marker
    capture = CaptureService(journal, config.db_path, actor="intent-gate")
    summary = verdict.reasons[0] if verdict.reasons else "structural change"
    backlog = await capture.record_missing_why(verdict.structural_files, summary)

    messages = [
        "Structural change with no recorded why:",
        *[f"  - {r}" for r in verdict.reasons],
        f"Backlog node {backlog.node.node_id} created — backfill with record_why "
        "or `codebase-intel why`.",
    ]
    if hard:
        messages.append("Hard gate enabled: commit blocked until a why is recorded.")
    return GateResult(
        passed=not hard,
        significant=True,
        covered=False,
        reasons=verdict.reasons,
        messages=messages,
        backlog_node_id=backlog.node.node_id,
    )
