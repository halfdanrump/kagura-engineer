# Plan 3 `run` — Implementation Plan

> [!NOTE]
> Historical design record. It may describe pre-implementation state, internal
> plan numbers, or behavior that has since changed. See the
> [current README](../../README.md) and [document map](../README.md).

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement `kagura-engineer run <issue#>` as a memory-grounded agent loop that drives `gh-issue-driven` (via headless `claude -p`) from a GitHub issue to an open PR, with a HITL gate and Memory Cloud recall/persist.

**Architecture:** A thin Python orchestrator (`run/__init__.py`) runs the loop guard→recall→worktree→start→ship→persist. Each external boundary is its own focused module behind a function interface: `memory.py` (Memory Cloud SDK wrap behind a `MemoryClient` Protocol), `worktree.py` (git worktree isolation), `workflow.py` (one `claude -p` invocation per gh-issue-driven phase, with a machine-readable verdict marker), `gate.py` (verdict→proceed/halt). Mirrors the existing `doctor`/`setup` patterns exactly: frozen dataclasses for results, a rich-table + JSON renderer, and `subprocess.run` for every external command.

**Tech Stack:** Python 3.11+, typer, rich, pydantic (config only), `kagura-memory` SDK (`KaguraClient`), pytest with monkeypatch at every external boundary.

---

## Design refinements vs the design doc (`docs/plan/plan-3-run.md`)

Three small, deliberate deltas surfaced while mapping the spec to existing code. They do not change scope:

1. **Worktree uses plain `subprocess.run(["git", "worktree", ...])`.** The spec's `command git` note (§6) is a *developer shell* caveat (RTK rewrites the agent's Bash-tool `git`), not product behavior — a Python subprocess calls real `git` directly with no RTK in the path.
2. **Verdict capture via an injected `KAGURA_VERDICT=<green|yellow|red>` marker** (and `KAGURA_PR_URL=<url>`). We instruct the headless prompt to emit these lines, then parse them. This is robust and unit-testable, and avoids depending on gh-issue-driven's internal output format. Missing/unparseable marker → treated as `red` → HITL halt (spec §3.2 "verdict 不明は red").
3. **Guard = doctor blocking-check verification only; `run` does NOT auto-invoke `setup`.** Running `setup` (which can `sudo`-install) inside `run` is too much implicit action. `run` verifies the environment via `doctor.registry.run_all` and exits 2 with a "run `kagura-engineer setup`" hint if any blocking check fails. "Control before automation."

---

## File Structure

**Create:**
- `src/kagura_engineer/run/__init__.py` — orchestrator `run_idea()` + `STATUS_EXIT` map
- `src/kagura_engineer/run/result.py` — `RunStatus`, `PhaseResult`, `RunReport`
- `src/kagura_engineer/run/memory.py` — `MemoryClient` Protocol, `KaguraCloudClient`
- `src/kagura_engineer/run/worktree.py` — `worktree_path`, `ensure_worktree`, `remove_worktree`
- `src/kagura_engineer/run/workflow.py` — `build_prompt`, `parse_verdict`, `parse_pr_url`, `invoke_phase`, `PhaseInvocation`
- `src/kagura_engineer/run/gate.py` — `GateDecision`, `evaluate`
- `src/kagura_engineer/run/render.py` — `print_table`, `to_json`
- `tests/run/__init__.py` (empty)
- `tests/run/test_result.py`, `test_memory.py`, `test_worktree.py`, `test_workflow.py`, `test_gate.py`, `test_orchestrator.py`, `test_render.py`

**Modify:**
- `pyproject.toml` — add `kagura-memory>=0.29` dependency
- `src/kagura_engineer/doctor/checks.py` — add `check_gh_issue_driven()`
- `src/kagura_engineer/doctor/registry.py` — register the new check
- `src/kagura_engineer/cli.py:119-123` — replace the `run` stub
- `tests/test_cli.py` — replace `test_run_not_implemented` with run exit-code tests
- `tests/doctor/test_checks.py`, `tests/doctor/test_registry.py` — cover the new check
- `README.md` — update the Plan 3 status line

---

## Task 1: `run/result.py` — result data model

**Files:**
- Create: `src/kagura_engineer/run/__init__.py` (empty for now — created so the package imports)
- Create: `src/kagura_engineer/run/result.py`
- Test: `tests/run/__init__.py` (empty), `tests/run/test_result.py`

- [ ] **Step 1: Create empty package files**

Create `src/kagura_engineer/run/__init__.py` with a single line:

```python
"""Plan 3 `run` — memory-grounded agent loop (idea→PR)."""
```

Create `tests/run/__init__.py` empty (zero bytes).

- [ ] **Step 2: Write the failing test**

Create `tests/run/test_result.py`:

```python
from kagura_engineer.run.result import PhaseResult, RunReport, RunStatus


def test_status_values():
    assert RunStatus.OK.value == "ok"
    assert RunStatus.BLOCKED.value == "blocked"
    assert RunStatus.FAIL.value == "fail"


def test_report_status_is_worst_phase():
    ok = PhaseResult("recall", RunStatus.OK, "done")
    blocked = PhaseResult("start", RunStatus.BLOCKED, "red verdict", verdict="red")
    failed = PhaseResult("ship", RunStatus.FAIL, "claude exited 1")

    assert RunReport(issue=1, phases=[ok]).status is RunStatus.OK
    assert RunReport(issue=1, phases=[ok, blocked]).status is RunStatus.BLOCKED
    assert RunReport(issue=1, phases=[ok, blocked, failed]).status is RunStatus.FAIL


def test_empty_report_is_ok():
    assert RunReport(issue=1).status is RunStatus.OK


def test_phase_result_defaults():
    p = PhaseResult("recall", RunStatus.OK, "done")
    assert p.verdict is None
    assert p.duration_s == 0.0
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/run/test_result.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'kagura_engineer.run.result'`

- [ ] **Step 4: Write minimal implementation**

Create `src/kagura_engineer/run/result.py`:

