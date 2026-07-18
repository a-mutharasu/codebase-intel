"""Tests for the intent-graph eval harness — token AND correctness A/B (T-006).

Covers:
- Empty graph: zero cases, explicit warning, no crash (degradation)
- Query synthesis holds out title terms, so a title-echo can't trivially pass
- End-to-end: capture real intent nodes in a scratch project, run the eval,
  and assert the harness finds the right node and reports a token reduction
- Node with no anchors: naive_tokens 0, excluded from reduction/multiplier averages
- Reproducibility: same graph state -> same case selection and ordering
- Tracker round-trip: record_intent_eval / get_intent_eval_results
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from codebase_intel.analytics.eval import IntentEvalRunner
from codebase_intel.analytics.tracker import AnalyticsTracker
from codebase_intel.core.config import ProjectConfig
from codebase_intel.intentgraph import CaptureService, IntentJournal, IntentLevel

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    return tmp_path


@pytest.fixture
def config(project: Path) -> ProjectConfig:
    return ProjectConfig(project_root=project)


@pytest.fixture
def capture(config: ProjectConfig) -> CaptureService:
    journal = IntentJournal(config.intentgraph.journal_dir)
    return CaptureService(journal, config.intentgraph.db_path, actor="test")


def _write(project: Path, rel: str, content: str) -> Path:
    target = project / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return target


class TestEmptyGraph:
    async def test_no_cases_reports_warning(self, project: Path) -> None:
        report = await IntentEvalRunner(project).run()

        assert report.total_cases == 0
        assert report.results == []
        assert report.warnings
        assert report.accuracy_at_1 == 0.0
        assert report.avg_reduction_pct == 0.0

    async def test_title_only_node_is_not_eligible(
        self, project: Path, capture: CaptureService
    ) -> None:
        # why == title (no extra content) can't produce a distinguishing query
        await capture.record_why(title="Token bucket", why="Token bucket")

        report = await IntentEvalRunner(project).run()

        assert report.total_cases == 0


class TestEndToEnd:
    async def test_finds_correct_node_and_reports_reduction(
        self, project: Path, capture: CaptureService
    ) -> None:
        target = _write(project, "src/limiter.py", "class TokenBucket: ...\n" * 30)
        recorded = await capture.record_why(
            title="Token bucket rate limiting",
            why=(
                "Redis token bucket gives O(1) refill operations, unlike a sliding "
                "window which needs per-key sorted sets and costs more at scale"
            ),
            file_paths=[str(target)],
        )
        # A decoy with unrelated content — the harness must not just return "the only node"
        await capture.record_why(
            title="Session storage",
            why="Sessions live in Redis with a short TTL to bound memory growth",
        )

        report = await IntentEvalRunner(project).run(max_cases=5, budget_tokens=2000)

        assert report.total_cases >= 1
        matched = next(r for r in report.results if r.node_id == recorded.node.node_id)
        assert matched.found
        assert matched.correct
        assert matched.naive_tokens > 0
        assert matched.citation_tokens > 0
        assert matched.reduction_pct > 0
        assert report.accuracy_at_1 > 0
        table = report.format_table()
        assert "Intent eval" in table
        assert "Reduction" in table

    async def test_query_holds_out_title_terms(
        self, project: Path, capture: CaptureService
    ) -> None:
        await capture.record_why(
            title="Token bucket rate limiting",
            why="token bucket rate limiting uses redis atomic incrby with expiry",
        )

        report = await IntentEvalRunner(project).run()

        assert report.total_cases >= 1
        query_words = set(report.results[0].query.split())
        assert "token" not in query_words
        assert "bucket" not in query_words
        assert "rate" not in query_words
        assert "limiting" not in query_words

    async def test_node_without_anchors_excluded_from_reduction_average(
        self, project: Path, capture: CaptureService
    ) -> None:
        # No file_paths -> no anchors -> naive_tokens will be 0 for this case
        await capture.record_why(
            title="Org-wide Python version policy",
            why="Python 3.11 chosen for the exception-group syntax needed by the retry layer",
        )

        report = await IntentEvalRunner(project).run()

        assert report.total_cases >= 1
        assert report.results[0].naive_tokens == 0
        # Excluded from the average, not counted as a zero — average stays well-defined
        assert report.avg_reduction_pct == 0.0

    async def test_reproducible_case_selection(
        self, project: Path, capture: CaptureService
    ) -> None:
        for i in range(4):
            await capture.record_why(
                title=f"Decision {i}",
                why=f"reasoning number {i} covers latency budget constraint edge case",
            )

        first = await IntentEvalRunner(project).run(max_cases=2)
        second = await IntentEvalRunner(project).run(max_cases=2)

        assert [r.node_id for r in first.results] == [r.node_id for r in second.results]

    async def test_component_level_node_eligible(
        self, project: Path, capture: CaptureService
    ) -> None:
        await capture.record_why(
            title="Token family rotation",
            why="refresh token reuse detection requires tracking a family id per session",
            level=IntentLevel.COMPONENT,
        )

        report = await IntentEvalRunner(project).run()
        assert report.total_cases == 1


class TestTrackerRoundTrip:
    async def test_record_and_retrieve(self, project: Path, capture: CaptureService) -> None:
        target = _write(project, "src/limiter.py", "class TokenBucket: ...\n" * 10)
        await capture.record_why(
            title="Token bucket rate limiting",
            why="redis atomic incrby beats sliding window sorted sets at scale",
            file_paths=[str(target)],
        )

        tracker = AnalyticsTracker(project / ".codebase-intel" / "analytics.db")
        report = await IntentEvalRunner(project).run(tracker=tracker)
        results = tracker.get_intent_eval_results()
        tracker.close()

        assert len(results) == 1
        assert results[0]["repo_name"] == project.name
        assert results[0]["total_cases"] == report.total_cases
        assert results[0]["accuracy_at_1"] == pytest.approx(report.accuracy_at_1)
