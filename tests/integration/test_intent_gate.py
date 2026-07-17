"""Integration tests for the pre-commit intent gate against a real git repo.

The T-003 verify bar:
- structural staged change without a why → warning + missing-why backlog node
- mechanical staged change → silent pass
- why staged alongside the change → covered, no backlog
- hard mode → gate fails the commit
- not a git repo → gate stays out of the way
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from git import Repo

from codebase_intel.core.config import IntentGraphConfig
from codebase_intel.intentgraph.capture import MISSING_WHY_TAG, CaptureService
from codebase_intel.intentgraph.gate import run_gate
from codebase_intel.intentgraph.journal import IntentJournal
from codebase_intel.intentgraph.store import IntentGraphStore

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = Repo.init(tmp_path)
    with repo.config_writer() as cw:
        cw.set_value("user", "name", "test")
        cw.set_value("user", "email", "test@example.com")
    (tmp_path / "README.md").write_text("# demo\n")
    repo.index.add(["README.md"])
    repo.index.commit("initial")
    return tmp_path


@pytest.fixture
def ig_config(git_repo: Path) -> IntentGraphConfig:
    config = IntentGraphConfig()
    # Mirror ProjectConfig.resolve_relative_paths for a standalone config
    object.__setattr__(config, "journal_dir", git_repo / ".codebase-intel" / "intent-journal")
    object.__setattr__(config, "db_path", git_repo / ".codebase-intel" / "intent-graph.db")
    return config


def _stage(repo_root: Path, rel_path: str, content: str) -> None:
    target = repo_root / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    Repo(repo_root).index.add([rel_path])


class TestGate:
    async def test_structural_without_why_creates_backlog(
        self, git_repo: Path, ig_config: IntentGraphConfig
    ) -> None:
        _stage(git_repo, "src/billing/refunds.py", "def refund(order_id: int) -> None: ...\n")

        result = await run_gate(git_repo, ig_config)

        assert result.passed  # soft mode never blocks
        assert result.significant
        assert not result.covered
        assert result.backlog_node_id is not None
        assert any("no recorded why" in m for m in result.messages)

        async with IntentGraphStore.open(ig_config.db_path) as store:
            node = await store.get_node(result.backlog_node_id)
        assert node is not None
        assert MISSING_WHY_TAG in node.tags

    async def test_mechanical_change_is_silent(
        self, git_repo: Path, ig_config: IntentGraphConfig
    ) -> None:
        _stage(git_repo, "README.md", "# demo\n\nMore docs.\n")

        result = await run_gate(git_repo, ig_config)

        assert result.passed
        assert not result.significant
        assert result.messages == []
        assert result.backlog_node_id is None

    async def test_staged_journal_counts_as_covered(
        self, git_repo: Path, ig_config: IntentGraphConfig
    ) -> None:
        journal = IntentJournal(ig_config.journal_dir)
        capture = CaptureService(journal, ig_config.db_path, actor="test")
        await capture.record_why(title="Refund module", why="billing needs isolated refund path")
        repo = Repo(git_repo)
        journal_rel = journal.active_path.relative_to(git_repo).as_posix()
        repo.index.add([journal_rel])
        _stage(git_repo, "src/billing/refunds.py", "def refund(order_id: int) -> None: ...\n")

        result = await run_gate(git_repo, ig_config)

        assert result.passed
        assert result.significant
        assert result.covered
        assert result.backlog_node_id is None

    async def test_existing_anchor_counts_as_covered(
        self, git_repo: Path, ig_config: IntentGraphConfig
    ) -> None:
        journal = IntentJournal(ig_config.journal_dir)
        capture = CaptureService(journal, ig_config.db_path, actor="test")
        await capture.record_why(
            title="Billing module design",
            why="refund flow is isolated for auditability",
            file_paths=[str(git_repo / "src" / "billing")],
        )
        _stage(git_repo, "src/billing/refunds.py", "def refund(order_id: int) -> None: ...\n")

        result = await run_gate(git_repo, ig_config)

        assert result.passed
        assert result.covered

    async def test_hard_mode_blocks(self, git_repo: Path, ig_config: IntentGraphConfig) -> None:
        _stage(git_repo, "src/billing/refunds.py", "def refund(order_id: int) -> None: ...\n")

        result = await run_gate(git_repo, ig_config, hard=True)

        assert not result.passed
        assert any("blocked" in m for m in result.messages)

    async def test_not_a_git_repo_skips(self, tmp_path: Path) -> None:
        config = IntentGraphConfig()
        object.__setattr__(config, "journal_dir", tmp_path / "j")
        object.__setattr__(config, "db_path", tmp_path / "db.sqlite")

        result = await run_gate(tmp_path / "nowhere", config)

        assert result.passed
        assert any("skipped" in m for m in result.messages)
