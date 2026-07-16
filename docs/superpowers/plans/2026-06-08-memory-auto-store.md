# Memory auto-store (failure-mode learning) v1 — Implementation Plan

> [!NOTE]
> Historical design record. It may describe pre-implementation state, internal
> plan numbers, or behavior that has since changed. See the
> [current README](../../../README.md) and [document map](../../README.md).

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Auto-store failure and outcome memories during a `run`, and surface prior failures of an issue preemptively in grounding — so recurring failures cost less each time.

**Architecture:** A new pure-function module `run/learning.py` turns run outcomes into `MemorySpec` objects; `run_idea` calls a small best-effort `_capture` helper at its existing hook points (gate halt, phase FAIL, ship) and adds one issue-scoped failure recall in the recall phase. No `MemoryClient` Protocol / backend / config changes — uses the existing `remember` and `recall_detailed`, so it works on both backends.

**Tech Stack:** Python 3.11+, `pytest` + `monkeypatch` (no network/subprocess in tests), frozen dataclasses.

**Sequencing:** Implement only AFTER 0.1.0 is published (CEO call). The plan is ready; execution waits.

**Spec:** `docs/superpowers/specs/2026-06-08-memory-auto-store-design.md`

---

## File structure

- **Create** `src/kagura_engineer/run/learning.py` — `MemorySpec` + 3 pure spec builders (`gate_halt_spec`, `phase_fail_spec`, `outcome_spec`). Single responsibility: outcome → memory content. No I/O.
- **Create** `tests/run/test_learning.py` — unit tests for the pure functions.
- **Modify** `src/kagura_engineer/run/__init__.py` — add `_capture` helper + hook calls; refactor the persist savepoint to use `outcome_spec`; add issue-scoped failure recall.
- **Modify** `tests/run/test_orchestrator.py` — make `_FakeMemory` tag-aware; add capture + preemptive-surfacing tests.

---

## Task 1: `learning.py` — MemorySpec + spec builders

**Files:**
- Create: `src/kagura_engineer/run/learning.py`
- Test: `tests/run/test_learning.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/run/test_learning.py`:

```python
from kagura_engineer.run.learning import (
    MemorySpec, gate_halt_spec, phase_fail_spec, outcome_spec, _DETAIL_CAP,
)


def test_gate_halt_spec_shape():
    s = gate_halt_spec("myrepo", 42, "ship", "red")
    assert isinstance(s, MemorySpec)
    assert s.summary == "run #42 halted at ship (verdict red)"
    assert s.type == "failure"
    assert s.importance == 0.7
    assert "failure" in s.tags and "failure:gate-halt" in s.tags
    assert "phase:ship" in s.tags
    assert "repo:myrepo" in s.tags and "issue:42" in s.tags
    assert "source:harness" in s.tags


def test_phase_fail_spec_shape_and_kind():
    s = phase_fail_spec("myrepo", 7, "start", "claude-exit", detail="boom")
    assert s.summary == "run #7 failed at start (claude-exit)"
    assert s.type == "failure"
    assert s.importance == 0.7
    assert "failure:phase-fail" in s.tags and "phase:start" in s.tags
    assert "boom" in s.content


def test_phase_fail_spec_caps_detail():
    long = "x" * 1000
    s = phase_fail_spec("r", 1, "ship", "claude-exit", detail=long)
    # detail must be bounded — no raw blob re-injected into prompts
    assert len(s.content) <= _DETAIL_CAP + 120  # cap + fixed prefix headroom
    assert "x" * _DETAIL_CAP in s.content
    assert "x" * (_DETAIL_CAP + 1) not in s.content


def test_phase_fail_spec_without_detail_omits_it():
    s = phase_fail_spec("r", 1, "worktree", "worktree")
    assert s.summary == "run #1 failed at worktree (worktree)"
    assert "detail:" not in s.content


def test_outcome_spec_shape():
    s = outcome_spec("myrepo", 9, "https://x/pull/9")
    assert s.summary == "run #9 shipped → PR https://x/pull/9"
    assert s.type == "savepoint"          # keep type for cheap-resume compatibility
    assert s.importance == 0.5
    assert "outcome" in s.tags and "source:harness" in s.tags
    assert "repo:myrepo" in s.tags and "issue:9" in s.tags


def test_outcome_spec_handles_missing_url():
    s = outcome_spec("r", 9, None)
    assert "(no url)" in s.summary
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/run/test_learning.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'kagura_engineer.run.learning'`

