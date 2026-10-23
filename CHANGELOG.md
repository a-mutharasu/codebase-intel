# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [0.3.0] — 2026-10-23

### Added

- **Intent graph (v2).** A multi-level "why" layer. `.codebase-intel/intent-journal/*.jsonl` is the
  committed source of truth, and `intent-graph.db` is a rebuildable, git-ignored index. Nodes
  carry trust tiers (`mined` → `derived` → `asserted` → `confirmed`) and four abstraction levels.
- MCP tools: `bootstrap`, `check_precedent`, `record_why`, `get_intent`. The server now exposes 16 tools.
- CLI: `why`, `intent-gate`, `install-hooks`, `intent-check`, `intent-confirm`, `intent-render`,
  `intent-migrate`, `intent-rebuild`, `intent-eval`.
- Pre-commit intent gate that warns (or blocks, with `intentgraph.gate_hard`) on structural
  changes with no recorded why.
- Anchor repair and re-anchoring when code moves; divergence, conflict and coverage reporting.
- Human projection of the intent graph as HTML or Mermaid.
- Safe, re-runnable migration of v1 decision records into the intent graph.
- Eval harness (`intent-eval`) measuring token cost and retrieval correctness against the v1
  baseline. Self-hosted results and caveats are in `docs/EVAL-RESULTS.md`.
- Agent skill at `skills/codebase-intel/SKILL.md` that tells agents when to call the intent tools.

### Changed

- The package version now comes from `codebase_intel.__version__`, and `get_status` reports
  it. Before this, three version strings had drifted apart: `0.1.0`, `0.2.0` and `0.2.0`.
- Project URLs in the package metadata now point to the actual GitHub repository.

### Fixed

- Removed all pre-existing ruff and mypy errors in the v1 modules.

## [0.2.0] — 2026-04-09

### Added

- Global workspace manager: one MCP server for every registered project (`serve --auto`).
- Intent tracking (`set_intent`, `check_intent`, `list_intents`) with machine-verifiable
  acceptance criteria.
- Cross-repo awareness across 14 web frameworks in 10 languages.
- Feedback loop and efficiency reporting.

## [0.1.0]

- Initial release: code graph, decision journal, quality contracts, drift detection, MCP server, CLI.

[0.3.0]: https://github.com/a-mutharasu/codebase-intel/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/a-mutharasu/codebase-intel/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/a-mutharasu/codebase-intel/releases/tag/v0.1.0