```python
"""Result data model for the `run` command.

Mirrors `setup/result.py` and `doctor/result.py`: frozen dataclasses,
a string Enum, and an aggregate with a derived `status`. `run` walks a
fixed phase sequence (guard → recall → worktree → start → ship →
persist); each phase lands in one of three terminal states:

    OK       — phase completed
    BLOCKED  — a gate halted the run (red/unknown verdict) or a blocking
               guard check failed; the run is resumable
    FAIL     — hard error (claude exited non-zero, timeout, SDK auth)

`RunReport.status` is the worst phase status (FAIL > BLOCKED > OK); the
CLI maps it to an exit code (0/1/2) via `STATUS_EXIT` in __init__.py.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field


class RunStatus(enum.Enum):
    OK = "ok"
    BLOCKED = "blocked"
    FAIL = "fail"


_WORST = {RunStatus.OK: 0, RunStatus.BLOCKED: 1, RunStatus.FAIL: 2}


@dataclass(frozen=True)
class PhaseResult:
    name: str
    status: RunStatus
    detail: str
    verdict: str | None = None
    duration_s: float = 0.0


@dataclass(frozen=True)
class RunReport:
    issue: int
    phases: list[PhaseResult] = field(default_factory=list)
    pr_url: str | None = None
    worktree: str | None = None
    resume_hint: str | None = None
    duration_s: float = 0.0

    @property
    def status(self) -> RunStatus:
        if not self.phases:
            return RunStatus.OK
        return max(self.phases, key=lambda p: _WORST[p.status]).status
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/run/test_result.py -v`
Expected: PASS (4 passed)

- [ ] **Step 6: Commit**

```bash
git add src/kagura_engineer/run/__init__.py src/kagura_engineer/run/result.py tests/run/__init__.py tests/run/test_result.py
git commit -m "feat(run): add RunStatus/PhaseResult/RunReport result model"
```

---

## Task 2: `pyproject.toml` + `run/memory.py` — MemoryClient

**Files:**
- Modify: `pyproject.toml` (dependencies)
- Create: `src/kagura_engineer/run/memory.py`
- Test: `tests/run/test_memory.py`

- [ ] **Step 1: Add the SDK dependency**

In `pyproject.toml`, change the `dependencies` list (currently `typer`, `rich`, `pydantic`, `pyyaml`) to add one line so it reads:

```toml
dependencies = [
    "typer>=0.12",
    "rich>=13.7",
    "pydantic>=2.7",
    "pyyaml>=6.0",
    "kagura-memory>=0.29",
]
```

- [ ] **Step 2: Write the failing test**

Create `tests/run/test_memory.py`:

```python
from kagura_engineer.run.memory import KaguraCloudClient, MemoryClient


class _FakeSDK:
    """Stand-in for kagura_memory.KaguraClient with recorded calls."""

    def __init__(self):
        self.calls = []

    def recall(self, context_id, query="", k=5, filters=None, **kw):
        self.calls.append(("recall", context_id, query, k, filters))
        return {"results": [{"summary": "past decision A"}, {"summary": "pattern B"}, {"no_summary": 1}]}

    def load_pinned(self, context_id, cap=None):
        self.calls.append(("load_pinned", context_id))
        return {"memories": [{"summary": "guardrail: TDD required"}]}

    def remember(self, context_id, summary, content, type="note", **kw):
        self.calls.append(("remember", context_id, summary, type))
        return {"memory_id": "mem-123"}

    def get_state(self, context_id, key=None):
        self.calls.append(("get_state", context_id, key))
        return {"value": {"phase": "start"}}

    def set_state(self, context_id, key, value, **kw):
        self.calls.append(("set_state", context_id, key, value))
        return {"ok": True}


def test_recall_returns_summary_strings_and_skips_missing():
    sdk = _FakeSDK()
    client = KaguraCloudClient(sdk)
    out = client.recall("ctx", "issue 42 context", k=3)
    assert out == ["past decision A", "pattern B"]
    # trust_tier filter is passed through as a filters dict
    name, ctx, query, k, filters = sdk.calls[-1]
    assert ctx == "ctx" and k == 3
    assert filters == {"trust_tier": "trusted"}


def test_load_pinned_returns_summary_strings():
    client = KaguraCloudClient(_FakeSDK())
    assert client.load_pinned("ctx") == ["guardrail: TDD required"]


def test_remember_returns_memory_id():
    client = KaguraCloudClient(_FakeSDK())
    mid = client.remember("ctx", summary="s", content="c", type="savepoint")
    assert mid == "mem-123"


def test_get_state_unwraps_value():
    client = KaguraCloudClient(_FakeSDK())
    assert client.get_state("ctx", "run:42") == {"phase": "start"}


def test_set_state_passes_value():
    sdk = _FakeSDK()
    KaguraCloudClient(sdk).set_state("ctx", "run:42", {"done": True})
    assert sdk.calls[-1] == ("set_state", "ctx", "run:42", {"done": True})


def test_kagura_cloud_client_satisfies_protocol():
    client: MemoryClient = KaguraCloudClient(_FakeSDK())
    assert isinstance(client, MemoryClient)  # runtime_checkable
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/run/test_memory.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'kagura_engineer.run.memory'`

- [ ] **Step 4: Write minimal implementation**

Create `src/kagura_engineer/run/memory.py`:

```python
"""Memory Cloud client for the `run` agent loop.

`MemoryClient` is the narrow Protocol the orchestrator depends on — just
the five methods the loop needs (recall / load_pinned / remember /
get_state / set_state). `KaguraCloudClient` wraps the `kagura-memory`
SDK's `KaguraClient` and normalizes its dict responses into the simple
shapes the loop wants (recall/load_pinned → list[str] of summaries,
get_state → the stored value or None).

Two impls are anticipated (design doc §5): this `KaguraCloudClient` now,
a `LocalMemoryClient` (SQLite, offline) in Plan 5. Keeping the Protocol
narrow means tests use an in-memory fake and never touch the network.
"""
from __future__ import annotations

import os
from typing import Protocol, runtime_checkable

from ..config import Config


@runtime_checkable
class MemoryClient(Protocol):
    def load_pinned(self, context_id: str) -> list[str]: ...
    def recall(self, context_id: str, query: str, *, k: int = 5) -> list[str]: ...
    def remember(
        self, context_id: str, *, summary: str, content: str, type: str,
        tags: list[str] | None = None,
    ) -> str: ...
    def get_state(self, context_id: str, key: str) -> dict | None: ...
    def set_state(self, context_id: str, key: str, value: dict) -> None: ...


# Recalls that influence what the agent does are behaviour-influencing
# reads; the trusted tier excludes external/connector-ingested memories
# (OWASP LLM01/LLM03), matching the session-start bootstrap policy.
_TRUST_FILTER = {"trust_tier": "trusted"}


class KaguraCloudClient:
    """Adapter over `kagura_memory.KaguraClient`."""

    def __init__(self, sdk) -> None:
        self._sdk = sdk

    @classmethod
    def from_config(cls, cfg: Config) -> "KaguraCloudClient":
        import kagura_memory

        sdk = kagura_memory.KaguraClient(
            api_key=os.environ.get("KAGURA_API_KEY"),
            mcp_url=cfg.memory_cloud_url,
        )
        return cls(sdk)

    def load_pinned(self, context_id: str) -> list[str]:
        resp = self._sdk.load_pinned(context_id)
        return [m["summary"] for m in resp.get("memories", []) if m.get("summary")]

    def recall(self, context_id: str, query: str, *, k: int = 5) -> list[str]:
        resp = self._sdk.recall(context_id, query=query, k=k, filters=_TRUST_FILTER)
        return [r["summary"] for r in resp.get("results", []) if r.get("summary")]

    def remember(
        self, context_id: str, *, summary: str, content: str, type: str,
        tags: list[str] | None = None,
    ) -> str:
        resp = self._sdk.remember(
            context_id, summary=summary, content=content, type=type, tags=tags
        )
        return resp.get("memory_id", "")

    def get_state(self, context_id: str, key: str) -> dict | None:
        resp = self._sdk.get_state(context_id, key)
        if not resp:
            return None
        return resp.get("value")

    def set_state(self, context_id: str, key: str, value: dict) -> None:
        self._sdk.set_state(context_id, key, value)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/run/test_memory.py -v`