- [ ] **Step 3: Write the implementation**

Create `src/kagura_engineer/run/learning.py`:

```python
"""Plan 5+ v1 — failure-mode learning: turn run outcomes into memory specs.

Pure functions (no I/O) so they unit-test without a run. `run_idea` calls
these at its hook points and passes the spec to `MemoryClient.remember`. All
memory wording is built here; the run loop only supplies raw outcome facts.

Safety (OWASP LLM01): these memories are re-injected into the next `claude -p`
prompt via recall, so summaries are structured (never a raw issue body or raw
claude output), any error detail is length-capped, and every memory is tagged
`source:harness` to mark its trusted origin.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Max chars of free error detail carried into a memory's content. Matches the
# stderr tail bound `run` already uses, so no unbounded blob is ever stored.
_DETAIL_CAP = 200


@dataclass(frozen=True)
class MemorySpec:
    summary: str
    content: str
    type: str
    tags: list[str] = field(default_factory=list)
    importance: float = 0.5


def _base_tags(repo: str, issue: int) -> list[str]:
    return [f"repo:{repo}", f"issue:{issue}", "run", "source:harness"]


def gate_halt_spec(repo: str, issue: int, phase: str, verdict: str) -> MemorySpec:
    return MemorySpec(
        summary=f"run #{issue} halted at {phase} (verdict {verdict})",
        content=f"gate halt: issue #{issue}, phase {phase}, verdict {verdict}",
        type="failure",
        tags=_base_tags(repo, issue) + ["failure", "failure:gate-halt", f"phase:{phase}"],
        importance=0.7,
    )


def phase_fail_spec(
    repo: str, issue: int, phase: str, kind: str, detail: str = ""
) -> MemorySpec:
    tail = detail.strip()[:_DETAIL_CAP]
    content = f"phase fail: issue #{issue}, phase {phase}, kind {kind}"
    if tail:
        content += f", detail: {tail}"
    return MemorySpec(
        summary=f"run #{issue} failed at {phase} ({kind})",
        content=content,
        type="failure",
        tags=_base_tags(repo, issue) + ["failure", "failure:phase-fail", f"phase:{phase}"],
        importance=0.7,
    )


def outcome_spec(repo: str, issue: int, pr_url: str | None) -> MemorySpec:
    url = pr_url or "(no url)"
    return MemorySpec(
        summary=f"run #{issue} shipped → PR {url}",
        content=f"kagura-engineer run drove issue #{issue} to {url}",
        type="savepoint",
        tags=_base_tags(repo, issue) + ["outcome"],
        importance=0.5,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/run/test_learning.py -q`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add src/kagura_engineer/run/learning.py tests/run/test_learning.py
git commit -m "feat(run): add learning.py memory-spec builders (failure-mode learning v1)"
```

---

## Task 2: Wire failure capture into `run_idea`

**Files:**
- Modify: `src/kagura_engineer/run/__init__.py`
- Modify: `tests/run/test_orchestrator.py`

Adds a best-effort `_capture` helper (gated by `--no-remember`) and calls it at the gate-halt and three phase-FAIL points.

- [ ] **Step 1: Write the failing tests**

Add to `tests/run/test_orchestrator.py` (the `_FakeMemory`, `_cfg`, `_patch_boundaries`, and `PhaseInvocation` import already exist):

```python
def test_gate_halt_stores_failure_memory(monkeypatch):
    _patch_boundaries(monkeypatch, phases={
        "start": PhaseInvocation("start", 0, "", "", "red", None),
    })
    mem = _FakeMemory()
    run_idea(_cfg(), 42, memory=mem, repo_root=Path("/repo"))
    assert any(t == "failure" for t, _ in mem.remembered)
    assert mem.state.get("run:42") is not None  # resume marker still written


def test_phase_fail_stores_failure_memory(monkeypatch):
    _patch_boundaries(monkeypatch, phases={
        "start": PhaseInvocation("start", 1, "", "boom", None, None),
    })
    mem = _FakeMemory()
    run_idea(_cfg(), 42, memory=mem, repo_root=Path("/repo"))
    assert any(t == "failure" for t, _ in mem.remembered)


