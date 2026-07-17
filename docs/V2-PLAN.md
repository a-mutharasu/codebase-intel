# codebase-intel v2 — The Intent Graph: Requirements & Plan

Status: draft for review · Author: planning session 2026-07-17 · Source: VISION statement + v1 codebase analysis

---

## 0. What v1 already gives us (and what v2 actually is)

v2 is **not greenfield**. The audit of the current code shows the substrate exists:

| v2 needs | v1 already has | Gap |
|---|---|---|
| Decision records with why, alternatives, constraints | `decisions/models.py` — `DecisionRecord`, `AlternativeConsidered`, `Constraint`, supersedes chains, staleness, anchor orphan detection | No hierarchy; flat journal |
| Code anchoring | `graph/` (tree-sitter, 19 langs) + decision anchors + xxhash | No re-anchoring on refactor |
| Intent with verification | `intent/models.py` — `Intent`, `AcceptanceCriterion`, gap detection | Task-scoped, not architectural; not graph-linked |
| Token-budgeted assembly | `orchestrator/` (48–75% measured reduction) | Assembles over code graph, not intent graph |
| Honesty / drift | `drift/`, `contracts/` | No intent-vs-code divergence check |
| Agent interface | `mcp/server.py` — 13 tools | No capture-enforcement loop, no multi-level traversal |
| Measurement | `analytics/benchmark.py`, `feedback.py` | No correctness eval, only token counts |

**v2 = a hierarchy over decisions + a capture loop + traversal-first context + honesty machinery.** Roughly 60% of it is composition of existing modules.

---

## 1. Functional Requirements

### FR-1 · Intent Graph core (data model)
- **FR-1.1** Four-level node hierarchy: L0 system topology, L1 service/module, L2 component/flow, L3 function/decision. Every node carries: `why` (the captured rationale), provenance (who/what/when), trust tier, timestamps.
- **FR-1.2** Typed edges: `contains` (level N → N+1), `anchored-to` (node → code entity), `produced-by` (node → DecisionRecord), `supersedes`, `depends-on`, `conflicts-with`.
- **FR-1.3** Every node anchored to code via AST symbol path + content hash (reuse graph module). L0/L1 nodes anchor to directories/module roots; L3 to symbols/ranges.
- **FR-1.4** v1 `DecisionRecord` becomes the payload of L3 (and some L2) nodes — alternatives considered, constraints, confidence. Migration path required (FR-7.1).
- **FR-1.5** Append-only semantics: nodes are never hard-deleted; superseded with pointer + one-line why (mirrors the Task Ledger philosophy).
- **FR-1.6** Trust tiers on every `why`: `mined` (inferred from history, low trust) → `asserted` (agent wrote it at decision time) → `confirmed` (human reviewed). Tier is surfaced in every query response.

### FR-2 · Intent capture loop (the core loop of v2)
- **FR-2.1** MCP capture tools: `record_why(change_class, node_level, why, alternatives, anchors)` and `check_precedent(question/anchors)` — the latter answers "has this been decided before?" *before* an agent re-makes or reverses a decision.
- **FR-2.2** Git hook gate: pre-commit (and CI check) detects structural changes to anchored code with no linked why-entry in the staged journal → warns (soft mode) or blocks (hard mode, per-repo opt-in).
- **FR-2.3** Significance classifier: heuristics (new module, new external dep, changed public signature, new retry/cache/auth logic, deleted anchored node) decide what *demands* a why vs what is mechanical. Capture fatigue is the #1 adoption killer — mechanical edits must never prompt.
- **FR-2.4** Cold-start backfill: extend `decisions/miner.py` to mine git history / PR descriptions / existing ADRs into `mined`-tier nodes so the graph is non-empty on day 1.
- **FR-2.5** Capture is journal-first and crash-safe: an append to a plain-text journal is the commit point; SQLite materialization is derived (see NFR-R1).
- **FR-2.6** Review queue: `asserted` whys can be promoted to `confirmed` by a human (CLI first; team layer later).

