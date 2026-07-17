"""Token estimation shared by the orchestrator and the intent graph.

Hoisted from orchestrator/assembler.py so lower layers (intentgraph) can
budget-pack without importing upward through the orchestrator.
"""

from __future__ import annotations

import tiktoken

# cl100k_base is used by GPT-4 and is a reasonable baseline for Claude too
_encoder: tiktoken.Encoding | None = None


def _get_encoder() -> tiktoken.Encoding:
    global _encoder
    if _encoder is None:
        _encoder = tiktoken.get_encoding("cl100k_base")
    return _encoder


def estimate_tokens(text: str) -> int:
    """Estimate token count for a text string.

    Edge case: different models tokenize differently. cl100k_base is
    a reasonable approximation. The TokenBudget.safety_margin_pct
    accounts for variance (typically 10%).

    Edge case: empty string → 0 tokens (valid).
    Edge case: non-ASCII text → may tokenize differently, but the
    safety margin covers this.
    """
    if not text:
        return 0
    return len(_get_encoder().encode(text))
