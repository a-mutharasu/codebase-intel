"""Tests for anchor honesty — validation, repair, divergence, conflicts, coverage.

Covers:
- Capture stores content hashes; legacy anchors get hashes backfilled
- Rename/move → auto-repair via exact content match (single candidate only)
- Content change in place → diverged, node marked stale
- Deleted/split content → queued for review, node stale + orphan tag, anchor kept
- Duplicate content at two paths → ambiguous, queued (never guessed)
- T-005 verify bar: refactor simulation (9 renames + 1 split) → ≥90% auto-repair,
  remainder queued, zero silent anchor loss
- Second run is stable (repairs don't re-fire)
- find_conflicts and coverage_report
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from codebase_intel.intentgraph.anchors import (
    ORPHAN_TAG,
    AnchorRepairer,
    build_content_index,
    coverage_report,
    find_conflicts,
    hash_file,
)
from codebase_intel.intentgraph.capture import CaptureService
from codebase_intel.intentgraph.journal import IntentJournal
from codebase_intel.intentgraph.models import IntentNodeStatus
from codebase_intel.intentgraph.store import IntentGraphStore

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    return tmp_path


@pytest.fixture
def journal(project: Path) -> IntentJournal:
    return IntentJournal(project / ".codebase-intel" / "intent-journal")


@pytest.fixture
def db_path(project: Path) -> Path:
    return project / ".codebase-intel" / "intent-graph.db"


@pytest.fixture
def capture(journal: IntentJournal, db_path: Path) -> CaptureService:
    return CaptureService(journal, db_path, actor="test")


def _write(project: Path, rel: str, content: str) -> Path:
    target = project / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return target


class TestHashing:
    async def test_capture_stores_content_hash(
        self, project: Path, capture: CaptureService, db_path: Path
    ) -> None:
        target = _write(project, "src/limiter.py", "bucket = True\n")
        result = await capture.record_why(
            title="Token bucket", why="O(1)", file_paths=[str(target)]
        )
        assert result.node.anchors[0].content_hash == hash_file(target)

    def test_hash_missing_file_is_none(self, project: Path) -> None:
        assert hash_file(project / "ghost.py") is None

    def test_content_index_skips_ignored_dirs(self, project: Path) -> None:
        _write(project, "src/a.py", "a = 1\n")
        _write(project, ".venv/lib/junk.py", "junk = 1\n")
        index = build_content_index(project)
        paths = [p for plist in index.values() for p in plist]
        assert any(p.endswith("src/a.py") for p in paths)
        assert not any(".venv" in p for p in paths)


async def _run_repair(
    journal: IntentJournal, db_path: Path, project: Path
) -> tuple[object, IntentGraphStore]:
    async with IntentGraphStore.open(db_path) as store:
        await store.materialize(journal)
        report = await AnchorRepairer(journal, store).validate_and_repair(project)
    return report, store


class TestRepair:
    async def test_rename_auto_repairs(
        self, project: Path, capture: CaptureService, journal: IntentJournal, db_path: Path
    ) -> None:
        target = _write(project, "src/limiter.py", "bucket = True\n")
        recorded = await capture.record_why(
            title="Token bucket", why="O(1)", file_paths=[str(target)]
        )
        target.rename(project / "src" / "rate_limiter.py")

        report, _ = await _run_repair(journal, db_path, project)
        assert report.repaired == 1
        assert report.queued == 0

        async with IntentGraphStore.open(db_path) as store:
            node = await store.get_node(recorded.node.node_id)
        assert node is not None
        assert node.status == IntentNodeStatus.ACTIVE
        assert node.anchors[0].file_path.name == "rate_limiter.py"

    async def test_content_change_marks_stale(
        self, project: Path, capture: CaptureService, journal: IntentJournal, db_path: Path
    ) -> None:
        target = _write(project, "src/limiter.py", "bucket = True\n")
        recorded = await capture.record_why(
            title="Token bucket", why="O(1)", file_paths=[str(target)]
        )
        target.write_text("window = 'sliding'\n")

        report, _ = await _run_repair(journal, db_path, project)
        assert report.diverged == 1

        async with IntentGraphStore.open(db_path) as store:
            node = await store.get_node(recorded.node.node_id)
        assert node is not None
        assert node.status == IntentNodeStatus.STALE

    async def test_deleted_file_queued_with_orphan_tag(
        self, project: Path, capture: CaptureService, journal: IntentJournal, db_path: Path
    ) -> None:
        target = _write(project, "src/limiter.py", "bucket = True\n")
        recorded = await capture.record_why(
            title="Token bucket", why="O(1)", file_paths=[str(target)]
        )
        target.unlink()

        report, _ = await _run_repair(journal, db_path, project)
        assert report.queued == 1
        assert report.repaired == 0

        async with IntentGraphStore.open(db_path) as store:
            node = await store.get_node(recorded.node.node_id)
        assert node is not None
        assert node.status == IntentNodeStatus.STALE
        assert ORPHAN_TAG in node.tags
        assert len(node.anchors) == 1  # dead anchor kept — zero silent loss

    async def test_duplicate_content_is_ambiguous(
        self, project: Path, capture: CaptureService, journal: IntentJournal, db_path: Path
    ) -> None:
        target = _write(project, "src/limiter.py", "shared = True\n")
        _write(project, "src/copy_a.py", "shared = True\n")
        _write(project, "src/copy_b.py", "shared = True\n")
        await capture.record_why(title="Token bucket", why="O(1)", file_paths=[str(target)])
        target.unlink()

        report, _ = await _run_repair(journal, db_path, project)
        assert report.queued == 1
        assert report.repaired == 0

    async def test_legacy_anchor_gets_hash_backfilled(
        self, project: Path, capture: CaptureService, journal: IntentJournal, db_path: Path
    ) -> None:
        target = _write(project, "src/limiter.py", "bucket = True\n")
        recorded = await capture.record_why(title="Token bucket", why="O(1)")
        # Simulate a pre-honesty anchor: path only, no hash
        from codebase_intel.intentgraph.models import JournalEntry, JournalOp

        journal.append(
            JournalEntry.make(
                JournalOp.UPDATE_NODE,
                {
                    "node_id": recorded.node.node_id,
                    "changes": {"anchors": [{"file_path": str(target)}]},
                },
            )
        )

        report, _ = await _run_repair(journal, db_path, project)
        assert report.backfilled == 1

        async with IntentGraphStore.open(db_path) as store:
            node = await store.get_node(recorded.node.node_id)
        assert node is not None
        assert node.anchors[0].content_hash == hash_file(project / "src" / "limiter.py")

    async def test_second_run_is_stable(
        self, project: Path, capture: CaptureService, journal: IntentJournal, db_path: Path
    ) -> None:
        target = _write(project, "src/limiter.py", "bucket = True\n")
        await capture.record_why(title="Token bucket", why="O(1)", file_paths=[str(target)])
        target.rename(project / "src" / "rate_limiter.py")

        first, _ = await _run_repair(journal, db_path, project)
        second, _ = await _run_repair(journal, db_path, project)

        assert first.repaired == 1
        assert second.repaired == 0
        assert second.valid >= 1


class TestRefactorSimulation:
    """T-005 verify bar: ≥90% auto-reanchor on a realistic refactor."""

    async def test_nine_renames_one_split(
        self, project: Path, capture: CaptureService, journal: IntentJournal, db_path: Path
    ) -> None:
        files = []
        for i in range(10):
            files.append(_write(project, f"src/mod_{i}.py", f"value_{i} = {i}\n" * 5))
            await capture.record_why(
                title=f"Module {i} design",
                why=f"module {i} exists for reason {i}",
                file_paths=[str(files[i])],
            )

        # Refactor: 9 renames/moves, 1 split into two new files
        for i in range(9):
            new_dir = project / "src" / "reorganized"
            new_dir.mkdir(exist_ok=True)
            files[i].rename(new_dir / f"renamed_{i}.py")
        split_content = files[9].read_text()
        half = len(split_content) // 2
        _write(project, "src/split_a.py", split_content[:half])
        _write(project, "src/split_b.py", split_content[half:])
        files[9].unlink()

        report, _ = await _run_repair(journal, db_path, project)

        assert report.repaired == 9
        assert report.queued == 1
        assert report.auto_repair_rate >= 0.9
        # Zero silent loss: every anchor accounted for in findings
        assert report.anchors_checked == 10
        accounted = report.repaired + report.queued + report.valid + report.diverged
        assert accounted == report.anchors_checked


class TestConflictsAndCoverage:
    async def test_shared_anchor_flags_conflict(
        self, project: Path, capture: CaptureService, journal: IntentJournal, db_path: Path
    ) -> None:
        target = _write(project, "src/auth.py", "auth = True\n")
        await capture.record_why(title="Session policy", why="a", file_paths=[str(target)])
        await capture.record_why(title="Token policy", why="b", file_paths=[str(target)])

        async with IntentGraphStore.open(db_path) as store:
            await store.materialize(journal)
            conflicts = await find_conflicts(store)
        assert len(conflicts) == 1
        assert "different concerns" in conflicts[0][2]

    async def test_coverage_report_shape(
        self, project: Path, capture: CaptureService, journal: IntentJournal, db_path: Path
    ) -> None:
        await capture.record_why(title="A", why="a")
        await capture.record_missing_why(["src/x.py"], "new source file: src/x.py")

        async with IntentGraphStore.open(db_path) as store:
            await store.materialize(journal)
            health = await coverage_report(store)

        assert health["total_nodes"] == 2
        assert health["missing_why_backlog"] == 1
        assert health["by_status"]["active"] == 2
        assert "mined" in health["by_trust"]
        assert "asserted" in health["by_trust"]