### FR-3 · Traversal & query API (MCP-first)
- **FR-3.1** Enter at any level and drill: `get_intent(node_id | level | path)`, `why(symbol | file:line)`, `drill_down(node)`, `roll_up(node)`.
- **FR-3.2** Session bootstrap: `bootstrap(task_description, token_budget)` → relevant intent subgraph + only the code excerpts the intent graph cannot answer. Target: most sessions need zero raw file reads for *understanding* (edits still read files).
- **FR-3.3** Budget management extends `orchestrator/`: intent nodes are scored (relevance × trust × freshness) and packed within budget; code excerpts fill remaining budget.
- **FR-3.4** Every answer cites: node id, trust tier, freshness, and the code anchor — so the consuming agent can judge reliability and verify.
- **FR-3.5** Degradation ladder (also a resilience requirement): intent graph → code graph → raw files. Every query tool must return a useful (lower-confidence, labeled) answer when upper layers are missing or stale — never a hard failure.

### FR-4 · Honesty machinery (the map stays true)
- **FR-4.1** Anchor validation & repair: on each incremental update, dead anchors (hash mismatch / symbol gone) trigger re-anchoring by AST-path + similarity; confident matches auto-repair, ambiguous ones queue for review. Never blocks queries.
- **FR-4.2** Divergence detection: code changed under an intact why → node marked `stale`; surfaced in bootstrap responses and drift reports until re-affirmed or superseded.
- **FR-4.3** Conflict detection between whys (extend `orchestrator/conflict` + `contracts/`): contradictory constraints across levels are flagged, not silently averaged.
- **FR-4.4** Coverage & decay metrics: % of structural nodes carrying intent, staleness ratio, capture rate per session — via `analytics/`.

### FR-5 · Human projection (after machine loop works)
- **FR-5.1** Read-only rendering of the same graph: top-to-granular flow view (start: static HTML/Mermaid export; interactive later).
- **FR-5.2** Export: Markdown/ADR and JSON — the graph must never be a lock-in format.

### FR-6 · Team layer (last)
- **FR-6.1** Ownership per subtree, review workflow for promotion to `confirmed`, history/audit of supersessions.

### FR-7 · Compatibility
- **FR-7.1** One-shot migration: v1 decision journal → L3 intent nodes (`mined`/`asserted` tier preserved from provenance).
- **FR-7.2** All v1 MCP tools keep working; v2 tools are additive.

---

## 2. Non-Functional Requirements

### NFR-P · Performance
- Capture append: < 50 ms perceived (journal write only; materialization async).
- Bootstrap query: < 500 ms p95 on a 100k-node graph.
- Context efficiency: ≥ v1's 48–75% token reduction; v2 target is a further measurable cut on "understanding" sessions (Phase 4 defines the harness).

### NFR-R · Resilience (primary focus — see §5 for design)
- **NFR-R1** No captured why is ever lost to a crash: journal-first, append-only, replayable.
- **NFR-R2** Partial data degrades, never fails: the ladder of FR-3.5 is mandatory for every tool.
- **NFR-R3** Concurrent writers (multiple agent sessions + git hooks + workspace mode) cannot corrupt state: SQLite WAL, busy-timeout, single-writer async queue, idempotent upserts keyed by content hash.
- **NFR-R4** Full rebuild from journal is always possible (SQLite is an index, not the truth).
- **NFR-R5** Schema is versioned with forward-only migrations; old graphs open with automatic upgrade, newer graphs refuse with a clear message.
- **NFR-R6** Anchor repair is a background/on-demand job; a broken anchor degrades one node, never the graph.

### NFR-T · Trust & integrity
- Provenance mandatory on every node; no silent overwrites; supersession only.
- Trust tier appears in **every** rendered answer — a `mined` why must never read as fact.
- A wrong why is worse than no why: containment (tiers, staleness, citations) is a hard requirement, not polish.

### NFR-X · Portability & philosophy
- Zero-dependency storage stays (SQLite + plain-text journal). No server, no cloud required.
- The journal lives in-repo (`.intel/journal/`) and travels with `git clone`; merge conflicts are line-appends, resolvable.
- Agent-agnostic: MCP is the contract; nothing assumes a specific agent.

