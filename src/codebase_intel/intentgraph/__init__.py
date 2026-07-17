"""Intent graph — the multi-level "why" layer (v2 core).

Journal-first architecture: the append-only JSONL journal in the repo is the
source of truth; the SQLite store is a derived, rebuildable query index.
"""

from codebase_intel.intentgraph.capture import CaptureResult, CaptureService
from codebase_intel.intentgraph.classifier import ChangeSignificance, classify_diff
from codebase_intel.intentgraph.gate import GateResult, run_gate
from codebase_intel.intentgraph.journal import IntentJournal
from codebase_intel.intentgraph.migrate import MigrationResult, migrate_v1_decisions
from codebase_intel.intentgraph.models import (
    IntentEdge,
    IntentEdgeKind,
    IntentLevel,
    IntentNode,
    IntentNodeStatus,
    JournalEntry,
    JournalOp,
    TrustTier,
)
from codebase_intel.intentgraph.store import IntentGraphStore, MaterializeResult

__all__ = [
    "CaptureResult",
    "CaptureService",
    "ChangeSignificance",
    "GateResult",
    "IntentEdge",
    "IntentEdgeKind",
    "IntentGraphStore",
    "IntentJournal",
    "IntentLevel",
    "IntentNode",
    "IntentNodeStatus",
    "JournalEntry",
    "JournalOp",
    "MaterializeResult",
    "MigrationResult",
    "TrustTier",
    "classify_diff",
    "migrate_v1_decisions",
    "run_gate",
]