Expected: PASS (6 passed)

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml src/kagura_engineer/run/memory.py tests/run/test_memory.py
git commit -m "feat(run): add MemoryClient Protocol + KaguraCloudClient SDK wrap"
```

---

## Task 3: `run/worktree.py` — git worktree isolation

**Files:**
- Create: `src/kagura_engineer/run/worktree.py`
- Test: `tests/run/test_worktree.py`

> Note: product code calls real `git` via `subprocess.run(["git", ...])`. The `command git` RTK workaround is only for the developer's interactive shell, not here.

- [ ] **Step 1: Write the failing test**

Create `tests/run/test_worktree.py`:

```python
import subprocess
from pathlib import Path

import pytest

from kagura_engineer.run import worktree


def test_worktree_path_is_outside_repo_and_named_by_issue(tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    p = worktree.worktree_path(repo, 42)
    assert p.name == "run-42"
    assert "myrepo" in str(p)
    assert repo not in p.parents  # lives in a sibling .kagura-runs tree, not inside the repo


def test_ensure_worktree_resumes_when_path_exists(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    existing = worktree.worktree_path(repo, 7)
    existing.mkdir(parents=True)
    called = []
    monkeypatch.setattr(worktree.subprocess, "run", lambda *a, **k: called.append(a))
    out = worktree.ensure_worktree(repo, 7)
    assert out == existing
    assert called == []  # resume path: no git invocation


def test_ensure_worktree_creates_when_absent(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    cmds = []

    def _fake_run(cmd, **kw):
        cmds.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(worktree.subprocess, "run", _fake_run)
    out = worktree.ensure_worktree(repo, 9, base="main")
    assert out == worktree.worktree_path(repo, 9)
    assert cmds[0][:3] == ["git", "worktree", "add"]
    assert "main" in cmds[0]


def test_ensure_worktree_raises_on_git_failure(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(
        worktree.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "fatal: bad base"),
    )
    with pytest.raises(worktree.WorktreeError):
        worktree.ensure_worktree(repo, 9)


def test_remove_worktree_calls_git_remove_force(tmp_path, monkeypatch):
    cmds = []
    monkeypatch.setattr(
        worktree.subprocess, "run",
        lambda cmd, **kw: cmds.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""),
    )
    worktree.remove_worktree(Path("/tmp/run-1"))
    assert cmds[0] == ["git", "worktree", "remove", "--force", "/tmp/run-1"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/run/test_worktree.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'kagura_engineer.run.worktree'`

- [ ] **Step 3: Write minimal implementation**

Create `src/kagura_engineer/run/worktree.py`:

```python
"""Per-run git worktree isolation.

Each `run <issue#>` gets its own worktree named `run-<issue#>`, placed
OUTSIDE the repo working tree (in a sibling `.kagura-runs/<repo-name>/`
dir) so it never pollutes the repo's `git status`. The name is
deterministic so a resumed run finds the same worktree.

Product code uses plain `subprocess.run(["git", ...])` — real git, no
RTK proxy in the path (RTK only rewrites the agent's Bash-tool git).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

_TIMEOUT_S = 30


class WorktreeError(RuntimeError):
    """A `git worktree` command failed."""


def worktree_root(repo_root: Path) -> Path:
    """Sibling dir that holds this repo's run worktrees."""
    return repo_root.parent / ".kagura-runs" / repo_root.name


def worktree_path(repo_root: Path, issue: int) -> Path:
    return worktree_root(repo_root) / f"run-{issue}"


def ensure_worktree(repo_root: Path, issue: int, *, base: str = "HEAD") -> Path:
    """Return the worktree path, creating it off `base` if absent.

    If the path already exists this is a resume: return it untouched.
    """
    path = worktree_path(repo_root, issue)
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["git", "worktree", "add", str(path), base],
        cwd=repo_root, capture_output=True, text=True, timeout=_TIMEOUT_S,
    )
    if proc.returncode != 0:
        raise WorktreeError(f"git worktree add failed: {proc.stderr.strip()}")
    return path


def remove_worktree(path: Path) -> None:
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(path)],
        capture_output=True, text=True, timeout=_TIMEOUT_S,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/run/test_worktree.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add src/kagura_engineer/run/worktree.py tests/run/test_worktree.py
git commit -m "feat(run): add worktree isolation (run-<issue#>, resumable)"
```

---

## Task 4: `run/gate.py` — verdict gate

**Files:**
- Create: `src/kagura_engineer/run/gate.py`
- Test: `tests/run/test_gate.py`

- [ ] **Step 1: Write the failing test**

Create `tests/run/test_gate.py`:

```python
import pytest

from kagura_engineer.run.gate import GateDecision, evaluate


@pytest.mark.parametrize("verdict", ["green", "GREEN", "yellow", "Yellow"])
def test_green_and_yellow_proceed(verdict):
    d = evaluate(verdict)
    assert isinstance(d, GateDecision)
    assert d.proceed is True


@pytest.mark.parametrize("verdict", ["red", "RED", "", "  ", None, "garbage"])
def test_red_unknown_and_missing_halt(verdict):
    d = evaluate(verdict)
    assert d.proceed is False


def test_decision_records_normalized_verdict():
    assert evaluate("GREEN").verdict == "green"
    assert evaluate(None).verdict == "unknown"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/run/test_gate.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'kagura_engineer.run.gate'`

- [ ] **Step 3: Write minimal implementation**

Create `src/kagura_engineer/run/gate.py`:

```python
"""HITL gate: turn a gh-issue-driven verdict into proceed/halt.

The dial for v1 is fixed ON: green/yellow proceed, everything else
(red, unknown, missing) halts and surfaces to the human. Defaulting the
unknown case to halt is the safe direction — better to stop and show the
human than to mis-read a verdict and let an autonomous run barrel ahead
(`trust before integration`). `--unattended` (dial toward auto-continue)
is a later plan.
"""
from __future__ import annotations

from dataclasses import dataclass

_PROCEED = {"green", "yellow"}


@dataclass(frozen=True)
class GateDecision:
    proceed: bool
    verdict: str


def evaluate(verdict: str | None) -> GateDecision:
    v = (verdict or "").strip().lower()
    if v in _PROCEED:
        return GateDecision(True, v)
    return GateDecision(False, v or "unknown")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/run/test_gate.py -v`
Expected: PASS (10 passed)

- [ ] **Step 5: Commit**

```bash
git add src/kagura_engineer/run/gate.py tests/run/test_gate.py
git commit -m "feat(run): add verdict gate (green/yellow proceed, else halt)"
```

---

## Task 5: `run/workflow.py` — headless `claude -p` phase invocation

**Files:**
- Create: `src/kagura_engineer/run/workflow.py`
- Test: `tests/run/test_workflow.py`

- [ ] **Step 1: Write the failing test**

Create `tests/run/test_workflow.py`:

```python
import subprocess
from pathlib import Path

from kagura_engineer.run import workflow
from kagura_engineer.run.workflow import PhaseInvocation


def test_build_prompt_includes_command_grounding_and_marker_request():
    prompt = workflow.build_prompt("start", 42, ["guardrail: TDD", "decision A"])
    assert "/gh-issue-driven:start" in prompt
    assert "42" in prompt
    assert "guardrail: TDD" in prompt
    assert "KAGURA_VERDICT=" in prompt  # we instruct the session to emit the marker


def test_build_prompt_handles_empty_grounding():
    prompt = workflow.build_prompt("ship", 1, [])
    assert "/gh-issue-driven:ship" in prompt


def test_parse_verdict_reads_last_marker():
    text = "blah\nKAGURA_VERDICT=green\nmore\nKAGURA_VERDICT=red\n"
    assert workflow.parse_verdict(text) == "red"


def test_parse_verdict_returns_none_when_absent():
    assert workflow.parse_verdict("no marker here") is None


def test_parse_pr_url_reads_marker():
    assert workflow.parse_pr_url("KAGURA_PR_URL=https://github.com/o/r/pull/5\n") == "https://github.com/o/r/pull/5"


def test_parse_pr_url_none_when_absent_or_dash():
    assert workflow.parse_pr_url("KAGURA_PR_URL=-\n") is None
    assert workflow.parse_pr_url("nothing") is None


def test_invoke_phase_runs_claude_in_worktree_and_parses(monkeypatch, tmp_path):
    def _fake_run(cmd, **kw):
        assert cmd[0] == "claude" and "-p" in cmd
        assert kw["cwd"] == tmp_path
        return subprocess.CompletedProcess(
            cmd, 0, "work...\nKAGURA_VERDICT=green\nKAGURA_PR_URL=https://x/pull/1\n", ""
        )

    monkeypatch.setattr(workflow.subprocess, "run", _fake_run)
    inv = workflow.invoke_phase("ship", 3, tmp_path, ["g"])
    assert isinstance(inv, PhaseInvocation)
    assert inv.verdict == "green"
    assert inv.pr_url == "https://x/pull/1"
    assert inv.returncode == 0


def test_invoke_phase_nonzero_returncode_keeps_output(monkeypatch, tmp_path):
    monkeypatch.setattr(
        workflow.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "boom"),
    )
    inv = workflow.invoke_phase("start", 3, tmp_path, [])
    assert inv.returncode == 1
    assert inv.verdict is None
    assert "boom" in inv.stderr


