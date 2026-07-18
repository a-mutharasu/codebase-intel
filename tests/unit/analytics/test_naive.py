"""Tests for the shared naive-read-cost baseline."""

from __future__ import annotations

from typing import TYPE_CHECKING

from codebase_intel.analytics.naive import estimate_naive_read_cost

if TYPE_CHECKING:
    from pathlib import Path


class TestEstimateNaiveReadCost:
    def test_empty_input(self) -> None:
        tokens, files = estimate_naive_read_cost([])
        assert (tokens, files) == (0, 0)

    def test_reads_sibling_source_files(self, tmp_path: Path) -> None:
        pkg = tmp_path / "src" / "pkg"
        pkg.mkdir(parents=True)
        (pkg / "a.py").write_text("x = 1\n" * 20)
        (pkg / "b.py").write_text("y = 2\n" * 20)
        (pkg / "notes.md").write_text("not code\n")

        tokens, files = estimate_naive_read_cost([pkg / "a.py"])

        assert tokens > 0
        assert files == 2  # a.py + b.py, not the markdown file

    def test_missing_file_does_not_raise(self, tmp_path: Path) -> None:
        tokens, files = estimate_naive_read_cost([tmp_path / "ghost" / "nope.py"])
        assert (tokens, files) == (0, 0)

    def test_dedupes_across_targets(self, tmp_path: Path) -> None:
        pkg = tmp_path / "src"
        pkg.mkdir()
        (pkg / "shared.py").write_text("z = 3\n")

        _, files_one = estimate_naive_read_cost([pkg / "shared.py"])
        _, files_two = estimate_naive_read_cost([pkg / "shared.py", pkg / "shared.py"])

        assert files_one == files_two == 1
