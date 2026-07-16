# Memory auto-store (failure-mode learning) — v1 design

> [!NOTE]
> Historical design record. It may describe pre-implementation state, internal
> plan numbers, or behavior that has since changed. See the
> [current README](../../../README.md) and [document map](../../README.md).

- **Date:** 2026-06-08
- **Status:** Approved (brainstorming) — pending implementation plan
- **Scope:** Plan 5+ sub-project 1 of 3 (the other two — Sleep/decay maintenance, parallel worktree runs in `goal` — are out of scope here)
- **Sequencing:** Write spec now → ship 0.1.0 → implement v1. The release must NOT be gated on this implementation (CEO call, 2026-06-08).
- **Related decisions:** memory `d0021270` (Cloud-required for Plan 5+), `64b3d0ea` (best-effort ordering pattern), `c8aa1a63` (bounded-composable `run`).

## Problem

`run` is a memory-grounded loop, but today it only persists a *single* end-of-run
savepoint plus a done-state. When a run **fails** — a gate halt (red/unknown
verdict), a phase FAIL (claude exit, worktree error) — nothing durable is
written about *why*. The next run over a similar issue re-learns the same lesson
from scratch. The roadmap's defining capability is **failure-mode learning**:
every failure becomes a memory, surfaced preemptively next time so recurring
failures trend toward zero cost.

### What is already built (do not rebuild)

Most headline "Plan 5+" items already ship in `run/__init__.py`:

- **graph/explore** — recall→`explore` neighbours of the top hit (lines 97–109), capped grounding.
- **feedback reinforcement** — grounding memories reinforced after a successful run (lines 180–187).
- **worktree isolation** — `ensure_worktree(root, issue)` per run (line 122).

The genuine gap this spec closes is **auto-store**: capturing failure and
outcome memories during the run, and surfacing prior failures preemptively.

## Goals

1. Auto-store a **failure memory** at each run failure (gate halt, phase FAIL).
2. Auto-store one **outcome memory** on a successful ship (enriching the existing savepoint with the PR summary).
3. **Preemptively surface** prior failures of an issue in the recall phase, so a re-run leads its grounding with "you failed here before."
4. Do all of the above **best-effort** — a capture failure never changes the run's outcome or costs the resume marker.

## Non-goals (deferred to v2)

- **Graph edges** (`prevents` / `relates` linking failure → fix). v1 relies on tags + recall ranking only.
- **Sleep / decay consolidation usage.**
- **Cross-run upsert/dedup** of duplicate failure memories.
- **review-red capture** inside `run` — `run` does not call `review`; review-red capture belongs to the `review`/`--fix` path and is tracked separately.

## Design

### Architecture

A new, isolated module `src/kagura_engineer/run/learning.py` of **pure
functions** that turn a run outcome into a `MemorySpec` (a small dataclass:
`summary`, `content`, `type`, `tags`, `importance`). The `run_idea` loop calls
these at existing hook points and passes the spec to `mem.remember(...)`. The run
loop only supplies raw outcome facts (phase, verdict, issue, pr_url); **all
memory wording is built in the helper** so it is unit-testable without a run.

Rationale (chosen over 2 alternatives): scattering `remember()` calls inline is
untestable and noisy; an event/observer bus is YAGNI for a fixed phase sequence.

### Capture points

All emitted **after** the critical state writes (`set_state`), each in its own
`try/except`, gated by `--no-remember` (no-remember ⇒ no writes), following the
established ordering pattern (`64b3d0ea`).

| Trigger | Code point (`run/__init__.py`) | Memory |
|---|---|---|
| gate halt (red/unknown) | line ~149 (`if not decision.proceed`) | `type=failure`, importance **0.7** |
| phase FAIL (claude exit / launch / worktree) | lines ~125, 137, 146 | `type=failure`, importance **0.7** |
| ship success | line ~165 (persist) | `type=savepoint` (enriched), importance **0.5** |