def test_no_remember_skips_failure_capture(monkeypatch):
    _patch_boundaries(monkeypatch, phases={
        "start": PhaseInvocation("start", 0, "", "", "red", None),
    })
    mem = _FakeMemory()
    run_idea(_cfg(), 42, no_remember=True, memory=mem, repo_root=Path("/repo"))
    assert mem.remembered == []  # capture is a memory write → gated by --no-remember
    assert mem.state.get("run:42") is not None  # but the resume marker still writes


def test_failure_capture_hiccup_is_non_fatal(monkeypatch):
    _patch_boundaries(monkeypatch, phases={
        "start": PhaseInvocation("start", 0, "", "", "red", None),
    })

    class _BrokenRemember(_FakeMemory):
        def remember(self, context_id, *, summary, content, type, tags=None):
            raise RuntimeError("write down")

    report = run_idea(_cfg(), 42, memory=_BrokenRemember(), repo_root=Path("/repo"))
    assert report.status is RunStatus.BLOCKED  # still a clean halt, no traceback
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/run/test_orchestrator.py -q -k "failure_memory or skips_failure_capture or capture_hiccup"`
Expected: FAIL — `test_gate_halt_stores_failure_memory` / `test_phase_fail_stores_failure_memory` fail (no failure memory stored yet); `test_no_remember_skips_failure_capture` currently passes vacuously but keep it.

- [ ] **Step 3: Add the `_capture` helper and import**

In `src/kagura_engineer/run/__init__.py`, add to the imports near the other `from .` lines:

```python
from .learning import gate_halt_spec, phase_fail_spec, outcome_spec
```

Inside `run_idea`, right after the `_finish` inner function definition, add:

```python
    def _capture(spec) -> None:
        """Best-effort auto-store. Never raises; gated by --no-remember so a
        capture write follows the same opt-out as persist."""
        if no_remember:
            return
        try:
            mem.remember(cfg.context_id, summary=spec.summary, content=spec.content,
                         type=spec.type, tags=spec.tags)
        except Exception:  # noqa: BLE001 — auto-store is best-effort
            _log.exception("run auto-store failed (non-fatal)")
```

- [ ] **Step 4: Add capture at the worktree-fail point**

In the worktree `except` block (currently appends a `worktree` FAIL PhaseResult and returns), add the capture before `return _finish()`:

```python
    except (WorktreeError, OSError) as exc:
        _log.exception("run worktree phase failed")
        phases.append(PhaseResult("worktree", RunStatus.FAIL, f"worktree failed: {exc}"))
        _capture(phase_fail_spec(root.name, issue, "worktree", "worktree", str(exc)))
        return _finish()
```

- [ ] **Step 5: Add capture at the claude launch-OSError point**

In the `for phase in _PHASES:` loop, the launch `except OSError` block:

```python
        except OSError as exc:
            _log.exception("run %s phase failed to launch claude", phase)
            phases.append(PhaseResult(phase, RunStatus.FAIL, f"failed to launch claude: {exc}"))
            _capture(phase_fail_spec(root.name, issue, phase, "launch", str(exc)))
            return _finish(worktree=str(wt))
```

- [ ] **Step 6: Add capture at the claude non-zero / timeout point**

In the same loop, the `if inv.returncode != 0:` block, after building `tail` and appending the FAIL PhaseResult:

```python
            phases.append(PhaseResult(phase, RunStatus.FAIL, f"claude exited {inv.returncode}: {tail}"))
            _capture(phase_fail_spec(root.name, issue, phase, "claude-exit", tail))
            return _finish(worktree=str(wt))
```

- [ ] **Step 7: Add capture at the gate-halt point (after the resume marker)**

In the `if not decision.proceed:` block, AFTER the existing `set_state` try/except (critical write first) and the `phases.append(... BLOCKED ...)`:

```python
        if not decision.proceed:
            try:
                mem.set_state(cfg.context_id, _state_key(issue), {"halted_at": phase, "verdict": decision.verdict})
            except Exception:  # noqa: BLE001
                _log.exception("run halt set_state failed (non-fatal)")
            phases.append(PhaseResult(phase, RunStatus.BLOCKED, f"gate halt ({decision.verdict})", verdict=decision.verdict))
            _capture(gate_halt_spec(root.name, issue, phase, decision.verdict))
            return _finish(
                worktree=str(wt),
                resume_hint=f"review the {phase} gate, then re-run `kagura-engineer run {issue}`",
            )
