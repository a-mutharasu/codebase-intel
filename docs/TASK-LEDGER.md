# TASK LEDGER — codebase-intel

## Active Board

| ID | Title | Status | Touched |
|---|---|---|---|
| T-001 | v2 requirements analysis & robust plan | done | 2026-07-17 |
| T-002 | Phase 0 — intent graph foundation (models, journal-first store, v1 migration) | done | 2026-07-17 |
| T-003 | Phase 1 — capture loop (record_why, check_precedent, git-hook gate, classifier) | done | 2026-07-17 |
| T-004 | Phase 2 — traversal API, session bootstrap, degradation ladder | pending | 2026-07-17 |
| T-005 | Phase 3 — honesty machinery (anchor repair, staleness, conflicts, coverage) | pending | 2026-07-17 |
| T-006 | Phase 4 — eval harness: token + correctness A/B on real repos | pending | 2026-07-17 |
| T-007 | Phase 5 — human projection + team layer | pending | 2026-07-17 |
| T-008 | Chore — pre-existing lint/type/coverage baseline debt | pending | 2026-07-17 |

---

### T-004 · Traversal API + session bootstrap
Status:   pending
Touched:  2026-07-17
Blocked-by: T-002, T-003

Purpose:  Agents must enter at any level and get budgeted context without raw file reads —
          the graph-is-the-interface principle.
Impact:   orchestrator/ (scoring over intent nodes), mcp/server.py. Unblocks T-006.
Verify:   Bootstrap answers 10 canned why-questions on this repo with correct citations and
          zero raw file reads; degradation-ladder test passes with SQLite index deleted.

Decisions:
  - 2026-07-17: Mandatory degradation ladder intent→code-graph→files; tools never hard-fail
    on missing/stale layers (resilience NFR-R2).

### T-005 · Honesty machinery
Status:   pending
Touched:  2026-07-17
Blocked-by: T-002

Purpose:  "Every claim stays anchored to code" — without re-anchoring and staleness the
          graph becomes the Lucidchart rot it exists to replace.
Impact:   drift/, graph/, new anchor-repair job. Highest engineering risk (hotspot #3).
Verify:   Refactor-simulation suite: rename/move/split → ≥90% auto-reanchor, remainder
          queued for review, zero silent anchor loss.

Decisions: (none yet)

### T-006 · Eval harness
Status:   pending
Touched:  2026-07-17
Blocked-by: T-004; needs read access to 2–3 real repos (user to provide)

Purpose:  The bet claims token AND correctness gains; unmeasured, it stays a vibe.
Impact:   analytics/benchmark.py extension.
Verify:   Published A/B numbers (bootstrap vs file-reading) on real repos vs v1 baseline.

Decisions: (none yet)

### T-007 · Human projection + team layer
Status:   pending
Touched:  2026-07-17
Blocked-by: T-004, T-005

Purpose:  Render-for-humans-second principle; ownership/review enables confirmed-tier whys.
Impact:   New export/render path; CLI review queue.
Verify:   Mermaid/HTML render of this repo's graph is legible; promotion flow round-trips.

Decisions: (none yet)

### T-008 · Chore: pre-existing lint/type/coverage baseline debt
Status:   pending
Touched:  2026-07-17

Purpose:  Discovered during T-002 verification: `ruff check src/ tests/` = 222 pre-existing
          errors (newer ruff than v1 was written against), `mypy src/` = 33 pre-existing
          errors in 15 files, and the pytest 90% coverage gate fails at 42% project-wide
          (mcp/server 6%, graph/query 0%, intent/ 0%). New intentgraph module is clean
          (ruff+mypy) and 93–97% covered — the debt is all v1 code.
Impact:   Whole repo; until fixed, "pytest" exits non-zero on the coverage gate.
Verify:   ruff/mypy clean on src/, coverage gate passes (or gate consciously re-scoped).

Decisions: (none yet)

---

## Archive

### T-003 · Build capture loop
Status:   done
Owner:    2026-07-17a
Touched:  2026-07-17

Purpose:  Capture-at-decision-time is the core loop of v2; without enforcement + low
          friction the graph stays empty and the vision fails (hotspot #1).
Impact:   intentgraph/{capture,classifier,gate}.py; MCP tools record_why + check_precedent;
          CLI why/intent-gate/install-hooks/intent-migrate/intent-rebuild; gate_hard config.
Verify:   PASSED 2026-07-17 — 861 tests green (27 new); integration: structural commit
          without why → warning + missing-why backlog node, mechanical commit → silent,
          staged-journal and existing-anchor both count as covered, hard mode blocks;
          live CLI smoke on a scratch repo confirmed the full loop; append p95 = 4.7ms
          (< 50ms bar); mypy strict + ruff clean on all new files.

Decisions:
  - 2026-07-17: Soft gate by default, hard mode opt-in per repo (V2-PLAN.md D1) —
    hard-blocking by default gets uninstalled on the first blocked hotfix.
  - 2026-07-17: Classifier is conservative by design (capture fatigue kills adoption):
    tests/docs/lockfiles never significant; only new/deleted source, dependency
    manifests, public-surface additions, and sensitive keywords trigger.
  - 2026-07-17: check_precedent includes superseded nodes deliberately — "tried and
    reversed" is the highest-value precedent. CLI capture gets confirmed trust (human);
    MCP capture gets asserted (agent).

### T-002 · Build Phase 0 intent-graph foundation
Status:   done
Owner:    2026-07-17a
Touched:  2026-07-17

Purpose:  v2 vision requires a multi-level intent graph; nothing can be captured or
          traversed until the node/edge models, trust tiers, and journal-first store exist.
Impact:   New module src/codebase_intel/intentgraph/ (models, journal, store, migrate);
          IntentGraphConfig in core/config.py; .gitignore (derived DB ignored, journal
          committed). Unblocks T-003..T-005.
Verify:   PASSED 2026-07-17 — 834 tests green (48 new: crash-replay idempotence, torn-line
          tolerance, rebuild-from-journal equivalence, migration idempotence); mypy strict
          clean on intentgraph+core; ruff clean on all new files; new module 93–97% coverage.

Decisions:
  - 2026-07-17: Journal-in-git (JSONL) is source of truth, SQLite is derived index —
    chosen for crash-safety, portability, mergeability (V2-PLAN.md D2).
  - 2026-07-17: JournalEntry IDs are content-addressed (hash of op+payload+ts+actor) —
    replay idempotence lives in the data, not in writer discipline.
  - 2026-07-17: v1 trust mapping: manual→confirmed, git-mined→mined, ai-suggested→asserted.
  - 2026-07-17: New enums use StrEnum; added ruff flake8-type-checking config so Pydantic
    runtime imports aren't false-flagged (pyproject.toml).

### T-001 · v2 requirements analysis & robust plan
Status:   done
Touched:  2026-07-17

Purpose:  User requested FR/NFR segregation, robust plan, pros/cons, resilience focus,
          and complexity hotspots for the v2 Intent Graph vision before implementation.
Impact:   docs/V2-PLAN.md (new). Frames all v2 tasks T-002..T-007.
Verify:   Plan document written, grounded in v1 code audit (decisions/intent/mcp modules),
          covering FR, NFR, 5 design decisions with trade-offs, 10 ranked hotspots,
          resilience architecture, phased plan. User review pending → feedback lands as
          dated Decisions here.

Decisions:
  - 2026-07-17: v2 treated as inversion of v1, not greenfield (~60% composition of
    existing modules) — confirmed by code audit.
  - 2026-07-17: Resilience anchored on journal-first append-only truth + rebuildable
    SQLite index + mandatory degradation ladder.
