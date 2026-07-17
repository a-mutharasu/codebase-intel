"""Append-only intent journal — the source of truth for the intent graph.

The journal is plain-text JSONL committed to the repo (git's objects-vs-index
model): the SQLite store is a derived, rebuildable index. An append IS the
commit point — a crash after append loses nothing; materialization replays.

Edge cases:
- Crash mid-write: a partial trailing line is detected and skipped with a
  warning, never a hard failure (the entry was not committed)
- Malformed line mid-file (bad merge resolution): skipped, reported, replay continues
- Multiple journal files: compaction writes archive-*.jsonl; files are read in
  sorted name order, and 'archive-*' sorts before 'journal', preserving order
- Concurrent appends from two processes: single-line O_APPEND writes on POSIX
  do not interleave at the sizes we write; fsync makes them durable
"""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING

from pydantic import ValidationError

from codebase_intel.intentgraph.models import JournalEntry

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

JOURNAL_FILENAME = "journal.jsonl"


class ReadResult:
    """Entries plus the errors encountered while reading them.

    Errors never abort a read — a partially readable journal must still
    materialize (degradation over failure).
    """

    def __init__(self, entries: list[JournalEntry], errors: list[str]) -> None:
        self.entries = entries
        self.errors = errors


class IntentJournal:
    """Reads and appends the append-only intent journal."""

    def __init__(self, journal_dir: Path) -> None:
        self._journal_dir = journal_dir

    @property
    def journal_dir(self) -> Path:
        return self._journal_dir

    @property
    def active_path(self) -> Path:
        return self._journal_dir / JOURNAL_FILENAME

    def append(self, entry: JournalEntry) -> None:
        """Durably append one entry. This is the capture commit point.

        fsync before returning: NFR-R1 says no captured why is ever lost to a
        crash, so we pay the sync cost here rather than risk the page cache.
        """
        self._journal_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
        with self.active_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def read_all(self) -> ReadResult:
        """Read every entry from all journal files in order.

        Returns entries in file-order (append order within a file, sorted
        filename order across files) plus any per-line errors.
        """
        entries: list[JournalEntry] = []
        errors: list[str] = []

        if not self._journal_dir.exists():
            return ReadResult(entries, errors)

        for path in sorted(self._journal_dir.glob("*.jsonl")):
            entries.extend(self._read_file(path, errors))

        return ReadResult(entries, errors)

    def _read_file(self, path: Path, errors: list[str]) -> list[JournalEntry]:
        entries: list[JournalEntry] = []
        try:
            raw_lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            errors.append(f"{path.name}: unreadable ({exc})")
            logger.warning("Cannot read journal file %s: %s", path, exc)
            return entries

        for lineno, line in enumerate(raw_lines, start=1):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
                entries.append(JournalEntry.model_validate(data))
            except (json.JSONDecodeError, ValidationError) as exc:
                is_last = lineno == len(raw_lines)
                if is_last:
                    # Torn trailing line = crash mid-append; the entry never committed.
                    logger.warning(
                        "Skipping partial trailing line in %s (crash mid-write?)", path.name
                    )
                else:
                    errors.append(f"{path.name}:{lineno}: {exc}")
                    logger.warning("Skipping malformed journal line %s:%d", path.name, lineno)

        return entries
