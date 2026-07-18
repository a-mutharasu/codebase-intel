"""Intent-graph eval harness — token AND correctness A/B against the v1 baseline.

analytics/benchmark.py measures v1's code-graph pipeline: naive file reads vs
graph-targeted vs full (graph + decisions + contracts) — tokens only. It has
no v2 intent-graph scenario and no correctness signal, so the vision's central
claim ("token AND correctness gains") stays unmeasured. This module closes
that gap.

Methodology — for each eligible intent node (ACTIVE, level COMPONENT or
DECISION, a `why` with enough content beyond its own title):
1. Synthesize a query from the node's WHY text with its own TITLE terms
   removed. This forces a real query — the harness must find the node from
   how its reasoning is described, not by echoing its name back at it. This
   is the same regression shape validated in test_bootstrap.py's
   TestTenCannedQuestions, generalized to run against any repo.
2. Run IntentBootstrapper.bootstrap(query) and check whether the expected
   node comes back — this is the correctness signal (accuracy@1 / accuracy@3).
3. Compare token cost: the winning citation's own token cost vs
   estimate_naive_read_cost() over the node's anchored files — the same
   naive-cost methodology analytics/benchmark.py uses, so the two harnesses'
   "naive" baseline means the same thing.

Edge cases:
- Empty or sparse graph: zero cases, explicit warning, never crashes —
  the eval must degrade gracefully like every other v2 tool
- Node has no anchors: naive_tokens is 0 for that case, excluded from the
  reduction/multiplier averages (can't compute a ratio against zero)
- Reproducibility: candidates are sorted by node_id before truncating to
  max_cases, so the same graph state always yields the same report
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from codebase_intel.analytics.naive import estimate_naive_read_cost
from codebase_intel.core.text import extract_terms

if TYPE_CHECKING:
    from pathlib import Path

    from codebase_intel.analytics.tracker import AnalyticsTracker
    from codebase_intel.intentgraph.bootstrap import IntentBootstrapper
    from codebase_intel.intentgraph.models import IntentNode

DEFAULT_MAX_CASES = 10
DEFAULT_BUDGET_TOKENS = 2000
MIN_DISTINGUISHING_TERMS = 3
QUERY_TERM_COUNT = 6


@dataclass
class IntentEvalCase:
    """One held-out question: can bootstrap find this node from its own why?"""

    node_id: str
    node_title: str
    query: str
    anchor_paths: list[Path]


@dataclass
class IntentEvalResult:
    """Outcome of running one case through the bootstrapper."""

    node_id: str
    node_title: str
    query: str
    found: bool
    rank: int | None
    correct: bool  # rank == 1
    top3: bool  # rank in {1, 2, 3}
    naive_tokens: int
    naive_files: int
    citation_tokens: int  # tokens of just the winning citation
    session_tokens: int  # tokens of the whole packed bootstrap response
    coverage: str

    @property
    def reduction_pct(self) -> float:
        if self.naive_tokens == 0 or self.citation_tokens == 0:
            return 0.0
        return (1 - self.citation_tokens / self.naive_tokens) * 100

    @property
    def multiplier(self) -> float:
        if self.citation_tokens == 0:
            return 0.0
        return self.naive_tokens / self.citation_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "node_title": self.node_title,
            "query": self.query,
            "found": self.found,
            "rank": self.rank,
            "correct": self.correct,
            "top3": self.top3,
            "naive_tokens": self.naive_tokens,
            "citation_tokens": self.citation_tokens,
            "session_tokens": self.session_tokens,
            "reduction_pct": round(self.reduction_pct, 1),
            "multiplier": round(self.multiplier, 1),
            "coverage": self.coverage,
        }


@dataclass
class IntentEvalReport:
    """Complete eval report for a project."""

    repo_name: str
    repo_path: str
    total_cases: int = 0
    results: list[IntentEvalResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def accuracy_at_1(self) -> float:
        if not self.results:
            return 0.0
        return sum(1 for r in self.results if r.correct) / len(self.results)

    @property
    def accuracy_at_3(self) -> float:
        if not self.results:
            return 0.0
        return sum(1 for r in self.results if r.top3) / len(self.results)

    @property
    def avg_reduction_pct(self) -> float:
        vals = [
            r.reduction_pct for r in self.results if r.naive_tokens > 0 and r.citation_tokens > 0
        ]
        return sum(vals) / max(len(vals), 1)

    @property
    def avg_multiplier(self) -> float:
        vals = [r.multiplier for r in self.results if r.citation_tokens > 0]
        return sum(vals) / max(len(vals), 1)

    def format_table(self) -> str:
        """Markdown table for README/reports."""
        lines = [
            f"## Intent eval: {self.repo_name}",
            "",
            f"**Cases:** {self.total_cases} · **Accuracy@1:** {self.accuracy_at_1:.0%} · "
            f"**Accuracy@3:** {self.accuracy_at_3:.0%}",
            "",
        ]
        if not self.results:
            lines.append(f"_{self.warnings[0] if self.warnings else 'No eligible cases.'}_")
            return "\n".join(lines)

        lines.extend(
            [
                "| Query (from why, title-terms held out) | Found | Rank | Naive | "
                "Citation | Reduction |",
                "|---|---|---:|---:|---:|---:|",
            ]
        )
        for r in self.results:
            found_mark = "✓" if r.correct else ("~" if r.found else "✗")
            lines.append(
                f"| {r.query[:60]} | {found_mark} | {r.rank or '-'} | {r.naive_tokens:,} | "
                f"{r.citation_tokens:,} | {r.reduction_pct:.0f}% |"
            )

        lines.append(
            f"| **Average** | | | | | **{self.avg_reduction_pct:.0f}% "
            f"({self.avg_multiplier:.1f}x)** |"
        )
        return "\n".join(lines)


class IntentEvalRunner:
    """Runs the intent-graph eval against a project's existing intent journal."""

    def __init__(self, project_root: Path) -> None:
        self._project_root = project_root

    async def run(
        self,
        tracker: AnalyticsTracker | None = None,
        max_cases: int = DEFAULT_MAX_CASES,
        budget_tokens: int = DEFAULT_BUDGET_TOKENS,
    ) -> IntentEvalReport:
        from codebase_intel.core.config import ProjectConfig
        from codebase_intel.intentgraph import (
            IntentBootstrapper,
            IntentGraphStore,
            IntentJournal,
            IntentLevel,
        )

        config = ProjectConfig(project_root=self._project_root)
        journal = IntentJournal(config.intentgraph.journal_dir)
        db_path = config.intentgraph.db_path

        report = IntentEvalReport(
            repo_name=self._project_root.name, repo_path=str(self._project_root)
        )

        async with IntentGraphStore.open(db_path) as store:
            await store.materialize(journal)
            eligible: list[IntentNode] = []
            for level in (IntentLevel.COMPONENT, IntentLevel.DECISION):
                eligible.extend(await store.nodes_at_level(level))

        cases = self._build_cases(eligible, max_cases)
        report.total_cases = len(cases)
        if not cases:
            report.warnings.append(
                "no eligible intent nodes (need ACTIVE component/decision nodes with a "
                "why that says more than the title) — run `codebase-intel why ...` first"
            )
            return report

        bootstrapper = IntentBootstrapper(journal, db_path)
        for case in cases:
            report.results.append(await self._run_case(bootstrapper, case, budget_tokens))

        if tracker:
            tracker.record_intent_eval(
                repo_name=report.repo_name,
                repo_path=report.repo_path,
                total_cases=report.total_cases,
                accuracy_at_1=report.accuracy_at_1,
                accuracy_at_3=report.accuracy_at_3,
                avg_reduction_pct=report.avg_reduction_pct,
                avg_multiplier=report.avg_multiplier,
                results=[r.to_dict() for r in report.results],
            )

        return report

    async def _run_case(
        self, bootstrapper: IntentBootstrapper, case: IntentEvalCase, budget_tokens: int
    ) -> IntentEvalResult:
        boot = await bootstrapper.bootstrap(case.query, budget_tokens=budget_tokens)
        relevant = [item for item in boot.items if item.role == "relevant"]
        rank = next(
            (idx + 1 for idx, item in enumerate(relevant) if item.node_id == case.node_id),
            None,
        )
        citation = next((item for item in relevant if item.node_id == case.node_id), None)
        naive_tokens, naive_files = estimate_naive_read_cost(case.anchor_paths)

        return IntentEvalResult(
            node_id=case.node_id,
            node_title=case.node_title,
            query=case.query,
            found=rank is not None,
            rank=rank,
            correct=rank == 1,
            top3=rank is not None and rank <= 3,
            naive_tokens=naive_tokens,
            naive_files=naive_files,
            citation_tokens=citation.tokens if citation else 0,
            session_tokens=boot.total_tokens,
            coverage=boot.coverage,
        )

    def _build_cases(self, nodes: list[IntentNode], max_cases: int) -> list[IntentEvalCase]:
        from codebase_intel.intentgraph import IntentNodeStatus

        candidates: list[IntentEvalCase] = []
        for node in nodes:
            if node.status != IntentNodeStatus.ACTIVE:
                continue
            title_terms = extract_terms(node.title)
            why_terms = sorted(extract_terms(node.why) - title_terms)
            if len(why_terms) < MIN_DISTINGUISHING_TERMS:
                continue
            query = "why " + " ".join(why_terms[:QUERY_TERM_COUNT])
            candidates.append(
                IntentEvalCase(
                    node_id=node.node_id,
                    node_title=node.title,
                    query=query,
                    anchor_paths=[a.file_path for a in node.anchors],
                )
            )

        candidates.sort(key=lambda c: c.node_id)  # deterministic across runs
        return candidates[:max_cases]
