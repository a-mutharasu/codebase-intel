"""Version single-sourcing: pyproject reads it from __init__ via hatch, so they must agree."""

from __future__ import annotations

import re
from importlib.metadata import version

import codebase_intel

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def test_dunder_version_is_semver() -> None:
    assert SEMVER.match(codebase_intel.__version__)


def test_installed_metadata_matches_dunder_version() -> None:
    # A mismatch means the hatch dynamic-version wiring broke and PyPI would ship a
    # different number than get_status reports to agents.
    assert version("codebase-intel") == codebase_intel.__version__
