"""Significance classifier — decides which changes demand a recorded why.

Capture fatigue is the #1 adoption killer (V2-PLAN.md hotspot list): if the
gate prompts on mechanical edits, it gets uninstalled. The classifier is
deliberately conservative — it flags structural signals only, and when in
doubt stays silent.

Pure functions over a unified diff string: no git access here, so the git
hook, tests, and future CI checks share one implementation.

Edge cases:
- Renames: git shows them as delete+add of similar paths — both flagged only
  if the file is source code (renaming docs is mechanical)
- Test/doc-only commits: never significant, regardless of content
- Diff of a binary file: "Binary files differ" lines carry no +/- content — ignored
- Empty diff: not significant
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

SOURCE_EXTENSIONS = {
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".go",
    ".rs",
    ".java",
    ".rb",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".cs",
    ".php",
    ".swift",
    ".kt",
    ".scala",
    ".lua",
    ".dart",
    ".ex",
    ".exs",
    ".hs",
}

DEPENDENCY_MANIFESTS = {
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "setup.py",
    "package.json",
    "go.mod",
    "cargo.toml",
    "pom.xml",
    "build.gradle",
    "gemfile",
    "composer.json",
    "mix.exs",
}

MECHANICAL_PATH_MARKERS = ("tests/", "test/", "docs/", "doc/", "examples/")

# Added-line patterns that indicate a public surface or policy change
_SIGNATURE_RE = re.compile(
    r"^\+\s*(def |class |async def |func |fn |public |interface |type \w+ struct)"
)
_SENSITIVE_RE = re.compile(
    r"retry|backoff|cache|ttl|auth|token|session|timeout|rate.?limit|circuit|lock|encrypt|"
    r"permission|migrat",
    re.IGNORECASE,
)

_DIFF_FILE_RE = re.compile(r"^\+\+\+ b/(.+)$")
_NEW_FILE_RE = re.compile(r"^new file mode")
_DELETED_FILE_RE = re.compile(r"^deleted file mode")


class ChangeSignificance(BaseModel):
    """Classifier verdict: does this diff need a recorded why?"""

    model_config = ConfigDict(frozen=True)

    significant: bool
    reasons: list[str] = Field(default_factory=list)
    structural_files: list[str] = Field(default_factory=list)


def _is_source_file(path: str) -> bool:
    lowered = path.lower()
    return any(lowered.endswith(ext) for ext in SOURCE_EXTENSIONS)


def _is_mechanical_path(path: str) -> bool:
    lowered = path.lower()
    if any(marker in lowered for marker in MECHANICAL_PATH_MARKERS):
        return True
    return lowered.endswith((".md", ".rst", ".txt", ".lock"))


def _is_dependency_manifest(path: str) -> bool:
    return path.rsplit("/", 1)[-1].lower() in DEPENDENCY_MANIFESTS


def classify_diff(diff_text: str) -> ChangeSignificance:
    """Classify a unified diff (e.g. `git diff --cached`) for significance."""
    if not diff_text.strip():
        return ChangeSignificance(significant=False)

    reasons: list[str] = []
    structural: list[str] = []
    current_file: str | None = None
    pending_new_file = False
    pending_deleted_file = False

    for line in diff_text.splitlines():
        if line.startswith("diff --git"):
            pending_new_file = False
            pending_deleted_file = False
            current_file = None
            continue
        if _NEW_FILE_RE.match(line):
            pending_new_file = True
            continue
        if _DELETED_FILE_RE.match(line):
            pending_deleted_file = True
            continue

        file_match = _DIFF_FILE_RE.match(line)
        if file_match:
            current_file = file_match.group(1)
            if _is_mechanical_path(current_file):
                current_file = None  # everything under this file is mechanical
            elif pending_new_file and _is_source_file(current_file):
                reasons.append(f"new source file: {current_file}")
                structural.append(current_file)
            elif _is_dependency_manifest(current_file):
                reasons.append(f"dependency manifest changed: {current_file}")
                structural.append(current_file)
            continue
        # Deleted files have '+++ /dev/null'; recover the path from the '---' side
        if line.startswith("--- a/") and pending_deleted_file:
            deleted = line[len("--- a/") :]
            if _is_source_file(deleted) and not _is_mechanical_path(deleted):
                reasons.append(f"source file deleted: {deleted}")
                structural.append(deleted)
            pending_deleted_file = False
            continue

        if current_file is None or not line.startswith("+") or line.startswith("+++"):
            continue
        if _is_source_file(current_file):
            if _SIGNATURE_RE.match(line):
                reason = f"public surface changed: {current_file}"
                if reason not in reasons:
                    reasons.append(reason)
                    structural.append(current_file)
            elif _SENSITIVE_RE.search(line):
                reason = f"sensitive logic touched: {current_file}"
                if reason not in reasons:
                    reasons.append(reason)
                    structural.append(current_file)

    # De-dup while preserving order
    unique_structural = list(dict.fromkeys(structural))

    return ChangeSignificance(
        significant=bool(reasons),
        reasons=reasons,
        structural_files=unique_structural,
    )