def test_invoke_phase_timeout_returns_marker(monkeypatch, tmp_path):
    def _raise(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(workflow.subprocess, "run", _raise)
    inv = workflow.invoke_phase("start", 3, tmp_path, [])
    assert inv.returncode == -1
    assert inv.timed_out is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/run/test_workflow.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'kagura_engineer.run.workflow'`

- [ ] **Step 3: Write minimal implementation**

Create `src/kagura_engineer/run/workflow.py`:

```python
"""Drive one gh-issue-driven phase via a headless `claude -p` call.

We do NOT depend on gh-issue-driven's internal output format. Instead the
prompt instructs the session to print two machine-readable marker lines
at the very end:

    KAGURA_VERDICT=<green|yellow|red>
    KAGURA_PR_URL=<url|->

`invoke_phase` runs `claude -p <prompt>` with the worktree as cwd, then
parses those markers. A missing verdict marker parses to None, which the
gate treats as a halt (safe default).

Phases are separate `claude -p` calls because gh-issue-driven checkpoints
to the branch + memory between phases, so each call resumes cleanly.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

_PHASE_TIMEOUT_S = 1800  # 30 min per phase

_VERDICT_RE = re.compile(r"^KAGURA_VERDICT=(\w+)\s*$", re.MULTILINE)
_PR_RE = re.compile(r"^KAGURA_PR_URL=(\S+)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class PhaseInvocation:
    phase: str
    returncode: int
    stdout: str
    stderr: str
    verdict: str | None
    pr_url: str | None
    timed_out: bool = False


def build_prompt(phase: str, issue: int, grounding: list[str]) -> str:
    context = "\n".join(f"- {g}" for g in grounding) or "- (no prior memory)"
    return (
        "You are running inside an automated kagura-engineer run.\n"
        "Relevant memory (recall + pinned guardrails):\n"
        f"{context}\n\n"
        f"Run the slash command `/gh-issue-driven:{phase} {issue}` to completion.\n"
        "When finished, print these two lines LAST, exactly:\n"
        "KAGURA_VERDICT=<green|yellow|red>   (the phase gate verdict)\n"
        "KAGURA_PR_URL=<pull-request-url or - if none>\n"
    )


def parse_verdict(text: str) -> str | None:
    matches = _VERDICT_RE.findall(text or "")
    return matches[-1].lower() if matches else None


def parse_pr_url(text: str) -> str | None:
    matches = _PR_RE.findall(text or "")
    if not matches:
        return None
    url = matches[-1]
    return None if url == "-" else url


def invoke_phase(
    phase: str, issue: int, worktree: Path, grounding: list[str],
    *, timeout: int = _PHASE_TIMEOUT_S,
) -> PhaseInvocation:
    prompt = build_prompt(phase, issue, grounding)
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt],
            cwd=worktree, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return PhaseInvocation(phase, -1, "", "timed out", None, None, timed_out=True)
    return PhaseInvocation(
        phase, proc.returncode, proc.stdout, proc.stderr,
        parse_verdict(proc.stdout), parse_pr_url(proc.stdout),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/run/test_workflow.py -v`
Expected: PASS (9 passed)

- [ ] **Step 5: Commit**

```bash
git add src/kagura_engineer/run/workflow.py tests/run/test_workflow.py
git commit -m "feat(run): add claude -p phase invocation + verdict/PR marker parsing"
```

---

## Task 6: doctor `gh-issue-driven` blocking check (#9)

**Files:**
- Modify: `src/kagura_engineer/doctor/checks.py` (add `check_gh_issue_driven`)
- Modify: `src/kagura_engineer/doctor/registry.py:15-22` (register it)
- Test: `tests/doctor/test_checks.py`, `tests/doctor/test_registry.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/doctor/test_checks.py`:

```python
def test_check_gh_issue_driven_ok_when_plugin_present(tmp_path, monkeypatch):
    from kagura_engineer.doctor import checks
    from kagura_engineer.doctor.result import Status

    plugins = tmp_path / "plugins"
    (plugins / "cache" / "gh-issue-driven" / "gh-issue-driven" / "0.13.0" / "commands").mkdir(parents=True)
    monkeypatch.setenv("KAGURA_PLUGINS_DIR", str(plugins))
    res = checks.check_gh_issue_driven()
    assert res.status is Status.OK


def test_check_gh_issue_driven_fail_when_absent(tmp_path, monkeypatch):
    from kagura_engineer.doctor import checks
    from kagura_engineer.doctor.result import Status

    monkeypatch.setenv("KAGURA_PLUGINS_DIR", str(tmp_path / "empty"))
    res = checks.check_gh_issue_driven()
    assert res.status is Status.FAIL
    assert res.is_blocking is True  # FAIL ⇒ blocking; run guard refuses to start
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/doctor/test_checks.py -k gh_issue_driven -v`
Expected: FAIL with `AttributeError: module ... has no attribute 'check_gh_issue_driven'`

- [ ] **Step 3: Add the check**

In `src/kagura_engineer/doctor/checks.py`, add `from pathlib import Path` to the imports, then append this function:

```python
def check_gh_issue_driven() -> CheckResult:
    """Verify the gh-issue-driven plugin is installed.

    `run` (Plan 3) drives gh-issue-driven via headless claude; without the
    plugin the run would die deep inside a session. This is a blocking
    check (Status.FAIL ⇒ CheckResult.is_blocking), so `run`'s guard can
    refuse to start. Plugin root is overridable via KAGURA_PLUGINS_DIR
    for tests.
    """
    root = Path(os.environ.get("KAGURA_PLUGINS_DIR", Path.home() / ".claude" / "plugins"))
    hits = [p for p in root.glob("**/gh-issue-driven") if p.is_dir()] if root.exists() else []
    if hits:
        return CheckResult("gh-issue-driven", Status.OK, "plugin installed")
    return CheckResult(
        "gh-issue-driven",
        Status.FAIL,
        "gh-issue-driven plugin not found",
        "install the gh-issue-driven Claude Code plugin (run requires it)",
    )
```

- [ ] **Step 4: Register the check**

In `src/kagura_engineer/doctor/registry.py`, add one entry to the `_CHECKS` list (after the `memory-cloud` line):

```python
    ("memory-cloud", lambda c: checks.check_memory_cloud(c.memory_cloud_url)),
    ("gh-issue-driven", lambda c: checks.check_gh_issue_driven()),
```

- [ ] **Step 5: Update the two registry tests for the 7th check**

`tests/doctor/test_registry.py` has TWO tests that stub the 6 checks and assert a 6-name set. Both must learn about `check_gh_issue_driven`.

In `test_run_all_invokes_every_check`, after the `check_memory_cloud` monkeypatch line (line 20), add:

```python
    monkeypatch.setattr(registry.checks, "check_gh_issue_driven", _stub("gh-issue-driven"))
```

Change its expected-set assertion to add `"gh-issue-driven"` and change `assert len(calls) == 6` to `== 7`:

```python
    assert {r.name for r in results} == {
        "git",
        "claude-code",
        "gh",
        "ollama",
        "haiku",
        "memory-cloud",
        "gh-issue-driven",
    }
    assert len(calls) == 7
```

In `test_run_all_isolates_check_exceptions`, after the `check_memory_cloud` monkeypatch block (ends ~line 82), add:

```python
    monkeypatch.setattr(
        registry.checks,
        "check_gh_issue_driven",
        lambda: CheckResult("gh-issue-driven", Status.OK, "ok"),
    )
```

And add `"gh-issue-driven"` to that test's expected-set assertion (the second `{r.name for r in results} == {...}` block).

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/doctor/ -v`
Expected: PASS (all doctor tests, including the two new gh-issue-driven cases)

- [ ] **Step 7: Commit**

```bash
git add src/kagura_engineer/doctor/checks.py src/kagura_engineer/doctor/registry.py tests/doctor/test_checks.py tests/doctor/test_registry.py
git commit -m "feat(doctor): add blocking gh-issue-driven plugin check (#9)"
```

---

## Task 7: `run/render.py` — table + JSON

**Files:**
- Create: `src/kagura_engineer/run/render.py`
- Test: `tests/run/test_render.py`

- [ ] **Step 1: Write the failing test**

Create `tests/run/test_render.py`:

```python
import json

from kagura_engineer.run.render import to_json
from kagura_engineer.run.result import PhaseResult, RunReport, RunStatus


def test_to_json_shape():
    report = RunReport(
        issue=42,
        phases=[
            PhaseResult("recall", RunStatus.OK, "3 memories"),
            PhaseResult("start", RunStatus.BLOCKED, "red verdict", verdict="red", duration_s=1.2),
        ],
        pr_url=None,
        worktree="/tmp/.kagura-runs/repo/run-42",
        resume_hint="re-run `kagura-engineer run 42`",
        duration_s=2.5,
    )
    data = json.loads(to_json(report))
    assert data["issue"] == 42
    assert data["status"] == "blocked"
    assert data["pr_url"] is None
    assert data["resume_hint"].startswith("re-run")
    assert data["phases"][1]["verdict"] == "red"
    assert data["phases"][1]["duration_s"] == 1.2


def test_print_table_smoke(capsys):
    from kagura_engineer.run.render import print_table

    print_table(RunReport(issue=1, phases=[PhaseResult("ship", RunStatus.OK, "PR opened")], pr_url="https://x/pull/1"))
    out = capsys.readouterr().out
    assert "ship" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/run/test_render.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'kagura_engineer.run.render'`

- [ ] **Step 3: Write minimal implementation**

Create `src/kagura_engineer/run/render.py`:

```python
"""Renderers for `RunReport` (rich table + JSON). Mirrors setup/render.py."""
from __future__ import annotations

import json

from rich.console import Console
from rich.table import Table

from .result import PhaseResult, RunReport, RunStatus

_ICON: dict[RunStatus, str] = {
    RunStatus.OK: "✅",
    RunStatus.BLOCKED: "⏸",
    RunStatus.FAIL: "❌",
}


def _phase_to_dict(p: PhaseResult) -> dict:
    return {
        "name": p.name,
        "status": p.status.value,
        "detail": p.detail,
        "verdict": p.verdict,
        "duration_s": round(p.duration_s, 3),
    }


def to_json(report: RunReport) -> str:
    return json.dumps(
        {
            "issue": report.issue,
            "status": report.status.value,
            "pr_url": report.pr_url,
            "worktree": report.worktree,
            "resume_hint": report.resume_hint,
            "phases": [_phase_to_dict(p) for p in report.phases],
            "duration_s": round(report.duration_s, 3),
        },
        ensure_ascii=False,
    )


def print_table(report: RunReport) -> None:
    table = Table(title=f"kagura-engineer run #{report.issue} — {report.status.value}")
    table.add_column("")
    table.add_column("phase")
    table.add_column("status")
    table.add_column("verdict")
    table.add_column("detail")
    for p in report.phases:
        table.add_row(_ICON[p.status], p.name, p.status.value, p.verdict or "", p.detail)
    console = Console()
    console.print(table)
    if report.pr_url:
        console.print(f"PR: {report.pr_url}")
    if report.resume_hint:
        console.print(f"resume: {report.resume_hint}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/run/test_render.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add src/kagura_engineer/run/render.py tests/run/test_render.py
git commit -m "feat(run): add RunReport table + JSON renderer"
```

---

## Task 8: `run/__init__.py` — the agent loop orchestrator

**Files:**
- Modify: `src/kagura_engineer/run/__init__.py`
- Test: `tests/run/test_orchestrator.py`

This is the heart: `run_idea(cfg, issue, *, no_remember, memory, repo_root)` runs guard→recall→worktree→start→ship→persist and returns a `RunReport`. All external boundaries (`run_all`, `invoke_phase`, `ensure_worktree`, `memory`) are injectable/monkeypatchable so the loop is tested without real git/claude/network.

- [ ] **Step 1: Write the failing test**

Create `tests/run/test_orchestrator.py`:

```python
from pathlib import Path

from kagura_engineer.doctor.result import CheckResult, Status
from kagura_engineer.run import run_idea, STATUS_EXIT
from kagura_engineer.run.result import RunStatus
from kagura_engineer.run.workflow import PhaseInvocation
from tests._constants import (
    VALID_CONTEXT_UUID, VALID_MEMORY_URL, VALID_PROFILE, VALID_WORKSPACE,
)
from kagura_engineer.config import Config


def _cfg() -> Config:
    return Config(
        profile=VALID_PROFILE, memory_cloud_url=VALID_MEMORY_URL,
        workspace_id=VALID_WORKSPACE, context_id=VALID_CONTEXT_UUID,
    )


class _FakeMemory:
    def __init__(self):
        self.state = {}
        self.remembered = []

    def load_pinned(self, context_id): return ["guardrail: TDD"]
    def recall(self, context_id, query, *, k=5): return ["decision A"]
    def remember(self, context_id, *, summary, content, type, tags=None):
        self.remembered.append((type, summary)); return "mem-1"
    def get_state(self, context_id, key): return self.state.get(key)
    def set_state(self, context_id, key, value): self.state[key] = value


def _patch_boundaries(monkeypatch, *, blocking=False, phases=None):
    """Patch guard/worktree/workflow. `phases` maps phase->PhaseInvocation."""
    checks = [CheckResult("gh-issue-driven", Status.FAIL if blocking else Status.OK, "x")]
    monkeypatch.setattr("kagura_engineer.run.run_all", lambda cfg: checks)
    monkeypatch.setattr("kagura_engineer.run.ensure_worktree", lambda root, issue, base="HEAD": Path(f"/wt/run-{issue}"))
    phases = phases or {}

    def _invoke(phase, issue, worktree, grounding, **kw):
        return phases[phase]

    monkeypatch.setattr("kagura_engineer.run.invoke_phase", _invoke)


def test_status_exit_map():
    assert STATUS_EXIT[RunStatus.OK] == 0
    assert STATUS_EXIT[RunStatus.FAIL] == 1
    assert STATUS_EXIT[RunStatus.BLOCKED] == 2


def test_guard_blocks_when_doctor_has_blocking_fail(monkeypatch):
    _patch_boundaries(monkeypatch, blocking=True)
    mem = _FakeMemory()
    report = run_idea(_cfg(), 42, memory=mem, repo_root=Path("/repo"))
    assert report.status is RunStatus.BLOCKED
    assert report.phases[0].name == "guard"
    assert "setup" in report.resume_hint.lower()
    assert mem.remembered == []  # never got to act/persist


def test_happy_path_reaches_pr_and_persists(monkeypatch):
    _patch_boundaries(monkeypatch, phases={
        "start": PhaseInvocation("start", 0, "", "", "green", None),
        "ship": PhaseInvocation("ship", 0, "", "", "green", "https://x/pull/9"),
    })
    mem = _FakeMemory()
    report = run_idea(_cfg(), 42, memory=mem, repo_root=Path("/repo"))
    assert report.status is RunStatus.OK
    assert report.pr_url == "https://x/pull/9"
    assert [p.name for p in report.phases] == ["guard", "recall", "worktree", "start", "ship", "persist"]
    assert any(t == "savepoint" for t, _ in mem.remembered)
    assert mem.state.get("run:42") is not None  # resume marker set to done


def test_red_verdict_at_start_halts_and_sets_resume(monkeypatch):
    _patch_boundaries(monkeypatch, phases={
        "start": PhaseInvocation("start", 0, "", "", "red", None),
    })
    mem = _FakeMemory()
    report = run_idea(_cfg(), 42, memory=mem, repo_root=Path("/repo"))
    assert report.status is RunStatus.BLOCKED
    assert report.phases[-1].name == "start"
    assert report.phases[-1].verdict == "red"
    assert mem.state.get("run:42") is not None  # resume state persisted
    assert "run 42" in report.resume_hint


def test_phase_nonzero_returncode_is_fail(monkeypatch):
    _patch_boundaries(monkeypatch, phases={
        "start": PhaseInvocation("start", 1, "", "boom", None, None),
    })
    report = run_idea(_cfg(), 42, memory=_FakeMemory(), repo_root=Path("/repo"))
    assert report.status is RunStatus.FAIL
    assert report.phases[-1].name == "start"


def test_no_remember_skips_persist(monkeypatch):
    _patch_boundaries(monkeypatch, phases={
        "start": PhaseInvocation("start", 0, "", "", "green", None),
        "ship": PhaseInvocation("ship", 0, "", "", "green", "https://x/pull/9"),
    })
    mem = _FakeMemory()
    report = run_idea(_cfg(), 42, memory=mem, no_remember=True, repo_root=Path("/repo"))
    assert report.status is RunStatus.OK
    assert mem.remembered == []  # recall still happened, persist skipped
    assert "persist" not in [p.name for p in report.phases]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/run/test_orchestrator.py -v`
Expected: FAIL with `ImportError: cannot import name 'run_idea' from 'kagura_engineer.run'`

- [ ] **Step 3: Write the orchestrator**

Replace the contents of `src/kagura_engineer/run/__init__.py` with:

```python
"""Plan 3 `run` — memory-grounded agent loop (idea→PR).

`run_idea` walks a fixed phase sequence and returns a `RunReport`:

    guard    → doctor blocking-check verification (no auto-setup)
    recall   → load_pinned + recall + get_state (grounding / resume)
    worktree → ensure run-<issue#> worktree (resumable)
    start    → claude -p /gh-issue-driven:start → gate
    ship     → claude -p /gh-issue-driven:ship  → gate → PR
    persist  → remember(savepoint) + set_state(done)   (skipped by --no-remember)

A red/unknown gate verdict halts with BLOCKED and a resume hint; a
non-zero claude exit is FAIL. Every external boundary (`run_all`,
`ensure_worktree`, `invoke_phase`, the `MemoryClient`) is imported at
module scope so tests can monkeypatch them.
"""
from __future__ import annotations

import time
from pathlib import Path

from ..config import Config
from ..doctor.registry import run_all
from .gate import evaluate
from .memory import KaguraCloudClient, MemoryClient
from .result import PhaseResult, RunReport, RunStatus
from .worktree import ensure_worktree
from .workflow import invoke_phase

STATUS_EXIT: dict[RunStatus, int] = {
    RunStatus.OK: 0,
    RunStatus.FAIL: 1,
    RunStatus.BLOCKED: 2,
}

_PHASES = ("start", "ship")


def _state_key(issue: int) -> str:
    return f"run:{issue}"


def run_idea(
    cfg: Config,
    issue: int,
    *,
    no_remember: bool = False,
    memory: MemoryClient | None = None,
    repo_root: Path | None = None,
) -> RunReport:
    mem = memory if memory is not None else KaguraCloudClient.from_config(cfg)
    root = repo_root if repo_root is not None else Path.cwd()
    started = time.monotonic()
    phases: list[PhaseResult] = []

    def _finish(*, pr_url=None, worktree=None, resume_hint=None) -> RunReport:
        return RunReport(
            issue=issue, phases=phases, pr_url=pr_url, worktree=worktree,
            resume_hint=resume_hint, duration_s=time.monotonic() - started,
        )

    # 0. guard — verify, do not auto-provision.
    blocking = [c for c in run_all(cfg) if c.is_blocking]
    if blocking:
        names = ", ".join(c.name for c in blocking)
        phases.append(PhaseResult("guard", RunStatus.BLOCKED, f"blocking checks failed: {names}"))
        return _finish(resume_hint="run `kagura-engineer setup` to fix the environment, then retry")

    # 1. recall — grounding + resume point.
    grounding = mem.load_pinned(cfg.context_id) + mem.recall(
        cfg.context_id, f"issue {issue} implementation context", k=5
    )
    resumed = mem.get_state(cfg.context_id, _state_key(issue))
    detail = f"{len(grounding)} memories" + (" (resuming)" if resumed else "")
    phases.append(PhaseResult("recall", RunStatus.OK, detail))

    # 2. worktree.
    wt = ensure_worktree(root, issue)
    phases.append(PhaseResult("worktree", RunStatus.OK, str(wt)))

    # 3-4. act: start, then ship.
    pr_url = None
    for phase in _PHASES:
        inv = invoke_phase(phase, issue, wt, grounding)
        if inv.returncode != 0:
            tail = inv.stderr.strip()[-200:] if inv.stderr else ("timed out" if inv.timed_out else "")
            phases.append(PhaseResult(phase, RunStatus.FAIL, f"claude exited {inv.returncode}: {tail}"))
            return _finish(worktree=str(wt))
        decision = evaluate(inv.verdict)
        if not decision.proceed:
            mem.set_state(cfg.context_id, _state_key(issue), {"halted_at": phase, "verdict": decision.verdict})
            phases.append(PhaseResult(phase, RunStatus.BLOCKED, f"gate halt ({decision.verdict})", verdict=decision.verdict))
            return _finish(
                worktree=str(wt),
                resume_hint=f"review the {phase} gate, then re-run `kagura-engineer run {issue}`",
            )
        pr_url = inv.pr_url or pr_url
        phases.append(PhaseResult(phase, RunStatus.OK, f"{phase} ok", verdict=decision.verdict))

    # 5. persist.
    if not no_remember:
        mem.remember(
            cfg.context_id,
            summary=f"run #{issue} → PR {pr_url or '(no url)'}",
            content=f"kagura-engineer run drove issue #{issue} to {pr_url}",
            type="savepoint",
            tags=["repo:kagura-engineer", "run", f"issue:{issue}"],
        )
        mem.set_state(cfg.context_id, _state_key(issue), {"done": True, "pr_url": pr_url})
        phases.append(PhaseResult("persist", RunStatus.OK, "savepoint stored"))

    return _finish(pr_url=pr_url, worktree=str(wt))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/run/test_orchestrator.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add src/kagura_engineer/run/__init__.py tests/run/test_orchestrator.py
git commit -m "feat(run): add agent-loop orchestrator (guard→recall→worktree→start→ship→persist)"
```

---

## Task 9: CLI wiring — replace the `run` stub

**Files:**
- Modify: `src/kagura_engineer/cli.py:114-123` (the run section)
- Modify: `tests/test_cli.py` (replace `test_run_not_implemented`)

- [ ] **Step 1: Write the failing tests**

In `tests/test_cli.py`, DELETE `test_run_not_implemented` (lines 177-180) and add these tests (place them after the setup tests):

```python
def _stub_run_report(status):
    from kagura_engineer.run.result import PhaseResult, RunReport, RunStatus
    return RunReport(
        issue=42,
        phases=[PhaseResult("guard", status, "x")],
        pr_url="https://x/pull/1" if status is RunStatus.OK else None,
        resume_hint=None if status is RunStatus.OK else "re-run",
    )


def test_run_exit_0_on_ok(write_cfg, monkeypatch):
    from kagura_engineer.run.result import RunStatus
    monkeypatch.setattr("kagura_engineer.cli.run_idea", lambda *a, **kw: _stub_run_report(RunStatus.OK))
    result = runner.invoke(app, ["run", "42", "--config", str(write_cfg)])
    assert result.exit_code == 0


def test_run_exit_1_on_fail(write_cfg, monkeypatch):
    from kagura_engineer.run.result import RunStatus
    monkeypatch.setattr("kagura_engineer.cli.run_idea", lambda *a, **kw: _stub_run_report(RunStatus.FAIL))
    result = runner.invoke(app, ["run", "42", "--config", str(write_cfg)])
    assert result.exit_code == 1


def test_run_exit_2_on_blocked(write_cfg, monkeypatch):
    from kagura_engineer.run.result import RunStatus
    monkeypatch.setattr("kagura_engineer.cli.run_idea", lambda *a, **kw: _stub_run_report(RunStatus.BLOCKED))
    result = runner.invoke(app, ["run", "42", "--config", str(write_cfg)])
    assert result.exit_code == 2


def test_run_json_emits_report(write_cfg, monkeypatch):
    import json
    from kagura_engineer.run.result import RunStatus
    monkeypatch.setattr("kagura_engineer.cli.run_idea", lambda *a, **kw: _stub_run_report(RunStatus.OK))
    result = runner.invoke(app, ["run", "42", "--config", str(write_cfg), "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["issue"] == 42 and data["status"] == "ok"


def test_run_no_remember_propagates(write_cfg, monkeypatch):
    from kagura_engineer.run.result import RunStatus
    captured = {}

    def _spy(cfg, issue, **kw):
        captured.update(kw); captured["issue"] = issue
        return _stub_run_report(RunStatus.OK)

    monkeypatch.setattr("kagura_engineer.cli.run_idea", _spy)
    runner.invoke(app, ["run", "7", "--config", str(write_cfg), "--no-remember"])
    assert captured["issue"] == 7
    assert captured.get("no_remember") is True


def test_run_missing_config_clean_error(tmp_path):
    missing = tmp_path / "nope.yaml"
    result = runner.invoke(app, ["run", "42", "--config", str(missing)])
    assert result.exit_code == 2
    assert "config" in result.output.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cli.py -k run -v`
Expected: FAIL (the new tests fail because `run` still has the old signature / no `run_idea` import)

- [ ] **Step 3: Replace the run command**

In `src/kagura_engineer/cli.py`, update the imports near the top (after the existing setup imports) to add:

```python
from .run import STATUS_EXIT, run_idea
from .run.render import print_table as run_print_table
from .run.render import to_json as run_to_json
```

Then replace the entire run section (`cli.py:114-123`, from the `# run (Plan 3 placeholder)` comment through the stub function) with:

```python
# ---------------------------------------------------------------------------
# run (Plan 3 — memory-grounded agent loop)
# ---------------------------------------------------------------------------


@app.command()
def run(
    issue: int = typer.Argument(..., help="GitHub issue number to drive to a PR"),
    config: str = _CONFIG_OPT,
    no_remember: bool = typer.Option(
        False, "--no-remember", help="skip memory persist (recall still happens)"
    ),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Drive a GitHub issue to a PR via the memory-grounded agent loop.

    Exit codes: 0 = PR reached · 1 = hard fail · 2 = blocked
    (guard / gate halt — resumable by re-running).
    """
    try:
        cfg = load_config(config)
    except ConfigError as exc:
        typer.echo(f"run: invalid config '{config}': {exc}", err=True)
        raise typer.Exit(code=2)

    report = run_idea(cfg, issue, no_remember=no_remember)

    if json_out:
        typer.echo(run_to_json(report))
    else:
        run_print_table(report)

    raise typer.Exit(code=STATUS_EXIT[report.status])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cli.py -v`
Expected: PASS (all CLI tests, including the 6 new run tests; `test_help_lists_commands` still passes)

- [ ] **Step 5: Commit**

```bash
git add src/kagura_engineer/cli.py tests/test_cli.py
git commit -m "feat(run): wire run command (issue arg, exit-code contract, --json/--no-remember)"
```

---

## Task 10: Full suite + README + final E2E smoke

**Files:**
- Modify: `README.md` (Plan 3 status line)
- Test: full suite

- [ ] **Step 1: Run the whole suite**

Run: `pytest -q`
Expected: PASS, ~210+ tests, no failures. If any doctor/registry count test still asserts the old check count, fix it now (Task 6 Step 5).

- [ ] **Step 2: Update README**

In `README.md`, update the Plan 3 status row (line ~22) from:

```
| **Plan 3** | `run` — the idea-mode / task pipeline | 🚧 in design |
```

to:

```
| **Plan 3** | `run` — memory-grounded agent loop (issue→PR) | ✅ done |
```

And update the `run` section (lines ~135-138) from:

```
### `kagura-engineer run`

The idea-mode pipeline. **Not implemented yet (Plan 3)** — currently prints a
notice and exits 2.
```

to:

```
### `kagura-engineer run`

The memory-grounded agent loop. `run <issue#>` verifies the environment,
recalls relevant memory, isolates a worktree, drives `gh-issue-driven`
start→ship via headless `claude -p` (HITL gate on red/unknown verdicts),
and opens a PR — persisting a savepoint to Memory Cloud.

```
kagura-engineer run 42                 # drive issue #42 to a PR
kagura-engineer run 42 --no-remember   # recall but don't persist
kagura-engineer run 42 --json
```

Exit codes: `0` PR reached · `1` hard fail · `2` blocked (guard or gate
halt — resumable by re-running).
```

Also replace README line 25 exactly — from:

```
`doctor` and `setup` are runnable now (186 tests green). `run` is a stub.
```

to:

```
`doctor`, `setup`, and `run` are runnable now (210+ tests green).
```

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs(readme): mark Plan 3 run as done"
```

- [ ] **Step 4: Verify the command is wired (no network)**

Run: `kagura-engineer run --help`
Expected: shows the `issue` argument and `--no-remember` / `--json` options, exit 0.

Run: `kagura-engineer run 1 --config /nonexistent.yaml`
Expected: exit 2, "run: invalid config" on stderr.

---

## Self-Review (run after completing all tasks)

- [ ] **Spec coverage:** every design-doc section maps to a task — §2.2 layout → Tasks 1-8; §3 loop → Task 8; §3.2 verdict → Task 5; §4.1 context_id (project ctx + `run:<issue#>` state) → Task 8; §4.2 is_blocking → Task 6; §5 memory → Task 2; §6 worktree → Task 3; §7 errors → Tasks 5+8; §2.5 exit codes → Tasks 8+9.
- [ ] **No placeholders:** every step has real code/commands.
- [ ] **Type consistency:** `PhaseInvocation(phase, returncode, stdout, stderr, verdict, pr_url, timed_out)`, `RunReport.status`, `STATUS_EXIT`, `MemoryClient` 5 methods, `evaluate()→GateDecision(proceed, verdict)`, `ensure_worktree(root, issue, base=)` — names used identically in Tasks 5/8/9.
```
