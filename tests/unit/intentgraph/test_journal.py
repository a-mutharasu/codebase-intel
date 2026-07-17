"""Tests for the append-only intent journal.

Covers:
- Append/read round-trip and directory auto-creation
- Missing directory → empty result, not an error
- Crash mid-write simulation: partial trailing line skipped silently
- Malformed mid-file line: skipped, reported in errors, rest still read
- Multi-file ordering: archive-*.jsonl replays before journal.jsonl
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from codebase_intel.intentgraph.journal import IntentJournal
from codebase_intel.intentgraph.models import JournalEntry, JournalOp

if TYPE_CHECKING:
    from pathlib import Path


def _entry(marker: int) -> JournalEntry:
    return JournalEntry.make(JournalOp.ADD_NODE, {"marker": marker}, actor="test")


class TestAppendRead:
    def test_round_trip(self, tmp_path: Path) -> None:
        journal = IntentJournal(tmp_path / "journal")
        first, second = _entry(1), _entry(2)
        journal.append(first)
        journal.append(second)

        result = journal.read_all()
        assert result.errors == []
        assert [e.entry_id for e in result.entries] == [first.entry_id, second.entry_id]
        assert result.entries[0].payload == {"marker": 1}

    def test_append_creates_directory(self, tmp_path: Path) -> None:
        journal = IntentJournal(tmp_path / "does" / "not" / "exist")
        journal.append(_entry(1))
        assert journal.active_path.exists()

    def test_missing_directory_reads_empty(self, tmp_path: Path) -> None:
        journal = IntentJournal(tmp_path / "nope")
        result = journal.read_all()
        assert result.entries == []
        assert result.errors == []


class TestCrashTolerance:
    def test_partial_trailing_line_skipped_without_error(self, tmp_path: Path) -> None:
        journal = IntentJournal(tmp_path)
        good = _entry(1)
        journal.append(good)
        # Simulate a crash mid-append: torn, uncommitted trailing line
        with journal.active_path.open("a", encoding="utf-8") as fh:
            fh.write('{"entry_id": "torn", "op": "add_no')

        result = journal.read_all()
        assert [e.entry_id for e in result.entries] == [good.entry_id]
        assert result.errors == []

    def test_malformed_middle_line_reported_but_rest_read(self, tmp_path: Path) -> None:
        journal = IntentJournal(tmp_path)
        first, last = _entry(1), _entry(2)
        journal.append(first)
        with journal.active_path.open("a", encoding="utf-8") as fh:
            fh.write("not json at all\n")
        journal.append(last)

        result = journal.read_all()
        assert [e.entry_id for e in result.entries] == [first.entry_id, last.entry_id]
        assert len(result.errors) == 1
        assert ":2:" in result.errors[0]

    def test_blank_lines_ignored(self, tmp_path: Path) -> None:
        journal = IntentJournal(tmp_path)
        entry = _entry(1)
        journal.append(entry)
        with journal.active_path.open("a", encoding="utf-8") as fh:
            fh.write("\n\n")
        journal.append(_entry(2))

        result = journal.read_all()
        assert len(result.entries) == 2
        assert result.errors == []


class TestMultiFileOrdering:
    def test_archive_files_read_before_active_journal(self, tmp_path: Path) -> None:
        journal = IntentJournal(tmp_path)
        archived, active = _entry(1), _entry(2)
        archive_line = json.dumps(archived.model_dump(mode="json"), sort_keys=True)
        (tmp_path / "archive-0001.jsonl").write_text(archive_line + "\n", encoding="utf-8")
        journal.append(active)

        result = journal.read_all()
        assert [e.payload["marker"] for e in result.entries] == [1, 2]