**Memory shapes** (summaries are structured, not free-form — see Trust & safety):

- Failure (gate halt): summary `"run #<N> halted at <phase> (verdict <v>)"`;
  tags `["repo:<root>", "issue:<N>", "run", "failure", "failure:gate-halt", "phase:<start|ship>", "source:harness"]`.
- Failure (phase FAIL): summary `"run #<N> failed at <phase> (<failure-kind>)"`
  where `<failure-kind>` ∈ {`claude-exit`, `launch`, `worktree`}; tag
  `failure:phase-fail`.
- Outcome (ship): summary `"run #<N> shipped → PR <url>"` (+ PR title if
  available); tags `["repo:<root>", "issue:<N>", "run", "outcome", "source:harness"]`.

### Trust & safety (CEO condition 1 — OWASP LLM01)

Auto-stored memories are **re-injected into the next `claude -p` prompt** via
recall. To keep that feedback loop safe:

- **Structured summaries only.** Do not store raw issue bodies or raw `claude`
  stdout/stderr verbatim. The `content` field carries only harness-derived
  facts: phase, verdict, failure-kind, issue number, PR url, and a **bounded**
  (≤ 200 char) tail of any error detail, already the cap `run` uses for stderr.
- **Trust labelling.** Every auto-stored memory carries `source:harness` and is
  written by the harness itself (trusted origin), distinguishable at recall time
  from external/connector-ingested memories. This composes with the existing
  `trust_tier` filtering the bootstrap uses.

### Backend scope (CEO condition 2)

v1 capture uses **only the existing `MemoryClient.remember`**, so it works on
**both** the local SQLite and cloud backends — no Protocol, backend, or config
change. This refines decision `d0021270`: **v1 capture is backend-agnostic; the
"Cloud-required" boundary applies to the v2 features (graph edges, Sleep), not to
auto-store.** Update `d0021270` to reflect this when v1 lands.

### Preemptive surfacing (the "smart" part — CEO condition 3)

Two mechanisms, no graph edges required:

1. **Passive** — because failures now live in the store, the existing semantic
   recall in phase 1 surfaces them for *similar future issues* automatically.
2. **Active** — add one targeted recall in phase 1 for prior failures of *this*
   issue (`recall_detailed(context_id, "issue <N> prior failures", tags=["failure"], ...)`),
   merged ahead of the general grounding (still under `_GROUNDING_CAP`).

**Acceptance test (mandatory):** an integration test proving that after a run
fails at a phase, a subsequent `run` of the same issue has that failure memory
present in its grounding (ranked into the injected set). If this does not hold,
the feature is not working — tags alone are insufficient and the active recall
must be fixed before shipping.

## Testing strategy (TDD)

1. **Unit** — `learning.py` pure functions: each `outcome → MemorySpec`
   mapping (gate-halt, each phase-FAIL kind, ship-outcome), asserting summary
   text, type, tags (incl. `source:harness`), importance, and the ≤200-char
   content bound.
2. **Integration** — `run_idea` against a fake `MemoryClient` recording calls:
   - failure path emits exactly one failure `remember` with the right spec;
   - ship path emits the enriched outcome `remember`;
   - `--no-remember` emits **zero** auto-store writes;
   - auto-store runs **after** the `set_state` resume marker (ordering).
3. **Acceptance** — the preemptive-surfacing test above.

## Files

- New: `src/kagura_engineer/run/learning.py`, `tests/run/test_learning.py`.
- Edit: `src/kagura_engineer/run/__init__.py` (hook points + active failure recall).
- No changes to `MemoryClient` Protocol, either backend, `config.py`, or `pyproject.toml`.

## v2 (future, out of scope)

- `create_edge` on the `MemoryClient` Protocol (cloud passthrough + local edges
  table) and a `prevents` linking rule (failure → fix across runs). **Cloud-required.**
- Sleep / decay consolidation usage.
- Cross-run upsert/dedup of repeated failure memories (only if v1 noise warrants it).