### NFR-S · Scalability
- 19 languages (existing), monorepos via `workspace/`, cross-service intent via `crossrepo/`.
- Graph growth must be sublinear in query cost: relevance scoring + archive tier for superseded/stale nodes.

### NFR-Sec · Security & privacy
- Whys contain business rationale — export supports redaction; team layer adds access control; secrets in why-text rejected at capture (simple pattern gate).

### NFR-O · Observability
- Capture rate, skip rate, staleness %, bootstrap token savings, precedent-hit rate ("re-decision prevented") — all in `analytics/`.

### NFR-DX · Friction ceiling (adoption-critical)
- Default capture path costs the agent one tool call and the human zero keystrokes. If capture ever feels like homework, the vision's premise ("capture at decision time, not after") fails in practice.

---

## 3. Key design decisions — pros / cons / recommendation

**D1 — Enforcement: hard gate vs soft nag.**
Hard pre-commit block guarantees coverage but will get uninstalled the first time it blocks a hotfix. *Recommend:* soft by default (warn + record the gap as a `missing-why` node), hard mode per-repo opt-in, and always-hard in CI for designated paths. The `missing-why` backlog makes the soft mode honest.

**D2 — Source of truth: SQLite vs append-only journal in git.**
SQLite-only is simple but a binary blob that doesn't merge, doesn't travel, and dies with the disk. *Recommend:* plain-text append-only journal (JSONL) committed to the repo as truth; SQLite as a rebuildable index (git's objects-vs-index model). Cons: repo noise and size — mitigated by `.intel/` dir, compaction of superseded entries into archive files. This single decision buys most of the resilience story for free.

**D3 — Upper levels: auto-summarized vs linkage-only.**
Auto-summarizing L3 whys into an L1 narrative reintroduces exactly the "auto-generated documentation is fiction" failure the vision rejects. *Recommend:* upper levels are captured or linkage-only; any derived summary is explicitly tagged `derived` (lowest trust) and regenerated, never hand-edited.

**D4 — SQLite vs a real graph DB.**
Traversals are shallow (≤4 levels + typed edges); recursive CTEs handle this fine, and zero-dependency portability is a stated project principle. *Recommend:* stay SQLite. Revisit only if team-layer scale demands it.

**D5 — MCP-first vs library-first.**
Stay MCP-first (agents are the consumer), but expose the capture/query core as a plain Python API so git hooks and CI don't need a running server.

---

## 4. Complexity & challenge hotspots (ranked by risk)

1. **Capture enforcement without a hook point (make-or-break).** MCP is pull-only — nothing forces an agent to call `record_why`. Mitigation is a layered net: git-hook gate (deterministic, agent-independent), `check_precedent` made so useful agents call it voluntarily, CLAUDE.md/skill conventions, CI check, PR bot. Assume every layer leaks; measure capture rate from day 1 (NFR-O).
2. **Fictional intent (worst failure mode).** An agent writing a plausible-but-wrong why poisons every future session with confident falsehood — strictly worse than no data. Containment: trust tiers surfaced everywhere, capture-time cross-check of the why against the actual diff, human promotion queue, staleness demotion.
3. **Anchor stability under refactor.** Renames/moves/splits break anchors; content hashes change on *every* edit by design. Re-anchoring needs AST-path + structural similarity, auto-repair only above a confidence threshold, review queue below it. This is the hardest pure-engineering problem in v2.
4. **Level assignment & aggregation.** "Which level does this why belong to?" has no crisp answer; misfiled nodes make traversal lie. Start with heuristics + agent-declared level + cheap re-file operation; accept fuzziness, optimize for findability (search across levels) over taxonomic purity.
5. **Supersession semantics.** Partial reversals, whys that conflict across levels, temporal queries ("why was it shaped this way in March?"). v1's supersedes chain is the seed; needs effect-scoping (which anchors a supersession actually covers).
6. **Concurrency.** Multiple agent sessions + hooks + `--auto` workspace mode against one SQLite file. WAL + single-writer queue + idempotent upserts (NFR-R3); the journal-first design makes contention a latency issue, never a data-loss issue.
7. **Cold start / empty-graph value gap.** The graph's value accrues with capture; day-1 it's empty. Mined backfill (FR-2.4) plus full v1 fallback keep the tool useful immediately — v2 must never be *worse* than v1 on an uncaptured repo.
8. **Proving the bet.** "Correctness gains" stays a vibe without an eval harness: fixed task set on real repos, bootstrap-vs-file-reading A/B, measuring tokens *and* task correctness. `analytics/benchmark.py` is the seed.
9. **Token economics of the graph itself.** The graph grows unboundedly; without relevance scoring + archive tiers the cure re-creates the disease (context bloat).
10. **Cross-repo intent.** A why spanning services (API contract decisions) has no single home repo. Park it on the `crossrepo/` registry; don't block v2 core on it.

---

## 5. Resilience design (the requested focus)

```
capture ──▶ .intel/journal/*.jsonl   (append-only, in git — TRUTH)
                    │ replay/materialize (async, idempotent)
                    ▼
             .intel/index.sqlite     (WAL mode — derived INDEX, rebuildable)
                    ▼
        MCP queries — degradation ladder:
        intent graph → code graph → raw files
        (missing/stale layer ⇒ lower-confidence labeled answer, never an error)
```

- **Crash-safety:** the journal append *is* the commit point. Crash before materialization ⇒ replay on next start. Nothing captured is ever lost (NFR-R1/R4).
- **Corruption recovery:** delete the SQLite index → full rebuild from journal. One command (`codebase-intel rebuild`).
- **Concurrency:** journal appends are O_APPEND single-line writes; SQLite writes flow through one async queue per process, WAL + busy-timeout across processes; all upserts idempotent by content hash — replays and races converge to the same state.
- **Partial failure:** one broken anchor degrades one node (marked, still queryable); parse failure in one file degrades that file's subgraph; the ladder guarantees every tool still answers.
- **Evolution:** `schema_version` table, forward-only migrations, refuse-with-message on version from the future.
- **Portability/DR:** truth is plain text in git — clones, backups, and merges come free; merge conflicts are line-appends.

---

## 6. Phased plan (each phase independently shippable)

| Phase | Delivers | Verify bar |
|---|---|---|
| **0 · Foundation** | IntentNode/edge models, trust tiers, journal-first store, `rebuild`, migration of v1 decisions → L3 nodes | Unit + property tests: crash-replay idempotence; migrated graph queryable; mypy/ruff clean |
| **1 · Capture loop** | `record_why`, `check_precedent`, git-hook soft gate, significance classifier, `missing-why` backlog | Integration: structural commit without why → warning + backlog node; mechanical commit → silent |
| **2 · Traversal** | Multi-level query tools, `bootstrap`, budget integration, degradation ladder | On this repo: bootstrap answers 10 canned "why" questions with correct citations, zero raw file reads; ladder test with deleted index |
| **3 · Honesty** | Anchor repair, staleness, conflict detection, coverage metrics | Refactor-simulation suite: rename/move/split → ≥90% auto-reanchor, rest queued, zero silent loss |
| **4 · Measure** | Eval harness: token + correctness A/B on 2–3 real repos | Published numbers vs v1 baseline |
| **5 · Projection & team** | Mermaid/HTML export, review queue promotion, ownership | Render of this repo's graph is legible; promotion flow round-trips |

Dependency-wise: 0 → 1 → 2 can overlap with 3; 4 needs 2; 5 last. Phases 0–3 need no new access.

---

## 7. Access needed

- **Now (Phases 0–3):** nothing beyond this repo — already have it.
- **Phase 2/4:** read access to 2–3 real production repos for bootstrap benchmarking (the v1 48–75% claim's successors need real substrates).
- **Backfill mining of PRs/issues (FR-2.4, optional):** a read-only GitHub token.
- **Team layer (Phase 5, later):** decide then — nothing yet.
