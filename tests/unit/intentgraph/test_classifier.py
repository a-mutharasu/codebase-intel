"""Tests for the significance classifier.

Covers:
- New source file, deleted source file, dependency manifest → significant
- Public surface additions and sensitive-keyword lines → significant
- Test-only / doc-only / lockfile / empty diffs → NOT significant (fatigue guard)
- Binary diffs carry no content → ignored
"""

from __future__ import annotations

from codebase_intel.intentgraph.classifier import classify_diff


def _new_file_diff(path: str, body: str = "+x = 1") -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "index 0000000..abc1234\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        "@@ -0,0 +1,1 @@\n"
        f"{body}\n"
    )


def _modified_diff(path: str, added_lines: list[str]) -> str:
    added = "\n".join(added_lines)
    return (
        f"diff --git a/{path} b/{path}\n"
        "index abc1234..def5678 100644\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1,2 +1,4 @@\n"
        f"{added}\n"
    )


def _deleted_diff(path: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        "deleted file mode 100644\n"
        "index abc1234..0000000\n"
        f"--- a/{path}\n"
        "+++ /dev/null\n"
        "@@ -1,1 +0,0 @@\n"
        "-x = 1\n"
    )


class TestSignificant:
    def test_new_source_file(self) -> None:
        verdict = classify_diff(_new_file_diff("src/billing/refunds.py"))
        assert verdict.significant
        assert "src/billing/refunds.py" in verdict.structural_files
        assert any("new source file" in r for r in verdict.reasons)

    def test_deleted_source_file(self) -> None:
        verdict = classify_diff(_deleted_diff("src/billing/refunds.py"))
        assert verdict.significant
        assert any("deleted" in r for r in verdict.reasons)

    def test_dependency_manifest(self) -> None:
        verdict = classify_diff(_modified_diff("pyproject.toml", ['+redis = ">=5"']))
        assert verdict.significant
        assert any("dependency manifest" in r for r in verdict.reasons)

    def test_public_surface_added(self) -> None:
        verdict = classify_diff(
            _modified_diff("src/api.py", ["+def refund_order(order_id: int) -> None:"])
        )
        assert verdict.significant
        assert any("public surface" in r for r in verdict.reasons)

    def test_sensitive_keyword(self) -> None:
        verdict = classify_diff(
            _modified_diff("src/client.py", ["+    delay = backoff_base * 2 ** attempt"])
        )
        assert verdict.significant
        assert any("sensitive logic" in r for r in verdict.reasons)


class TestNotSignificant:
    def test_empty_diff(self) -> None:
        assert not classify_diff("").significant
        assert not classify_diff("   \n").significant

    def test_test_only_change(self) -> None:
        diff = _new_file_diff("tests/unit/test_refunds.py", "+def test_refund(): ...")
        assert not classify_diff(diff).significant

    def test_doc_only_change(self) -> None:
        diff = _modified_diff("README.md", ["+## New auth section with token details"])
        assert not classify_diff(diff).significant

    def test_lockfile_change(self) -> None:
        diff = _modified_diff("uv.lock", ["+version = 2"])
        assert not classify_diff(diff).significant

    def test_plain_body_edit(self) -> None:
        diff = _modified_diff("src/api.py", ["+    total = price * quantity"])
        assert not classify_diff(diff).significant

    def test_binary_diff(self) -> None:
        diff = (
            "diff --git a/assets/logo.png b/assets/logo.png\n"
            "index abc..def 100644\n"
            "Binary files a/assets/logo.png and b/assets/logo.png differ\n"
        )
        assert not classify_diff(diff).significant
