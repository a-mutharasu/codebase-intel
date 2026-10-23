# Codebase Intel — Project CLAUDE.md

## Project Overview
Codebase Intelligence Platform: an open-source, agent-agnostic system that provides AI coding agents with structured context, decision provenance, and quality contracts. Solves the three biggest gaps in AI-assisted development: context/memory, judgment/business context, and quality assurance.

## Tech Stack
- **Language:** Python 3.11+
- **Type System:** Pydantic v2 for all models, mypy strict mode
- **Storage:** SQLite via aiosqlite (zero-dependency, portable)
- **Parsing:** tree-sitter for language-agnostic AST analysis
- **Agent Interface:** MCP (Model Context Protocol) server
- **CLI:** Typer + Rich
- **Git Integration:** GitPython
- **Token Counting:** tiktoken
- **Hashing:** xxhash for content fingerprinting

## Architecture
Layered system with five core modules + two interface layers:

```
AI Agent (any) → MCP Server / CLI
                      ↓
            Context Orchestrator
          ↙         ↓          ↘
   Code Graph   Decisions   Contracts
          ↘         ↓          ↙
            Drift Detector
                  ↓
             Codebase (git)
```

### Module Responsibilities:
- **core/** — Shared types, config, exceptions. No business logic.
- **graph/** — Semantic code graph: AST parsing, dependency mapping, impact analysis, SQLite storage
- **decisions/** — Decision journal: structured records, git mining, code linking, temporal validation
- **contracts/** — Quality contracts: architectural rules, pattern libraries, evaluation engine
- **orchestrator/** — Context assembly: budget management, freshness scoring, conflict detection
- **drift/** — Drift detection: staleness, pattern violations, knowledge decay
- **mcp/** — MCP server: exposes all modules as queryable tools for AI agents
- **cli/** — CLI interface: init, analyze, query, serve commands

## Project Flow
1. `codebase-intel init` → scans repo, builds initial code graph, generates starter configs
2. Git hooks keep graph updated incrementally on each commit
3. AI agent connects via MCP → sends task description
4. Orchestrator assembles relevant context (files, decisions, contracts) within token budget
5. Agent receives structured context, writes code
6. Drift detector flags violations post-commit

## Directory Structure
```
src/codebase_intel/
├── core/          # types.py, config.py, exceptions.py
├── graph/         # models.py, storage.py, parser.py, query.py, builder.py
├── decisions/     # models.py, store.py, linker.py, miner.py, validator.py
├── contracts/     # models.py, parser.py, evaluator.py, registry.py
├── orchestrator/  # assembler.py, budget.py, scorer.py, conflict.py
├── drift/         # detector.py, reporter.py
├── intentgraph/   # v2 core: models, journal (JSONL truth), store (SQLite index),
│                  #   capture, classifier, gate, traversal, bootstrap, anchors,
│                  #   render, migrate — the multi-level intent graph
├── workspace/     # manager.py, registry.py — global multi-project workspace
├── crossrepo/     # models.py, scanner.py, registry.py — cross-service deps
├── analytics/     # tracker.py, benchmark.py, feedback.py
├── intent/        # models.py, verifier.py, store.py
├── mcp/           # server.py (supports --auto multi-project mode)
└── cli/           # main.py
```

Outside `src/`: `skills/codebase-intel/SKILL.md` is the agent skill (portable frontmatter only:
name, description, license, compatibility). `CHANGELOG.md` records releases.

## Key Commands
- `ruff check src/ tests/` — Lint
- `ruff format src/ tests/` — Format
- `mypy src/` — Type check
- `pytest` — Run tests with coverage
- `pytest tests/unit/` — Unit tests only
- `pytest tests/integration/` — Integration tests only

## Conventions
- All models inherit from Pydantic BaseModel — never use raw dicts for structured data
- Async-first: all I/O operations are async
- Repository pattern for storage: models never touch SQLite directly
- Type hints on every function (params + return)
- Custom exceptions over generic ones — always include context
- Content hashing via xxhash for change detection
- ISO 8601 for all timestamps, UTC timezone

## Intent Graph (v2)
- Journal-first: `.codebase-intel/intent-journal/*.jsonl` is committed source of truth;
  `intent-graph.db` is a derived, rebuildable index (git-ignored)
- Trust tiers: mined → derived → asserted (agent) → confirmed (human) — surfaced in every render
- MCP tools: record_why, check_precedent, bootstrap, get_intent
- CLI: why, intent-gate, install-hooks, intent-check, intent-confirm, intent-render,
  intent-migrate, intent-rebuild, intent-eval
- Degradation ladder everywhere: intent graph → code graph → raw files, never hard-fail
- Eval harness (T-006): `codebase-intel intent-eval <repo...>` — token AND correctness
  A/B (accuracy@1/@3, naive-vs-citation token reduction) vs the v1 baseline. See
  docs/EVAL-RESULTS.md for methodology and this repo's self-hosted numbers.
- See docs/V2-PLAN.md (requirements/design) and docs/TASK-LEDGER.md (state)

## Current Focus
- v0.3.0 release prep (intent graph + agent skill) on `chore/release-0.3.0`; version is
  single-sourced from `codebase_intel.__version__` via `[tool.hatch.version]`
  (release = GitHub Release → `publish.yml` → PyPI)
- v2 intent graph: Phases 0–5 + eval harness shipped (see TASK-LEDGER)
- Next: eval harness needs 2–3 external production repos for a broader A/B (T-006
  remains open for that), team layer depth, pre-existing coverage debt (T-008)