```

- [ ] **Step 8: Run the new tests + the full run suite**

Run: `python -m pytest tests/run/ -q`
Expected: PASS (all run tests green, including the 4 new ones)

- [ ] **Step 9: Commit**

```bash
git add src/kagura_engineer/run/__init__.py tests/run/test_orchestrator.py
git commit -m "feat(run): auto-store failure memories at gate halt + phase FAIL"
```

---

## Task 3: Use `outcome_spec` for the success savepoint

**Files:**
- Modify: `src/kagura_engineer/run/__init__.py`

Refactor the inline savepoint `remember` in the persist phase to build its args from `outcome_spec` (adds the `outcome` + `source:harness` tags). Behavior-preserving for existing tests (`type` stays `savepoint`).

- [ ] **Step 1: Write the failing test**

Add to `tests/run/test_orchestrator.py`:

```python
def test_ship_outcome_memory_is_tagged(monkeypatch):
    _patch_boundaries(monkeypatch, phases={
        "start": PhaseInvocation("start", 0, "", "", "green", None),
        "ship": PhaseInvocation("ship", 0, "", "", "green", "https://x/pull/9"),
    })

    class _TagRecordingMemory(_FakeMemory):
        def __init__(self):
            super().__init__()
            self.last_tags = None

        def remember(self, context_id, *, summary, content, type, tags=None):
            self.last_tags = tags
            return super().remember(context_id, summary=summary, content=content, type=type, tags=tags)

    mem = _TagRecordingMemory()
    run_idea(_cfg(), 9, memory=mem, repo_root=Path("/repo"))
    assert mem.last_tags is not None
    assert "outcome" in mem.last_tags and "source:harness" in mem.last_tags
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/run/test_orchestrator.py::test_ship_outcome_memory_is_tagged -q`
Expected: FAIL — current savepoint tags are `["repo:repo", "run", "issue:9"]`, missing `outcome`/`source:harness`.

- [ ] **Step 3: Refactor the persist remember**

In `run_idea`, the persist block currently is:

```python
    if not no_remember:
        try:
            mem.remember(
                cfg.context_id,
                summary=f"run #{issue} → PR {pr_url or '(no url)'}",
                content=f"kagura-engineer run drove issue #{issue} to {pr_url}",
                type="savepoint",
                tags=[f"repo:{root.name}", "run", f"issue:{issue}"],
            )
            mem.set_state(cfg.context_id, _state_key(issue), {"done": True, "pr_url": pr_url})
            phases.append(PhaseResult("persist", RunStatus.OK, "savepoint stored"))
```

Replace the `mem.remember(...)` call with a spec-built one:

```python
    if not no_remember:
        try:
            _outcome = outcome_spec(root.name, issue, pr_url)
            mem.remember(
                cfg.context_id,
                summary=_outcome.summary, content=_outcome.content,
                type=_outcome.type, tags=_outcome.tags,
            )
            mem.set_state(cfg.context_id, _state_key(issue), {"done": True, "pr_url": pr_url})
            phases.append(PhaseResult("persist", RunStatus.OK, "savepoint stored"))
```

(Leave the surrounding `except` block and the feedback-reinforcement loop unchanged.)

- [ ] **Step 4: Run the run suite**

Run: `python -m pytest tests/run/ -q`
Expected: PASS — the new test passes and `test_happy_path_reaches_pr_and_persists` / `test_persist_failure_is_non_fatal` / `test_feedback_failure_does_not_lose_savepoint` (which assert `type == "savepoint"`) stay green.

- [ ] **Step 5: Commit**

```bash
git add src/kagura_engineer/run/__init__.py tests/run/test_orchestrator.py
git commit -m "feat(run): build ship savepoint from outcome_spec (outcome/source tags)"
```

---

## Task 4: Preemptive failure surfacing + acceptance test

**Files:**
- Modify: `src/kagura_engineer/run/__init__.py`
- Modify: `tests/run/test_orchestrator.py`

Lead grounding with prior failures of *this* issue via a tag-filtered recall, and prove with an acceptance test that a re-run sees a past failure. Requires making `_FakeMemory` tag-aware.

- [ ] **Step 1: Make `_FakeMemory` tag-aware (test infra)**

In `tests/run/test_orchestrator.py`, replace the `_FakeMemory.recall_detailed` and `remember` methods so it records failure memories and returns them for a `failure`-tagged recall:

```python
    def recall_detailed(self, context_id, query, *, k=5, tags=None, min_importance=0.0):
        if tags and "failure" in tags:
            return [(f"f{i}", s) for i, (t, s) in enumerate(self.remembered) if t == "failure"][:k]
        return [("m1", "decision A")]

    def remember(self, context_id, *, summary, content, type, tags=None):
        self.remembered.append((type, summary)); return "mem-1"
