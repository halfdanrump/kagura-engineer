# Brain Backend Selection Implementation Plan

> [!NOTE]
> Historical design record. It may describe pre-implementation state, internal
> plan numbers, or behavior that has since changed. See the
> [current README](../../../README.md) and [document map](../../README.md).

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a kagura-engineer run target `claude` (default) or `codex` (incl. Ollama Cloud) as the brain backend, selected from `repo.yaml`, without losing the backend-agnostic out-of-band memory grounding.

**Architecture:** A single `select_brain(cfg, env) -> BrainCall` factory resolves `Config` + env into the chosen `kagura_brain` adapter's `invoke` plus per-backend kwargs. It confines the claude/codex MCP asymmetry to one place (claude gets `mcp_config`/`allowed_tools`; codex does not). The two existing call sites (`run/workflow.py::invoke_phase`, `review/fixer.py::run_fixer`) receive a resolved `BrainCall` instead of importing `claude` directly. The doctor guard checks the selected backend's CLI.

**Tech Stack:** Python 3.11+, pydantic v2 (`Config`), `kagura-brain>=0.2.0` (claude/codex adapters + doctor), pytest.

**Spec:** `docs/superpowers/specs/2026-06-10-brain-backend-selection-design.md`

---

## File Structure

- `pyproject.toml` — bump `kagura-brain` pin to `>=0.2.0,<0.3` (Task 1).
- `src/kagura_engineer/config.py` — add `brain_backend`, `brain_endpoint` fields (Task 2).
- `src/kagura_engineer/run/brain_select.py` — **new**: `BrainCall` + `select_brain()` (Task 3).
- `src/kagura_engineer/run/workflow.py` — `invoke_phase` takes a `BrainCall` (Task 4).
- `src/kagura_engineer/run/__init__.py` — resolve `select_brain` once, thread into `invoke_phase` (Task 4).
- `src/kagura_engineer/review/fixer.py` — `run_fixer` takes a `BrainCall` (Task 5).
- `src/kagura_engineer/review/loop.py` — resolve `select_brain`, thread into `run_fixer` (Task 5).
- `src/kagura_engineer/doctor/checks.py` + `doctor/registry.py` — backend-aware CLI check (Task 6).

Run the whole suite with `python -m pytest -q` (CI gate = `test`).

---

### Task 1: Bump the kagura-brain dependency to 0.2.0

**Files:**
- Modify: `pyproject.toml:48`
- Test: `tests/test_version.py` (add a pin assertion)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_version.py`:

```python
import tomllib
from pathlib import Path


def test_kagura_brain_pinned_to_0_2_plus():
    # Backend selection (codex adapter + doctor.check) needs kagura-brain >= 0.2.0;
    # the old `<0.2` pin also excluded brain #11's CLAUDE_* security scrub.
    data = tomllib.loads(Path("pyproject.toml").read_text())
    deps = data["project"]["dependencies"]
    brain = [d for d in deps if d.replace(" ", "").startswith("kagura-brain")]
    assert brain == ["kagura-brain>=0.2.0,<0.3"], brain
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_version.py::test_kagura_brain_pinned_to_0_2_plus -v`
Expected: FAIL — current pin is `kagura-brain>=0.1.0,<0.2`.

- [ ] **Step 3: Edit the pin**

In `pyproject.toml`, change the dependency line (currently `    "kagura-brain>=0.1.0,<0.2",`) to:

```toml
    "kagura-brain>=0.2.0,<0.3",
```

- [ ] **Step 4: Sync the environment and run the test**

Run: `uv sync 2>&1 | tail -3 && python -m pytest tests/test_version.py -v`
Expected: PASS. `uv.lock` now resolves `kagura-brain==0.2.x`.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock tests/test_version.py
git commit -m "build(deps): require kagura-brain>=0.2.0 (#51)

0.2.0 ships the codex adapter, doctor primitives, and the CLAUDE_* env
scrub (#11); the old <0.2 pin excluded all three. Refs #51, #50."
```

---

### Task 2: Add `brain_backend` / `brain_endpoint` to Config

**Files:**
- Modify: `src/kagura_engineer/config.py` (Config field block, ~line 68)
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_config.py`:

```python
from kagura_engineer.config import Config, ConfigError, load_config


def _minimal_local() -> dict:
    # local backend needs no cloud creds — keeps these tests focused on the new fields
    return {"profile": "p", "memory_backend": "local"}


