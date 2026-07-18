"""Naive-read-cost baseline — what an agent without any tooling would read.

Shared by analytics/benchmark.py (v1 code-graph A/B) and analytics/eval.py
(v2 intent-graph A/B) so both harnesses measure "naive" the same way and their
numbers stay comparable.

Heuristic: agents without context tools typically read the target file's own
directory plus its parent directory, grepping for anything nearby. We simulate
that by reading every source file two directory levels up from each target.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from codebase_intel.core.tokens import estimate_tokens

if TYPE_CHECKING:
    from pathlib import Path

CODE_EXTENSIONS = {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".rb"}


def estimate_naive_read_cost(target_paths: list[Path]) -> tuple[int, int]:
    """Return (tokens, file_count) for reading around each target path's neighborhood.

    Edge cases:
    - Missing/unreadable files: skipped, never raises (naive cost is still estimable
      from whatever siblings do exist)
    - No target paths: (0, 0)
    """
    content = ""
    files: set[Path] = set()

    for fp in target_paths:
        for ancestor_dir in [fp.parent, fp.parent.parent]:
            if not ancestor_dir.exists():
                continue
            try:
                for child in ancestor_dir.iterdir():
                    if child.is_file() and child.suffix in CODE_EXTENSIONS and child not in files:
                        files.add(child)
                        with contextlib.suppress(OSError):
                            content += child.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                pass

    return estimate_tokens(content), len(files)
