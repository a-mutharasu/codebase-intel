# Handoff — codebase-intel v2 (Intent Graph)

Last updated: 2026-07-18 · Branch: `main` · For the next session (human or agent)

## Read this first

1. This file (5 min) — orientation and where things stand
2. `docs/V2-PLAN.md` — the requirements analysis and phased plan this work follows
3. `docs/TASK-LEDGER.md` — the authoritative, up-to-date task state (source of truth,
   supersedes anything below if they disagree)

## What this is

`codebase-intel` v1 gives AI agents structured code-graph context, a decision journal,
and quality contracts. v2 (this work) adds an **intent graph**: a multi-level "why" layer
captured at decision time, so agents traverse recorded reasoning instead of re-reading
files and re-deciding things that were already decided. See `docs/V2-PLAN.md` for the
full functional/non-functional requirements and the resilience design.

## State right now

All of v2 Phases 0–5 are built, tested, and merged to `main`. Plus a chore pass that
eliminated all pre-existing v1 ruff/mypy debt. Concretely:

| Phase | What | Status |
|---|---|---|
| 0 | Intent graph foundation — journal-first store (JSONL truth + rebuildable SQLite index), trust tiers, v1→v2 migration | done |
| 1 | Capture loop — `record_why`, `check_precedent`, pre-commit gate, significance classifier | done |
| 2 | Traversal + session bootstrap with a mandatory degradation ladder | done |
| 3 | Honesty machinery — anchor repair/re-anchoring, staleness, conflicts, coverage | done |
| 5 | Human projection (Mermaid/HTML render) + review promotion (asserted→confirmed) | done |
| 4 (T-006) | Eval harness — token AND correctness A/B | **blocked**, see below |
| — (T-008) | v1 lint/type/coverage debt | ruff+mypy done; coverage gate still open |

913 tests passing, `ruff check`/`ruff format --check`/`mypy src/` all clean on `main`.

### What's genuinely unfinished

- **T-006 (eval harness) is blocked, not done.** The harness itself
  (`analytics/eval.py`, CLI `codebase-intel intent-eval`) is code-complete and verified
  end-to-end — it ran against this repo and produced real numbers
  (`docs/EVAL-RESULTS.md`: 100% accuracy@1/@3, 99% token reduction, n=3 cases, self-hosted
  only). The task's actual bar ("real repos," plural, vs v1 baseline) needs 2+ external
  production repos. The CLI already accepts multiple paths
  (`codebase-intel intent-eval repo-a repo-b repo-c`) — no more code is needed, just repos
  to point it at. **This is the most concrete unblock a human can hand over.**
- **T-008 (v1 coverage debt) is in-progress.** `ruff` (222→0) and `mypy` (33→0) errors are
  fully fixed. The `pytest` 90% coverage gate still fails (~45% project-wide) because large
  v1 modules have zero dedicated unit tests: `mcp/server.py` (~5%), `graph/query.py` (0%),
  `intent/*.py` (0%), `crossrepo/*.py` (0%), `decisions/miner.py` (0%). This is a
  substantially larger effort (writing tests for existing untested v1 code) than the
  lint/type fixes were — treat it as its own project, not a quick follow-on. Until it's
  addressed, run tests with `pytest --no-cov` or expect the coverage gate to fail even
  though every test passes.

## How to verify the repo is healthy

```bash
uv sync --extra dev
uv run ruff check src/ tests/          # should be clean
uv run ruff format --check src/ tests/ # should be clean
uv run mypy src/                        # should be clean
uv run pytest --no-cov -q               # 913 passed
```

## How to explore what v2 built

```bash
codebase-intel why "<title>" "<reason>" -f <file>   # record a why
codebase-intel intent-gate                          # pre-commit gate (also installable: install-hooks)
codebase-intel intent-check                         # anchor validation/repair report
codebase-intel intent-render -f html -o out.html     # human-readable graph
codebase-intel intent-eval <repo...>                 # token+correctness eval (T-006)
```

Or via MCP tools: `record_why`, `check_precedent`, `bootstrap`, `get_intent`.

This repo dogfoods its own intent graph — `.codebase-intel/intent-journal/journal.jsonl`
is committed and has real entries from this session's own work (see git history on that
path). `docs/intent-graph.html` is a static render of it.

## Working conventions this session used (carry these forward)

- One feature branch per phase (`feature/v2-phaseN-...`), squash of work, merge
  `--ff-only` to `main` after full verification (ruff + mypy + full test suite).
- `docs/TASK-LEDGER.md` is updated at claim-time (status → in-progress, owner, touched)
  and at completion (status → done/blocked, a dated Decisions entry explaining what
  passed/what didn't and why) — read it before trusting any status claim here, this file
  is a snapshot and will go stale.
- Commits in this session were made **without** a Co-Authored-By trailer, per explicit
  user instruction — continue that unless told otherwise.
- Don't mark a ledger task `done` if its literal Verify bar isn't met — T-006 is the
  example: the harness works, but "done" would overclaim a multi-repo A/B that hasn't
  happened yet. `blocked` was used instead, with what's built vs what's missing spelled
  out.

## Suggested next steps (not mandated — use judgment)

1. If you have other repos handy: `codebase-intel init` in them, capture a few real
   `why`s, then `codebase-intel intent-eval` across all of them for T-006's real bar.
2. Otherwise: pick up T-008's coverage debt, module by module (mcp/server.py and
   graph/query.py are the biggest zero-coverage gaps).
3. Team-layer depth (multi-user review queues, ownership UI, access control) was
   deliberately deferred in Phase 5 — `capture.promote()` + owner metadata is the seed if
   that becomes a priority.