def test_brain_backend_defaults_to_claude_no_endpoint():
    cfg = Config.model_validate(_minimal_local())
    assert cfg.brain_backend == "claude"
    assert cfg.brain_endpoint == ""


def test_brain_backend_accepts_codex_and_endpoint():
    cfg = Config.model_validate(
        {**_minimal_local(), "brain_backend": "codex", "brain_endpoint": "ollama-cloud"}
    )
    assert cfg.brain_backend == "codex"
    assert cfg.brain_endpoint == "ollama-cloud"


def test_brain_backend_rejects_unknown_value():
    import pytest
    with pytest.raises(Exception):  # pydantic ValidationError for the Literal
        Config.model_validate({**_minimal_local(), "brain_backend": "gpt"})


def test_unknown_brain_key_still_forbidden():
    # extra="forbid" must still catch a typo'd new field
    import pytest
    with pytest.raises(Exception):
        Config.model_validate({**_minimal_local(), "brain_backendd": "codex"})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_config.py -k brain -v`
Expected: FAIL — `brain_backend`/`brain_endpoint` are unknown keys (rejected by `extra="forbid"`).

- [ ] **Step 3: Add the fields**

In `src/kagura_engineer/config.py`, immediately after the `memory_failover` field (line 68) and before `def resolve_mcp_config`, add:

```python
    # Brain backend (issue #51). "claude" (default) drives Claude Code; "codex"
    # drives the Codex CLI (incl. Ollama Cloud via brain_endpoint). The default
    # reproduces today's behaviour byte-for-byte.
    brain_backend: Literal["claude", "codex"] = "claude"
    # Optional caller-chosen endpoint (non-secret URL/alias only — NEVER a key):
    #   claude -> an Anthropic-compatible gateway URL
    #   codex  -> "ollama-cloud" (alias for Ollama Cloud) or an OpenAI-compatible URL
    # The API key is resolved from the KAGURA_BRAIN_API_KEY env var (see
    # run/brain_select.py), never from repo.yaml.
    brain_endpoint: str = ""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_config.py -k brain -v`
Expected: PASS (all four).

- [ ] **Step 5: Commit**

```bash
git add src/kagura_engineer/config.py tests/test_config.py
git commit -m "feat(config): add brain_backend/brain_endpoint (#51)"
```

---

### Task 3: `BrainCall` + `select_brain` factory

**Files:**
- Create: `src/kagura_engineer/run/brain_select.py`
- Test: `tests/run/test_brain_select.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/run/test_brain_select.py`:

```python
import pytest

from kagura_engineer.config import Config, ConfigError
from kagura_engineer.mcp import MEMORY_TOOLS
from kagura_engineer.run.brain_select import BrainCall, select_brain


def _cfg(**over) -> Config:
    base = {"profile": "p", "memory_backend": "local"}
    base.update(over)
    return Config.model_validate(base)


class _Spy:
    """Records the kwargs an adapter.invoke received."""
    def __init__(self):
        self.kwargs = None
    def __call__(self, prompt, **kwargs):
        self.kwargs = kwargs
        return "RESULT"


def test_default_is_claude_with_mcp_tools():
    call = select_brain(_cfg(), env={})
    assert call.backend == "claude"
    assert call.supports_mcp is True
    spy = _Spy()
    object.__setattr__(call, "_invoke", spy)
    call.invoke("hi", cwd=None, timeout=1, mcp_config="/x/.mcp.json")
    assert spy.kwargs["mcp_config"] == "/x/.mcp.json"
    assert spy.kwargs["allowed_tools"] == MEMORY_TOOLS
    assert "endpoint" not in spy.kwargs  # no endpoint set


def test_codex_gets_no_mcp_kwargs():
    call = select_brain(_cfg(brain_backend="codex"), env={})
    assert call.backend == "codex"
    assert call.supports_mcp is False
    spy = _Spy()
    object.__setattr__(call, "_invoke", spy)
    call.invoke("hi", cwd=None, timeout=1, mcp_config="/x/.mcp.json")
    # The asymmetry: codex.invoke MUST NOT receive these (they are unknown kwargs).
    assert "mcp_config" not in spy.kwargs
    assert "allowed_tools" not in spy.kwargs


