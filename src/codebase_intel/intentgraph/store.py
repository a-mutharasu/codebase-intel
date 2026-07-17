"""Intent graph SQLite index — a derived, rebuildable view of the journal.

The journal (JSONL in git) is the source of truth; this store is the query
index. Deleting the DB and running rebuild() must always reproduce identical
state — nothing lives only here.

Edge cases handled:
- Replay idempotence: applied_entries table dedupes by content-addressed entry_id,
  so materializing the same journal twice is a no-op
- Entry referencing an unknown node (partial clone, bad merge): recorded as an
  error, replay continues — one bad entry degrades one node, never the graph
- Two ADD_NODE entries for the same node_id (re-capture of same title): last
  writer wins in journal order — an upsert, not a conflict
- Schema from a newer version of the tool: refuse with a clear message rather
  than silently misreading (StorageMigrationError)
- Concurrent writers (MCP server + git hook): WAL + busy timeout + one asyncio
  lock per process serializes writes; reads stay concurrent
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import aiosqlite

from codebase_intel.core.exceptions import ErrorContext, StorageMigrationError
from codebase_intel.intentgraph.models import (
    IntentEdge,
    IntentEdgeKind,
    IntentLevel,
    IntentNode,
    IntentNodeStatus,
    JournalEntry,
    JournalOp,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from codebase_intel.intentgraph.journal import IntentJournal

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL,
    migrated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Idempotent replay: an entry is applied at most once
CREATE TABLE IF NOT EXISTS applied_entries (
    entry_id TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Full node JSON is the record of truth for the row; columns exist for querying
CREATE TABLE IF NOT EXISTS intent_nodes (
    node_id TEXT PRIMARY KEY,
    level INTEGER NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    trust TEXT NOT NULL,
    decision_id TEXT,
    data_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_intent_nodes_level ON intent_nodes(level, status);

CREATE TABLE IF NOT EXISTS intent_edges (
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    retracted INTEGER NOT NULL DEFAULT 0,
    data_json TEXT NOT NULL,
    PRIMARY KEY (source_id, target_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_intent_edges_target ON intent_edges(target_id, kind);

-- Anchor paths denormalized for "why(file)" lookups
CREATE TABLE IF NOT EXISTS intent_anchors (
    node_id TEXT NOT NULL REFERENCES intent_nodes(node_id) ON DELETE CASCADE,
    file_path TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_intent_anchors_path ON intent_anchors(file_path);
"""


class MaterializeResult:
    """Outcome of replaying journal entries into the index."""

    def __init__(self) -> None:
        self.applied = 0
        self.skipped = 0
        self.errors: list[str] = []