```

(The real `MemoryClient.recall_detailed` already accepts `tags`/`min_importance`; this aligns the fake.)

- [ ] **Step 2: Write the failing acceptance test**

Add to `tests/run/test_orchestrator.py`:

```python
def test_prior_failure_surfaces_in_grounding_on_rerun(monkeypatch):
    # ACCEPTANCE: after a run fails at a phase, a later run of the same issue
    # must lead its grounding with that failure memory.
    captured = {}
    monkeypatch.setattr("kagura_engineer.run.run_all",
                        lambda cfg: [CheckResult("gh-issue-driven", Status.OK, "x")])
    monkeypatch.setattr("kagura_engineer.run.ensure_worktree",
                        lambda root, issue, base="HEAD": Path(f"/wt/run-{issue}"))
    mem = _FakeMemory()

    # 1) first run: red at start → failure memory stored
    monkeypatch.setattr("kagura_engineer.run.invoke_phase",
                        lambda p, i, wt, g, **kw: PhaseInvocation(p, 0, "", "", "red", None))
    run_idea(_cfg(), 42, memory=mem, repo_root=Path("/repo"))
    assert any(t == "failure" for t, _ in mem.remembered)

    # 2) second run: capture the grounding handed to the phase
    def _invoke(p, i, wt, grounding, **kw):
        captured["grounding"] = list(grounding)
        return PhaseInvocation(p, 0, "", "", "green", "https://x/pull/42")

    monkeypatch.setattr("kagura_engineer.run.invoke_phase", _invoke)
    # clear the done/halt state so the second run actually re-runs
    mem.state.pop("run:42", None)
    run_idea(_cfg(), 42, memory=mem, repo_root=Path("/repo"))

    assert any("halted at start" in g for g in captured["grounding"]), \
        "prior failure memory must surface in the re-run's grounding"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python -m pytest tests/run/test_orchestrator.py::test_prior_failure_surfaces_in_grounding_on_rerun -q`
Expected: FAIL — grounding does not yet include the failure memory (no failure-tagged recall wired).

- [ ] **Step 4: Add the failure recall to the recall phase**

In `run_idea`, the recall phase currently builds `grounding = pinned + [s for _, s in recalled]`. Replace that line and the explore block with a unified builder that leads with failures. Find:

```python
        recalled_ids = [mid for mid, _ in recalled]
        grounding = pinned + [s for _, s in recalled]
        resumed = mem.get_state(cfg.context_id, _state_key(issue))
```

Replace with:

```python
        recalled_ids = [mid for mid, _ in recalled]
        resumed = mem.get_state(cfg.context_id, _state_key(issue))
        grounding = list(pinned)
        seen = set(grounding)

        def _add_grounding(summary: str) -> None:
            if summary and summary not in seen and len(grounding) < _GROUNDING_CAP:
                grounding.append(summary)
                seen.add(summary)

        # 1c. preemptive failure surfacing: prior failures of THIS issue lead the
        # grounding so a re-run sees "you failed here before". Best-effort — a
        # failure-recall hiccup must not fail the (hard-FAIL) recall phase.
        try:
            for _, summary in mem.recall_detailed(
                cfg.context_id, f"issue {issue} prior failures", k=3, tags=["failure"]
            ):
                _add_grounding(summary)
        except Exception:  # noqa: BLE001 — preemptive recall is best-effort
            _log.exception("run failure recall failed (non-fatal)")

        for _, summary in recalled:
            _add_grounding(summary)
```

Then update the explore block (1a) to reuse `_add_grounding` and the existing `seen`/cap, replacing its inline append:

```python
    if recalled:
        try:
            for _, summary in mem.explore(cfg.context_id, recalled[0][0], depth=1):
                _add_grounding(summary)
        except Exception:  # noqa: BLE001 — graph enrichment is best-effort
            _log.exception("run explore enrichment failed (non-fatal)")