def test_mcp_enabled_is_false_for_codex_even_with_config():
    claude_call = select_brain(_cfg(), env={})
    codex_call = select_brain(_cfg(brain_backend="codex"), env={})
    assert claude_call.mcp_enabled("/x/.mcp.json") is True
    assert codex_call.mcp_enabled("/x/.mcp.json") is False
    assert claude_call.mcp_enabled(None) is False


def test_endpoint_passes_through_with_api_key_from_env():
    call = select_brain(
        _cfg(brain_backend="codex", brain_endpoint="ollama-cloud"),
        env={"KAGURA_BRAIN_API_KEY": "sk-test"},
    )
    spy = _Spy()
    object.__setattr__(call, "_invoke", spy)
    call.invoke("hi", cwd=None, timeout=1, mcp_config=None)
    assert spy.kwargs["endpoint"] == "ollama-cloud"
    assert spy.kwargs["api_key"] == "sk-test"


def test_endpoint_without_api_key_raises_configerror():
    with pytest.raises(ConfigError, match="KAGURA_BRAIN_API_KEY"):
        select_brain(_cfg(brain_endpoint="ollama-cloud"), env={})


def test_subscription_claude_needs_no_api_key():
    # default claude, no endpoint -> no key required, no endpoint kwarg
    call = select_brain(_cfg(), env={})
    assert call.api_key is None
    assert call.endpoint is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/run/test_brain_select.py -v`
Expected: FAIL — module `kagura_engineer.run.brain_select` does not exist.

- [ ] **Step 3: Write the implementation**

Create `src/kagura_engineer/run/brain_select.py`:

```python
"""Resolve Config + env into the chosen kagura-brain backend (issue #51).

`select_brain` is the single point that maps `brain_backend`/`brain_endpoint`
to a `kagura_brain` adapter and its per-backend kwargs, confining the
claude/codex MCP asymmetry to one place:

  * claude — supports MCP memory tools (`mcp_config` + `allowed_tools`) and an
    Anthropic-compatible BYO endpoint.
  * codex  — takes endpoint/api_key (Ollama Cloud / BYO) but NOT MCP tools; the
    codex adapter cannot accept them today, so an in-task recall is unavailable
    and grounding falls back to engineer's out-of-band recall. Logged once.

The API key is read from the env (KAGURA_BRAIN_API_KEY), never repo.yaml, so a
secret never lands in a committed config (cf. the secret-handling discipline of
issue #47 / the memory-mcp setup step).
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from kagura_brain import claude, codex
from kagura_brain.core import BrainResult

from ..config import Config, ConfigError
from ..mcp import MEMORY_TOOLS

_log = logging.getLogger(__name__)

#: Env var supplying the API key for a BYO/Ollama-Cloud endpoint. Kept out of
#: repo.yaml so a key is never committed.
BRAIN_API_KEY_ENV = "KAGURA_BRAIN_API_KEY"


@dataclass(frozen=True)
class BrainCall:
    """A resolved backend: the adapter's `invoke` plus per-backend kwargs.

    `invoke` forwards the common args and adds the kwargs the chosen backend
    accepts — MCP tools for claude, none for codex.
    """

    backend: str
    _invoke: Callable[..., BrainResult]
    supports_mcp: bool
    endpoint: str | None = None
    api_key: str | None = None

    def mcp_enabled(self, mcp_config: str | None) -> bool:
        """Whether in-task MCP recall is actually live for this call — used by the
        prompt builder. False for codex regardless of a resolved mcp_config."""
        return self.supports_mcp and bool(mcp_config)

    def invoke(
        self, prompt: str, *, cwd: Path | None, timeout: int,
        mcp_config: str | None = None,
    ) -> BrainResult:
        kwargs: dict[str, object] = {}
        if self.endpoint:
            kwargs["endpoint"] = self.endpoint
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if self.supports_mcp:
            kwargs["mcp_config"] = mcp_config
            kwargs["allowed_tools"] = MEMORY_TOOLS
        return self._invoke(prompt, cwd=cwd, timeout=timeout, **kwargs)


def select_brain(cfg: Config, env: Mapping[str, str]) -> BrainCall:
    """Resolve the brain backend from Config + env. Raises ConfigError when an
    endpoint is set but no API key is available in the env."""
    endpoint = cfg.brain_endpoint or None
    api_key = (env.get(BRAIN_API_KEY_ENV) or "").strip() or None
    if endpoint and api_key is None:
        raise ConfigError(
            f"brain_endpoint={endpoint!r} requires an API key — "
            f"export {BRAIN_API_KEY_ENV}=... (it is never read from repo.yaml)"
        )
    if cfg.brain_backend == "codex":
        _log.warning(
            "brain_backend=codex: no in-task MCP memory tools; grounding is "
            "out-of-band recall only (codex adapter has no MCP wiring yet)"
        )
        return BrainCall(
            "codex", codex.invoke, supports_mcp=False,
            endpoint=endpoint, api_key=api_key,
        )
    return BrainCall(
        "claude", claude.invoke, supports_mcp=True,
        endpoint=endpoint, api_key=api_key,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/run/test_brain_select.py -v`
Expected: PASS (all six).

- [ ] **Step 5: Commit**

```bash
git add src/kagura_engineer/run/brain_select.py tests/run/test_brain_select.py
git commit -m "feat(run): add select_brain backend factory (#51)"
```

---

### Task 4: Route the run loop through `select_brain`

**Files:**
- Modify: `src/kagura_engineer/run/workflow.py` (imports ~line 75; `invoke_phase` ~line 299-316)
- Modify: `src/kagura_engineer/run/__init__.py` (imports ~line 30-36; phase loop ~line 213-224)
- Test: `tests/run/test_workflow.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/run/test_workflow.py` (a fake `BrainCall` that records what it got):

```python
from kagura_engineer.run.brain_select import BrainCall
from kagura_engineer.run.workflow import invoke_phase


def _fake_call(records, *, supports_mcp=True):
    def _invoke(prompt, **kwargs):
        records.append(kwargs)
        class _R:
            returncode = 0
            stdout = "KAGURA_VERDICT=green"
            stderr = ""
            timed_out = False
            def detail(self): return ""
        return _R()
    return BrainCall("fake", _invoke, supports_mcp=supports_mcp)


def test_invoke_phase_uses_the_supplied_brain_call(tmp_path):
    records: list[dict] = []
    call = _fake_call(records, supports_mcp=True)
    inv = invoke_phase(
        "implement", 7, tmp_path, ["grounding line"],
        mcp_config="/x/.mcp.json", brain_call=call,
    )
    assert inv.returncode == 0
    assert records and records[0]["mcp_config"] == "/x/.mcp.json"


def test_invoke_phase_codex_call_gets_no_mcp_kwargs(tmp_path):
    records: list[dict] = []
    call = _fake_call(records, supports_mcp=False)
    invoke_phase(
        "implement", 7, tmp_path, ["g"],
        mcp_config="/x/.mcp.json", brain_call=call,
    )
    # kwargs-swallow guard (memory f6da90d9): assert the absence explicitly.
    assert "mcp_config" not in records[0]
    assert "allowed_tools" not in records[0]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/run/test_workflow.py -k brain_call -v`
Expected: FAIL — `invoke_phase` has no `brain_call` parameter (TypeError: unexpected keyword argument).

- [ ] **Step 3a: Update `workflow.py` imports**

In `src/kagura_engineer/run/workflow.py`, remove these two lines (the direct adapter import is no longer used here):

```python
from kagura_brain import claude as brain

from ..mcp import MEMORY_TOOLS
```

and add the BrainCall import next to the other relative imports:

```python
from .brain_select import BrainCall
```

- [ ] **Step 3b: Change `invoke_phase` to take a `BrainCall`**

Replace the `invoke_phase` signature and the prompt/invoke lines. The current signature is:

```python
def invoke_phase(
    phase: str, issue: int, worktree: Path, grounding: list[str],
    *, unattended: bool = False, mcp_config: str | None = None,
    timeout: int = _PHASE_TIMEOUT_S,
) -> PhaseInvocation:
    prompt = build_prompt(phase, issue, grounding, unattended=unattended,
                          mcp_enabled=bool(mcp_config))
```

Change it to (add `brain_call: BrainCall` as a required keyword; derive `mcp_enabled` from it):

```python
def invoke_phase(
    phase: str, issue: int, worktree: Path, grounding: list[str],
    *, brain_call: BrainCall, unattended: bool = False,
    mcp_config: str | None = None, timeout: int = _PHASE_TIMEOUT_S,
) -> PhaseInvocation:
    prompt = build_prompt(phase, issue, grounding, unattended=unattended,
                          mcp_enabled=brain_call.mcp_enabled(mcp_config))
```

Then replace the `brain.invoke(...)` call:

```python
    result = brain.invoke(
        prompt, cwd=worktree, timeout=timeout,
        mcp_config=mcp_config, allowed_tools=MEMORY_TOOLS,
    )
```

with:

```python
    result = brain_call.invoke(
        prompt, cwd=worktree, timeout=timeout, mcp_config=mcp_config,
    )
```

- [ ] **Step 3c: Resolve `select_brain` in the run loop and thread it in**

In `src/kagura_engineer/run/__init__.py`, add imports (near line 24 and 36):

```python
import os
```

and

```python
from .brain_select import select_brain
from ..config import Config, ConfigError
```

(Note: `from ..config import Config` already exists at line 30 — extend it to also import `ConfigError` rather than duplicating; the final line reads `from ..config import Config, ConfigError`.)

Before the phase loop (just before `pr_url = None` at ~line 213), resolve the backend once and halt cleanly on a bad endpoint/key combo:

```python
    try:
        brain_call = select_brain(cfg, os.environ)
    except ConfigError as exc:
        _record(PhaseResult("brain", RunStatus.FAIL, f"backend config error: {exc}"))
        return _finish(worktree=str(wt))
```

Then update the `invoke_phase` call (currently lines ~223-224):

```python
            inv = invoke_phase(phase, issue, wt, grounding, unattended=unattended,
                               mcp_config=cfg.resolve_mcp_config(root))
```

to pass the resolved call:

```python
            inv = invoke_phase(phase, issue, wt, grounding, brain_call=brain_call,
                               unattended=unattended,
                               mcp_config=cfg.resolve_mcp_config(root))
```

- [ ] **Step 4: Run the run-loop tests**

Run: `python -m pytest tests/run/ -v 2>&1 | tail -20`
Expected: PASS. If a pre-existing `invoke_phase(...)` test call now errors on the required `brain_call`, update that call to pass `brain_call=_fake_call([])` (the helper from Step 1) — this is the kwargs-swallow convention surfacing the new required arg, which is intended.

- [ ] **Step 5: Commit**

```bash
git add src/kagura_engineer/run/workflow.py src/kagura_engineer/run/__init__.py tests/run/test_workflow.py
git commit -m "feat(run): route invoke_phase through select_brain (#51)"
```

---

### Task 5: Route the review-fix loop through `select_brain`

**Files:**
- Modify: `src/kagura_engineer/review/fixer.py` (imports ~line 19-21; `run_fixer` ~line 67-78)
- Modify: `src/kagura_engineer/review/loop.py` (call site ~line 91-96)
- Test: `tests/review/test_fixer.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/review/test_fixer.py`:

```python
from kagura_engineer.run.brain_select import BrainCall
from kagura_engineer.review.fixer import run_fixer


def test_run_fixer_uses_brain_call_and_omits_mcp_for_codex(tmp_path):
    records: list[dict] = []
    def _invoke(prompt, **kwargs):
        records.append(kwargs)
        class _R:
            returncode = 0
            stdout = ""
            stderr = ""
            timed_out = False
            def detail(self): return ""
        return _R()
    codex_call = BrainCall("fake-codex", _invoke, supports_mcp=False)
    res = run_fixer(tmp_path, "fix it", mcp_config="/x/.mcp.json", brain_call=codex_call)
    assert res.returncode == 0
    assert "mcp_config" not in records[0]
    assert "allowed_tools" not in records[0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/review/test_fixer.py -k brain_call -v`
Expected: FAIL — `run_fixer` has no `brain_call` parameter.

- [ ] **Step 3a: Update `fixer.py` imports**

In `src/kagura_engineer/review/fixer.py`, remove:

```python
from kagura_brain import claude as brain

from ..mcp import MEMORY_TOOLS
```

and add:

```python
from ..run.brain_select import BrainCall
```

- [ ] **Step 3b: Change `run_fixer` to take a `BrainCall`**

Replace the current signature and call:

```python
def run_fixer(
    repo: Path, prompt: str, *, mcp_config: str | None = None, timeout: int = _FIX_TIMEOUT_S
) -> FixerResult:
```

…

```python
    result = brain.invoke(
        prompt, cwd=repo, timeout=timeout,
        mcp_config=mcp_config, allowed_tools=MEMORY_TOOLS,
    )
```

with:

```python
def run_fixer(
    repo: Path, prompt: str, *, brain_call: BrainCall,
    mcp_config: str | None = None, timeout: int = _FIX_TIMEOUT_S,
) -> FixerResult:
```

…

```python
    result = brain_call.invoke(
        prompt, cwd=repo, timeout=timeout, mcp_config=mcp_config,
    )
```

- [ ] **Step 3c: Resolve and thread the call in `loop.py`**

In `src/kagura_engineer/review/loop.py`, add imports at the top with the other imports:

```python
import os

from ..run.brain_select import select_brain
from ..config import ConfigError
```

Resolve the backend once before the fix loop begins (near where `cfg` is first available, before the `while`/retry loop). If your loop has a single entry, place it right after `cfg` is in scope:

```python
    try:
        brain_call = select_brain(cfg, os.environ)
    except ConfigError as exc:
        return _finish(ReviewStatus.FAIL, f"backend config error: {exc}")
```

Then update the call site (currently line ~96):

```python
                fix = run_fixer(root, prompt, mcp_config=mcp_config)
```

to:

```python
                fix = run_fixer(root, prompt, brain_call=brain_call, mcp_config=mcp_config)
```

Also update the prompt's `mcp_enabled` (line ~93, currently `mcp_enabled=bool(mcp_config)`) to reflect the backend:

```python
            prompt = build_fix_prompt(rep.report_path, blocking or rep.findings,
                                      mcp_enabled=brain_call.mcp_enabled(mcp_config))
```

- [ ] **Step 4: Run the review tests**

Run: `python -m pytest tests/review/ -v 2>&1 | tail -20`
Expected: PASS. Update any pre-existing `run_fixer(...)` test call that now errors to pass `brain_call=BrainCall("fake", lambda p, **k: _R(), supports_mcp=True)` (reuse the Step 1 fake) — intended kwargs-swallow surfacing.

- [ ] **Step 5: Commit**

```bash
git add src/kagura_engineer/review/fixer.py src/kagura_engineer/review/loop.py tests/review/test_fixer.py
git commit -m "feat(review): route run_fixer through select_brain (#51)"
```

---

### Task 6: Backend-aware doctor guard

**Files:**
- Modify: `src/kagura_engineer/doctor/checks.py` (add `check_codex`, after `check_claude_code` ~line 86)
- Modify: `src/kagura_engineer/doctor/registry.py` (select claude vs codex check by `cfg.brain_backend`)
- Test: `tests/doctor/test_checks.py`, `tests/doctor/test_registry.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/doctor/test_checks.py`:

```python
import shutil
from kagura_engineer.doctor.checks import check_codex
from kagura_engineer.doctor.result import Status


def test_check_codex_fails_when_absent(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    res = check_codex()
    assert res.name == "codex"
    assert res.status is Status.FAIL
```

Add to `tests/doctor/test_registry.py` (mirror the existing registry test style; the assertion is that the selected CLI check matches the backend):

```python
from kagura_engineer.config import Config
from kagura_engineer.doctor.registry import run_all


def _cfg(backend: str) -> Config:
    return Config.model_validate(
        {"profile": "p", "memory_backend": "local", "brain_backend": backend}
    )


def test_registry_checks_codex_when_backend_is_codex():
    names = {c.name for c in run_all(_cfg("codex"))}
    assert "codex" in names
    assert "claude-code" not in names


def test_registry_checks_claude_when_backend_is_claude():
    names = {c.name for c in run_all(_cfg("claude"))}
    assert "claude-code" in names
    assert "codex" not in names
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/doctor/test_checks.py::test_check_codex_fails_when_absent tests/doctor/test_registry.py -k backend -v`
Expected: FAIL — `check_codex` does not exist; the registry always runs `check_claude_code`.

- [ ] **Step 3a: Add `check_codex` to `checks.py`**

In `src/kagura_engineer/doctor/checks.py`, immediately after `check_claude_code` (ends ~line 86), add a mirror that checks the codex CLI (presence + `--version`):

```python
def check_codex() -> CheckResult:
    if shutil.which("codex") is None:
        return CheckResult(
            "codex",
            Status.FAIL,
            "codex not found on PATH",
            "install the Codex CLI and re-run doctor, or set brain_backend=claude",
        )
    try:
        proc = _run(["codex", "--version"])
    except OSError as exc:
        return CheckResult(
            "codex", Status.FAIL, f"codex invocation failed: {exc}", None
        )
    if proc.returncode != 0:
        return CheckResult(
            "codex",
            Status.FAIL,
            f"`codex --version` exited {proc.returncode}",
            "reinstall the Codex CLI",
        )
    version = proc.stdout.strip() or "unknown"
    return CheckResult("codex", Status.OK, version)
```

- [ ] **Step 3b: Select the CLI check by backend in `registry.py`**

Open `src/kagura_engineer/doctor/registry.py`. It currently calls `check_claude_code()` unconditionally inside `run_all(cfg)`. Import `check_codex` alongside `check_claude_code`, and replace the unconditional claude check with a backend-keyed one:

```python
    cli_check = check_codex() if cfg.brain_backend == "codex" else check_claude_code()
```

and append `cli_check` to the results list in the same position the old `check_claude_code()` result occupied (preserve ordering so existing report tests that don't touch the brain backend still see a CLI check at the same index).

- [ ] **Step 4: Run the doctor tests**

Run: `python -m pytest tests/doctor/ -v 2>&1 | tail -20`
Expected: PASS, including the two new registry tests and the new `check_codex` test.

- [ ] **Step 5: Commit**

```bash
git add src/kagura_engineer/doctor/checks.py src/kagura_engineer/doctor/registry.py tests/doctor/test_checks.py tests/doctor/test_registry.py
git commit -m "feat(doctor): check the selected brain backend's CLI (#51)"
```

---

### Task 7: Full-suite green + lint/type sanity

**Files:** none (verification only)

- [ ] **Step 1: Run the whole suite**

Run: `python -m pytest -q 2>&1 | tail -5`
Expected: all pass (the CI `test` gate).

- [ ] **Step 2: Lint + types on the touched modules**

Run: `ruff check src/kagura_engineer/run/brain_select.py src/kagura_engineer/config.py src/kagura_engineer/doctor/checks.py && mypy src/kagura_engineer/run/brain_select.py`
Expected: clean (mypy may emit the pre-existing `kagura_memory.*` import-untyped note in unrelated modules only — not in `brain_select.py`).

- [ ] **Step 3: Manual smoke (optional, no network)**

Run: `python -c "from kagura_engineer.config import Config; from kagura_engineer.run.brain_select import select_brain; c=Config.model_validate({'profile':'p','memory_backend':'local','brain_backend':'codex','brain_endpoint':'ollama-cloud'}); print(select_brain(c, {'KAGURA_BRAIN_API_KEY':'x'}).backend)"`
Expected: prints `codex`.

---

## Self-Review

**Spec coverage:**
- Scope 1 (dep bump `>=0.2.0,<0.3`) → Task 1. ✅
- Scope 2 (Config `brain_backend`/`brain_endpoint`, secret via env) → Task 2 (fields) + Task 3 (`KAGURA_BRAIN_API_KEY`, ConfigError when endpoint without key). ✅
- Scope 3 (adapter factory confining the MCP asymmetry) → Task 3 (`BrainCall`/`select_brain`) + Tasks 4-5 (call sites). ✅
- Scope 4 (backend-aware doctor guard) → Task 6. ✅
- Scope 5 (codex-MCP wiring) → out of scope by design; the `supports_mcp` flag is the single forward-compat seam (flip to True once brain wires it). Noted, no task. ✅
- Config defaults reproduce current behaviour byte-for-byte → Task 2 default `claude`/`""`; Task 3 claude path passes the same `mcp_config`/`allowed_tools` as today. ✅
- `extra="forbid"` still catches typos → Task 2 `test_unknown_brain_key_still_forbidden`. ✅

**Placeholder scan:** No TBD/TODO; every code step shows full code; the doctor registry edit references the existing `check_claude_code()` call it replaces. ✅

**Type consistency:** `BrainCall(backend, _invoke, supports_mcp, endpoint, api_key)`, `.invoke(prompt, *, cwd, timeout, mcp_config)`, `.mcp_enabled(mcp_config)`, and `select_brain(cfg, env)` are used identically in Tasks 3-5. `check_codex() -> CheckResult` matches `check_claude_code`'s shape and the `Status`/`CheckResult` types from `doctor/result`. ✅