class IntentGraphStore:
    """SQLite-backed index over the intent journal."""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db
        self._write_lock = asyncio.Lock()

    @classmethod
    @asynccontextmanager
    async def open(cls, db_path: Path) -> AsyncIterator[IntentGraphStore]:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA busy_timeout=5000")
            await db.execute("PRAGMA foreign_keys=ON")
            store = cls(db)
            await store._ensure_schema()
            yield store

    async def _ensure_schema(self) -> None:
        await self._db.executescript(SCHEMA_SQL)
        cursor = await self._db.execute("SELECT MAX(version) AS v FROM schema_version")
        row = await cursor.fetchone()
        current: int | None = row["v"] if row else None

        if current is None:
            await self._db.execute(
                "INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,)
            )
            await self._db.commit()
        elif current > SCHEMA_VERSION:
            raise StorageMigrationError(
                f"Intent index schema v{current} is newer than this tool supports "
                f"(v{SCHEMA_VERSION}) — update codebase-intel or run rebuild",
                ErrorContext(component="intentgraph", operation="ensure_schema"),
            )
        # current < SCHEMA_VERSION: forward migrations land here in future versions

    # ------------------------------------------------------------------
    # Materialization (journal → index)
    # ------------------------------------------------------------------

    async def materialize(self, journal: IntentJournal) -> MaterializeResult:
        """Replay all unapplied journal entries. Idempotent by entry_id."""
        read = journal.read_all()
        result = MaterializeResult()
        result.errors.extend(read.errors)

        async with self._write_lock:
            for entry in read.entries:
                try:
                    applied = await self._apply(entry)
                except Exception as exc:
                    result.errors.append(f"{entry.entry_id} ({entry.op.value}): {exc}")
                    logger.warning("Failed to apply journal entry %s: %s", entry.entry_id, exc)
                    continue
                if applied:
                    result.applied += 1
                else:
                    result.skipped += 1
            await self._db.commit()

        if result.errors:
            logger.warning(
                "Materialize finished with %d errors (applied=%d skipped=%d)",
                len(result.errors),
                result.applied,
                result.skipped,
            )
        return result

    async def rebuild(self, journal: IntentJournal) -> MaterializeResult:
        """Wipe the index and replay the full journal — the recovery path."""
        async with self._write_lock:
            await self._db.execute("DELETE FROM intent_anchors")
            await self._db.execute("DELETE FROM intent_edges")
            await self._db.execute("DELETE FROM intent_nodes")
            await self._db.execute("DELETE FROM applied_entries")
            await self._db.commit()
        return await self.materialize(journal)

    async def _apply(self, entry: JournalEntry) -> bool:
        """Apply one entry. Returns False if it was already applied."""
        cursor = await self._db.execute(
            "INSERT OR IGNORE INTO applied_entries (entry_id) VALUES (?)",
            (entry.entry_id,),
        )
        if cursor.rowcount == 0:
            return False

        if entry.op == JournalOp.ADD_NODE:
            await self._upsert_node(IntentNode.model_validate(entry.payload))
        elif entry.op == JournalOp.UPDATE_NODE:
            await self._update_node(entry)
        elif entry.op == JournalOp.SUPERSEDE_NODE:
            await self._supersede_node(entry)
        elif entry.op == JournalOp.ADD_EDGE:
            await self._upsert_edge(IntentEdge.model_validate(entry.payload), retracted=False)
        elif entry.op == JournalOp.RETRACT_EDGE:
            await self._retract_edge(entry)
        return True

    async def _upsert_node(self, node: IntentNode) -> None:
        await self._db.execute(
            """
            INSERT OR REPLACE INTO intent_nodes
                (node_id, level, title, status, trust, decision_id, data_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                node.node_id,
                int(node.level),
                node.title,
                node.status.value,
                node.trust.value,
                node.decision_id,
                node.model_dump_json(),
            ),
        )
        await self._db.execute("DELETE FROM intent_anchors WHERE node_id = ?", (node.node_id,))
        for anchor in node.anchors:
            await self._db.execute(
                "INSERT INTO intent_anchors (node_id, file_path) VALUES (?, ?)",
                (node.node_id, anchor.file_path.as_posix()),
            )

    async def _load_node_for_change(self, entry: JournalEntry) -> IntentNode:
        node_id = entry.payload.get("node_id")
        if not isinstance(node_id, str) or not node_id:
            msg = f"{entry.op.value} entry missing node_id"
            raise ValueError(msg)
        node = await self.get_node(node_id)
        if node is None:
            msg = f"{entry.op.value} references unknown node {node_id}"
            raise ValueError(msg)
        return node

    async def _update_node(self, entry: JournalEntry) -> None:
        node = await self._load_node_for_change(entry)
        changes_raw = entry.payload.get("changes")
        changes: dict[str, Any] = changes_raw if isinstance(changes_raw, dict) else {}
        # Identity and trail fields can't be patched — supersession is the only path
        for forbidden in ("node_id", "created_at", "superseded_by"):
            changes.pop(forbidden, None)
        merged = {**node.model_dump(), **changes, "updated_at": entry.ts}
        await self._upsert_node(IntentNode.model_validate(merged))

    async def _supersede_node(self, entry: JournalEntry) -> None:
        node = await self._load_node_for_change(entry)
        superseded_by = entry.payload.get("superseded_by")
        if not isinstance(superseded_by, str) or not superseded_by:
            msg = f"supersede_node entry for {node.node_id} missing superseded_by"
            raise ValueError(msg)
        merged = {
            **node.model_dump(),
            "status": IntentNodeStatus.SUPERSEDED,
            "superseded_by": superseded_by,
            "supersede_reason": entry.payload.get("reason"),
            "updated_at": entry.ts,
        }
        await self._upsert_node(IntentNode.model_validate(merged))

    async def _upsert_edge(self, edge: IntentEdge, *, retracted: bool) -> None:
        await self._db.execute(
            """
            INSERT OR REPLACE INTO intent_edges
                (source_id, target_id, kind, retracted, data_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                edge.source_id,
                edge.target_id,
                edge.kind.value,
                1 if retracted else 0,
                edge.model_dump_json(),
            ),
        )

    async def _retract_edge(self, entry: JournalEntry) -> None:
        await self._db.execute(
            "UPDATE intent_edges SET retracted = 1 "
            "WHERE source_id = ? AND target_id = ? AND kind = ?",
            (
                entry.payload.get("source_id"),
                entry.payload.get("target_id"),
                entry.payload.get("kind"),
            ),
        )

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_node(row: aiosqlite.Row) -> IntentNode:
        return IntentNode.model_validate(json.loads(row["data_json"]))

    @staticmethod
    def _row_to_edge(row: aiosqlite.Row) -> IntentEdge:
        return IntentEdge.model_validate(json.loads(row["data_json"]))

    async def get_node(self, node_id: str) -> IntentNode | None:
        cursor = await self._db.execute(
            "SELECT data_json FROM intent_nodes WHERE node_id = ?", (node_id,)
        )
        row = await cursor.fetchone()
        return self._row_to_node(row) if row else None

    async def nodes_at_level(
        self,
        level: IntentLevel,
        include_superseded: bool = False,
    ) -> list[IntentNode]:
        sql = "SELECT data_json FROM intent_nodes WHERE level = ?"
        if not include_superseded:
            sql += " AND status != 'superseded'"
        cursor = await self._db.execute(sql + " ORDER BY title", (int(level),))
        rows = await cursor.fetchall()
        return [self._row_to_node(r) for r in rows]

    async def children(self, node_id: str) -> list[IntentNode]:
        """Nodes this one CONTAINS — drill_down."""
        cursor = await self._db.execute(
            """
            SELECT n.data_json FROM intent_edges e
            JOIN intent_nodes n ON n.node_id = e.target_id
            WHERE e.source_id = ? AND e.kind = ? AND e.retracted = 0
            ORDER BY n.title
            """,
            (node_id, IntentEdgeKind.CONTAINS.value),
        )
        rows = await cursor.fetchall()
        return [self._row_to_node(r) for r in rows]

    async def parents(self, node_id: str) -> list[IntentNode]:
        """Nodes that CONTAIN this one — roll_up."""
        cursor = await self._db.execute(
            """
            SELECT n.data_json FROM intent_edges e
            JOIN intent_nodes n ON n.node_id = e.source_id
            WHERE e.target_id = ? AND e.kind = ? AND e.retracted = 0
            ORDER BY n.title
            """,
            (node_id, IntentEdgeKind.CONTAINS.value),
        )
        rows = await cursor.fetchall()
        return [self._row_to_node(r) for r in rows]

    async def edges_from(self, node_id: str) -> list[IntentEdge]:
        cursor = await self._db.execute(
            "SELECT data_json FROM intent_edges WHERE source_id = ? AND retracted = 0",
            (node_id,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_edge(r) for r in rows]

    async def nodes_for_path(self, file_path: str) -> list[IntentNode]:
        """All intent nodes anchored to a file — the why(file) primitive.

        Matches exact path or any anchor that is a parent directory of the file,
        so a node anchored to 'src/auth/' answers for 'src/auth/session.py'.
        """
        cursor = await self._db.execute(
            """
            SELECT DISTINCT n.data_json FROM intent_anchors a
            JOIN intent_nodes n ON n.node_id = a.node_id
            WHERE a.file_path = ?1 OR ?1 LIKE a.file_path || '/%'
            ORDER BY n.level
            """,
            (file_path,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_node(r) for r in rows]

    async def search(self, text: str, limit: int = 20) -> list[IntentNode]:
        """Substring search over title + why. Case-insensitive."""
        pattern = f"%{text}%"
        cursor = await self._db.execute(
            """
            SELECT data_json FROM intent_nodes
            WHERE title LIKE ?1 OR data_json LIKE ?1
            ORDER BY level LIMIT ?2
            """,
            (pattern, limit),
        )
        rows = await cursor.fetchall()
        return [self._row_to_node(r) for r in rows]

    async def counts(self) -> dict[str, int]:
        """Node counts by status — the coverage/decay metric primitive."""
        cursor = await self._db.execute(
            "SELECT status, COUNT(*) AS c FROM intent_nodes GROUP BY status"
        )
        rows = await cursor.fetchall()
        return {str(row["status"]): int(row["c"]) for row in rows}