```

(Remove the now-duplicate `seen = set(grounding)` line that was inside the old explore block.)

- [ ] **Step 5: Run the run suite**

Run: `python -m pytest tests/run/ -q`
Expected: PASS — the acceptance test passes; `test_grounding_enriched_with_explore_neighbors` and `test_explore_failure_does_not_fail_recall` still pass (explore still appends via `_add_grounding`).

- [ ] **Step 6: Commit**

```bash
git add src/kagura_engineer/run/__init__.py tests/run/test_orchestrator.py
git commit -m "feat(run): preemptively surface prior failures of an issue in grounding"
```

---

## Task 5: Docs + decision reconciliation + full verification

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Update the README Plan 5+ row**

In `README.md`, the Status table row for `Plan 5+`. Split out the now-done piece. Replace:

```
| Plan 5+ | rich graph/feedback/Sleep, memory auto-store, worktree runs — **Memory Cloud required** | 📋 planned |
```

with:

```
| Plan 5+ | memory auto-store / failure-mode learning (capture + preemptive surfacing) | ✅ done |
| Plan 6+ | graph `prevents` edges, Sleep consolidation, parallel worktree runs — **Memory Cloud required** | 📋 planned |
```

- [ ] **Step 2: Add a CHANGELOG entry**

In `CHANGELOG.md`, under `## [Unreleased]`, add:

```markdown
### Added

- Failure-mode learning: `run` auto-stores failure memories (gate halt / phase
  FAIL) and an outcome memory on ship, and preemptively surfaces an issue's
  prior failures in grounding. Trust-labelled (`source:harness`), structured
  summaries only, length-capped detail. Works on both memory backends.
```

- [ ] **Step 3: Run the full test suite**

Run: `python -m pytest -q`
Expected: PASS (all green; was 390 + new tests).

- [ ] **Step 4: Build + check the package**

Run: `rm -rf dist && uv build && uvx twine check dist/*`
Expected: both artifacts PASSED.

- [ ] **Step 5: Update the decision memory (manual note for the operator)**

After merge, update Kagura memory `d0021270` to record: v1 auto-store landed and is **backend-agnostic** (the Cloud-required boundary now applies only to the v2 graph edges + Sleep). This is a memory-tool action, not a code change.

- [ ] **Step 6: Commit**

```bash
git add README.md CHANGELOG.md
git commit -m "docs: mark memory auto-store v1 done; reframe Plan 6+ (edges/Sleep/parallel)"
```

---

## Self-review

**Spec coverage:**
- Goal 1 (failure memories at halt/FAIL) → Task 1 (`gate_halt_spec`, `phase_fail_spec`) + Task 2 (hooks). ✅
- Goal 2 (outcome memory on ship) → Task 1 (`outcome_spec`) + Task 3. ✅
- Goal 3 (preemptive surfacing) → Task 4 + its acceptance test. ✅
- Goal 4 (best-effort, after resume marker, `--no-remember`) → Task 2 `_capture` (gating + try/except; gate-halt capture placed after `set_state`). ✅
- Trust & safety (structured, capped, `source:harness`) → Task 1 (`_DETAIL_CAP`, `_base_tags`) + tests `test_phase_fail_spec_caps_detail`. ✅
- Backend scope (no Protocol/backend/config change) → confirmed: only `remember` + `recall_detailed(tags=...)`, both already on the Protocol. ✅
- Acceptance test mandate → Task 4 Step 2. ✅
- v2 deferral (edges/Sleep/dedup) → not implemented; README Plan 6+ row + CHANGELOG reflect it. ✅

**Placeholder scan:** No TBD/TODO; every code step shows complete code. ✅

**Type consistency:** `MemorySpec(summary, content, type, tags, importance)` used identically across builders and `_capture`/persist call sites. `phase_fail_spec(repo, issue, phase, kind, detail="")` signature matches all four call sites (worktree/launch/claude-exit). `recall_detailed(..., tags=[...])` matches the real Protocol and the updated fake. ✅

---

## Execution handoff

This plan executes **after 0.1.0 ships**. When ready, dispatch task-by-task (subagent-driven recommended) or inline via executing-plans.
