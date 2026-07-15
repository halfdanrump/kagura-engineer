# Failover Memory Implementation Plan

> [!NOTE]
> Historical design record. It may describe pre-implementation state, internal
> plan numbers, or behavior that has since changed. See the
> [current README](../../../README.md) and [document map](../../README.md).

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Memory Cloud writes durable across Cloud outages by buffering critical writes (`remember` savepoint + `set_state`) to a local WAL and auto-replaying them on the next run.

**Architecture:** A bounded-composable `FailoverMemoryClient` wraps the real `KaguraCloudClient`. Reads pass through (Cloud stays primary; `recall` hard-FAIL preserved). `remember`/`set_state` try Cloud and, only on a confirmed failure, append to a durable JSONL WAL keyed by `context_id`. `run_idea` calls `drain()` at start (via a `hasattr` guard, like `close()`) to replay buffered writes. Spec: `docs/superpowers/specs/2026-06-08-failover-memory-design.md`.

**Tech Stack:** Python 3.11+, pydantic `Config`, `MemoryClient` Protocol, pytest (offline, no network).

---

## File Structure

- Create `src/kagura_engineer/run/failover_memory.py` — `FailoverMemoryClient` + `default_wal_path()`. Single responsibility: Cloud-write failover via a local WAL.
- Modify `src/kagura_engineer/config.py` — add `memory_failover: bool = True`.
- Modify `src/kagura_engineer/run/memory.py` — `resolve_memory_client` wraps the cloud client when failover is on.
- Modify `src/kagura_engineer/run/__init__.py` — drain at `run_idea` start (hasattr-guarded).
- Create `tests/run/test_failover_memory.py` — unit tests (faked inner, real temp WAL).
- Modify `tests/run/test_orchestrator.py` — assert drain runs at start and a drain failure never fails the run.
- Modify `README.md` — positioning correction (Cloud-primary/moat; local=failover buffer; Plan 5+ row accuracy).

---

## Task 1: Add `memory_failover` config field

**Files:**
- Modify: `src/kagura_engineer/config.py:40`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py`:

```python
def test_memory_failover_defaults_true():
    from kagura_engineer.config import Config
    cfg = Config(profile="coding", memory_cloud_url="https://m", workspace_id="w", context_id="c")
    assert cfg.memory_failover is True


def test_memory_failover_can_be_disabled():
    from kagura_engineer.config import Config
    cfg = Config(
        profile="coding", memory_cloud_url="https://m", workspace_id="w",
        context_id="c", memory_failover=False,
    )
    assert cfg.memory_failover is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_config.py -k memory_failover -v`
Expected: FAIL (`Config` has no field `memory_failover` / attribute error).

- [ ] **Step 3: Add the field**

In `src/kagura_engineer/config.py`, after the `memory_mcp_config` field (line 40), add:

```python
    # issue: failover memory. When the cloud backend is active, wrap the cloud
    # client so critical writes (savepoint remember + set_state) that fail during
    # a Cloud outage are buffered to a local WAL and replayed on the next run.
    # Default on for resilience; set false to use the bare cloud client.
    memory_failover: bool = True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_config.py -k memory_failover -v`
Expected: PASS (both).

- [ ] **Step 5: Commit**

```bash
git add src/kagura_engineer/config.py tests/test_config.py
git commit -m "feat(config): add memory_failover flag (default on)"
```

---

## Task 2: `FailoverMemoryClient` skeleton + read passthrough + default WAL path

**Files:**
- Create: `src/kagura_engineer/run/failover_memory.py`
- Test: `tests/run/test_failover_memory.py`

- [ ] **Step 1: Write the failing test**

Create `tests/run/test_failover_memory.py`:

```python
import json
from pathlib import Path

import pytest

from kagura_engineer.run.failover_memory import FailoverMemoryClient, default_wal_path


class _FakeInner:
    """Fake MemoryClient. Toggle `fail_writes` to make writes raise."""
    def __init__(self):
        self.fail_writes = False
        self.calls = []          # (method, args) actually delegated to inner
        self.closed = False

    # reads
    def load_pinned(self, context_id):
        self.calls.append(("load_pinned", context_id)); return ["pin"]
    def recall(self, context_id, query, *, k=5, tags=None, min_importance=0.0):
        self.calls.append(("recall", query)); return ["r"]
    def recall_detailed(self, context_id, query, *, k=5, tags=None, min_importance=0.0):
        self.calls.append(("recall_detailed", query)); return [("m1", "r")]
    def explore(self, context_id, memory_id, *, depth=1):
        self.calls.append(("explore", memory_id)); return []
    def get_state(self, context_id, key):
        self.calls.append(("get_state", key)); return {"k": key}
    # writes
    def remember(self, context_id, *, summary, content, type, tags=None):
        if self.fail_writes:
            raise RuntimeError("cloud down")
        self.calls.append(("remember", summary)); return "cloud-id"
    def set_state(self, context_id, key, value):
        if self.fail_writes:
            raise RuntimeError("cloud down")
        self.calls.append(("set_state", key))
    def feedback(self, context_id, memory_id, *, weight=1.0):
        if self.fail_writes:
            raise RuntimeError("cloud down")
        self.calls.append(("feedback", memory_id))
    def pin(self, context_id, memory_id):
        self.calls.append(("pin", memory_id))
    def unpin(self, context_id, memory_id):
        self.calls.append(("unpin", memory_id))
    def close(self):
        self.closed = True


def _client(tmp_path) -> FailoverMemoryClient:
    return FailoverMemoryClient(_FakeInner(), tmp_path / "wal.jsonl")


def test_reads_delegate_to_inner(tmp_path):
    c = _client(tmp_path)
    assert c.load_pinned("ctx") == ["pin"]
    assert c.recall("ctx", "q") == ["r"]
    assert c.recall_detailed("ctx", "q") == [("m1", "r")]
    assert c.explore("ctx", "m1") == []
    assert c.get_state("ctx", "run:1") == {"k": "run:1"}


def test_read_failure_propagates(tmp_path):
    # Cloud-primary invariant: a read failure must NOT be swallowed (recall
    # hard-FAIL must survive).
    class _Boom(_FakeInner):
        def recall(self, *a, **k):
            raise RuntimeError("cloud down")
    c = FailoverMemoryClient(_Boom(), tmp_path / "wal.jsonl")
    with pytest.raises(RuntimeError):
        c.recall("ctx", "q")


def test_default_wal_path_is_under_kagura_and_keyed_by_context(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    p = default_wal_path("ctx-123")
    assert p == tmp_path / ".kagura" / "engineer" / "wal" / "ctx-123.jsonl"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/run/test_failover_memory.py -v`
Expected: FAIL (`ModuleNotFoundError: kagura_engineer.run.failover_memory`).

- [ ] **Step 3: Write minimal implementation**

Create `src/kagura_engineer/run/failover_memory.py`:

```python
"""Cloud-primary memory with a local write-ahead log (WAL) failover.

Wraps the real cloud `MemoryClient`. Reads pass straight through (Cloud stays
the primary read path; a read failure propagates so the run's `recall` hard-FAIL
is preserved). Only the run's CRITICAL writes — `remember` (savepoint) and
`set_state` (done/halt markers) — are protected: they try the cloud and, only on
a confirmed failure, append to a durable JSONL WAL. `drain()` (called at run
start) replays the WAL to the cloud so a write that missed during a Cloud outage
lands in the moat on the next run.

Best-effort writes (`feedback`, `pin`, `unpin`) are NOT buffered — losing them on
an outage is acceptable, matching the run's best-effort side-effect policy.

See docs/superpowers/specs/2026-06-08-failover-memory-design.md.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any

from .memory import MemoryClient

_log = logging.getLogger(__name__)


def default_wal_path(context_id: str) -> Path:
    """Per-context WAL file under ~/.kagura (the existing kagura convention)."""
    return Path.home() / ".kagura" / "engineer" / "wal" / f"{context_id}.jsonl"


class FailoverMemoryClient:
    """A `MemoryClient` that buffers critical cloud writes to a local WAL."""

    def __init__(self, inner: MemoryClient, wal_path: Path) -> None:
        self._inner = inner
        self._wal_path = Path(wal_path)

    # --- reads: delegate, let failures propagate (Cloud-primary) -------------
    def load_pinned(self, context_id: str) -> list[str]:
        return self._inner.load_pinned(context_id)

    def recall(self, context_id: str, query: str, *, k: int = 5,
               tags: list[str] | None = None, min_importance: float = 0.0) -> list[str]:
        return self._inner.recall(context_id, query, k=k, tags=tags,
                                  min_importance=min_importance)

    def recall_detailed(self, context_id: str, query: str, *, k: int = 5,
                        tags: list[str] | None = None,
                        min_importance: float = 0.0) -> list[tuple[str, str]]:
        return self._inner.recall_detailed(context_id, query, k=k, tags=tags,
                                          min_importance=min_importance)

    def explore(self, context_id: str, memory_id: str, *, depth: int = 1
                ) -> list[tuple[str, str]]:
        return self._inner.explore(context_id, memory_id, depth=depth)

    def get_state(self, context_id: str, key: str) -> dict | None:
        return self._inner.get_state(context_id, key)

    def close(self) -> None:
        closer = getattr(self._inner, "close", None)
        if closer is not None:
            closer()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/run/test_failover_memory.py -v`
Expected: PASS (`test_reads_delegate_to_inner`, `test_read_failure_propagates`, `test_default_wal_path_is_under_kagura_and_keyed_by_context`).

- [ ] **Step 5: Commit**

```bash
git add src/kagura_engineer/run/failover_memory.py tests/run/test_failover_memory.py
git commit -m "feat(run): FailoverMemoryClient skeleton + read passthrough"
```

---

## Task 3: Critical writes buffer to the WAL on failure

**Files:**
- Modify: `src/kagura_engineer/run/failover_memory.py`
- Test: `tests/run/test_failover_memory.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/run/test_failover_memory.py`:

```python
def _wal_records(path):
    if not Path(path).exists():
        return []
    return [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]


def test_remember_success_does_not_buffer(tmp_path):
    inner = _FakeInner()
    c = FailoverMemoryClient(inner, tmp_path / "wal.jsonl")
    rid = c.remember("ctx", summary="s", content="x", type="savepoint", tags=["t"])
    assert rid == "cloud-id"
    assert _wal_records(tmp_path / "wal.jsonl") == []


def test_remember_failure_buffers_and_returns_wal_id(tmp_path):
    inner = _FakeInner(); inner.fail_writes = True
    c = FailoverMemoryClient(inner, tmp_path / "wal.jsonl")
    rid = c.remember("ctx", summary="s", content="x", type="savepoint", tags=["t"])
    assert rid.startswith("wal:")                      # synthetic id, no raise
    recs = _wal_records(tmp_path / "wal.jsonl")
    assert len(recs) == 1
    assert recs[0]["op"] == "remember"
    assert recs[0]["context_id"] == "ctx"
    assert recs[0]["kwargs"] == {"summary": "s", "content": "x",
                                 "type": "savepoint", "tags": ["t"]}


def test_set_state_failure_buffers(tmp_path):
    inner = _FakeInner(); inner.fail_writes = True
    c = FailoverMemoryClient(inner, tmp_path / "wal.jsonl")
    c.set_state("ctx", "run:1", {"done": True})        # must not raise
    recs = _wal_records(tmp_path / "wal.jsonl")
    assert len(recs) == 1
    assert recs[0]["op"] == "set_state"
    assert recs[0]["kwargs"] == {"key": "run:1", "value": {"done": True}}


def test_feedback_failure_is_not_buffered(tmp_path):
    inner = _FakeInner(); inner.fail_writes = True
    c = FailoverMemoryClient(inner, tmp_path / "wal.jsonl")
    with pytest.raises(RuntimeError):                   # best-effort: propagates
        c.feedback("ctx", "m1")
    assert _wal_records(tmp_path / "wal.jsonl") == []   # never buffered
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/run/test_failover_memory.py -k "buffer or not_buffered or wal_id" -v`
Expected: FAIL (`FailoverMemoryClient` has no `remember`/`set_state`/`feedback`).

- [ ] **Step 3: Write minimal implementation**

In `src/kagura_engineer/run/failover_memory.py`, add these methods to the class (after `get_state`, before `close`):

```python
    # --- critical writes: cloud-first, buffer to WAL on confirmed failure ----
    def remember(self, context_id: str, *, summary: str, content: str, type: str,
                 tags: list[str] | None = None) -> str:
        kwargs = {"summary": summary, "content": content, "type": type, "tags": tags}
        try:
            return self._inner.remember(context_id, **kwargs)
        except Exception:  # noqa: BLE001 — confirmed cloud failure → buffer
            _log.warning("cloud remember failed; buffering to WAL %s", self._wal_path)
            self._append("remember", context_id, kwargs)
            return f"wal:{uuid.uuid4().hex}"

    def set_state(self, context_id: str, key: str, value: dict) -> None:
        try:
            self._inner.set_state(context_id, key, value)
        except Exception:  # noqa: BLE001 — confirmed cloud failure → buffer
            _log.warning("cloud set_state failed; buffering to WAL %s", self._wal_path)
            self._append("set_state", context_id, {"key": key, "value": value})

    # --- best-effort writes: delegate, NOT buffered --------------------------
    def feedback(self, context_id: str, memory_id: str, *, weight: float = 1.0) -> None:
        self._inner.feedback(context_id, memory_id, weight=weight)

    def pin(self, context_id: str, memory_id: str) -> None:
        self._inner.pin(context_id, memory_id)

    def unpin(self, context_id: str, memory_id: str) -> None:
        self._inner.unpin(context_id, memory_id)

    # --- WAL append (durable) ------------------------------------------------
    def _append(self, op: str, context_id: str, kwargs: dict[str, Any]) -> None:
        self._wal_path.parent.mkdir(parents=True, exist_ok=True)
        seq = len(self._read_records()) + 1
        record = {"seq": seq, "op": op, "context_id": context_id, "kwargs": kwargs}
        with open(self._wal_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def _read_records(self) -> list[dict]:
        if not self._wal_path.exists():
            return []
        return [
            json.loads(line)
            for line in self._wal_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/run/test_failover_memory.py -v`
Expected: PASS (all, including Task 2's).

- [ ] **Step 5: Commit**

```bash
git add src/kagura_engineer/run/failover_memory.py tests/run/test_failover_memory.py
git commit -m "feat(run): buffer critical cloud writes to WAL on failure"
```

---

## Task 4: `drain()` replays the WAL to the cloud

**Files:**
- Modify: `src/kagura_engineer/run/failover_memory.py`
- Test: `tests/run/test_failover_memory.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/run/test_failover_memory.py`:

```python
def test_drain_replays_in_order_and_empties_wal(tmp_path):
    inner = _FakeInner(); inner.fail_writes = True
    c = FailoverMemoryClient(inner, tmp_path / "wal.jsonl")
    c.remember("ctx", summary="s1", content="x", type="savepoint", tags=None)
    c.set_state("ctx", "run:1", {"done": True})
    assert len(_wal_records(tmp_path / "wal.jsonl")) == 2

    inner.fail_writes = False                          # cloud recovers
    replayed = c.drain()
    assert replayed == 2
    assert _wal_records(tmp_path / "wal.jsonl") == []   # WAL emptied
    assert ("remember", "s1") in inner.calls
    assert ("set_state", "run:1") in inner.calls
    # order preserved: remember before set_state
    assert inner.calls.index(("remember", "s1")) < inner.calls.index(("set_state", "run:1"))


def test_drain_partial_failure_keeps_remaining(tmp_path):
    inner = _FakeInner(); inner.fail_writes = True
    c = FailoverMemoryClient(inner, tmp_path / "wal.jsonl")
    c.remember("ctx", summary="s1", content="x", type="savepoint", tags=None)
    c.remember("ctx", summary="s2", content="x", type="savepoint", tags=None)

    # inner accepts the first replay then fails on the second
    calls = {"n": 0}
    def flaky_remember(context_id, *, summary, content, type, tags=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("cloud down again")
        inner.calls.append(("remember", summary))
    inner.remember = flaky_remember

    replayed = c.drain()
    assert replayed == 1
    recs = _wal_records(tmp_path / "wal.jsonl")
    assert len(recs) == 1                               # the second record retained
    assert recs[0]["kwargs"]["summary"] == "s2"


def test_drain_no_wal_is_zero(tmp_path):
    c = FailoverMemoryClient(_FakeInner(), tmp_path / "wal.jsonl")
    assert c.drain() == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/run/test_failover_memory.py -k drain -v`
Expected: FAIL (`FailoverMemoryClient` has no `drain`).

- [ ] **Step 3: Write minimal implementation**

In `src/kagura_engineer/run/failover_memory.py`, add to the class (after `_read_records`):

```python
    # --- replay (drain) ------------------------------------------------------
    def drain(self) -> int:
        """Replay buffered WAL records to the inner cloud client in order. Drop
        each record on success; stop at the first failure and keep the rest for
        the next drain. Returns the count replayed."""
        records = self._read_records()
        if not records:
            return 0
        replayed = 0
        remaining: list[dict] = []
        stop = False
        for rec in records:
            if stop:
                remaining.append(rec)
                continue
            try:
                self._replay(rec)
                replayed += 1
            except Exception:  # noqa: BLE001 — cloud still down; keep this + rest
                _log.warning("WAL replay failed at seq %s; %d records retained",
                             rec.get("seq"), len(records) - replayed)
                stop = True
                remaining.append(rec)
        if remaining:
            self._wal_path.write_text(
                "\n".join(json.dumps(r) for r in remaining) + "\n", encoding="utf-8")
        else:
            self._wal_path.unlink(missing_ok=True)
        return replayed

    def _replay(self, rec: dict) -> None:
        op, context_id, kwargs = rec["op"], rec["context_id"], rec["kwargs"]
        if op == "remember":
            self._inner.remember(context_id, **kwargs)
        elif op == "set_state":
            self._inner.set_state(context_id, kwargs["key"], kwargs["value"])
        else:  # unknown op (forward-compat): skip rather than loop forever
            _log.warning("unknown WAL op %r; dropping record", op)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/run/test_failover_memory.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add src/kagura_engineer/run/failover_memory.py tests/run/test_failover_memory.py
git commit -m "feat(run): drain() replays the WAL to cloud in order"
```

---

## Task 5: `resolve_memory_client` wraps the cloud client when failover is on

**Files:**
- Modify: `src/kagura_engineer/run/memory.py:224-233`
- Test: `tests/run/test_failover_memory.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/run/test_failover_memory.py`:

```python
def _cfg(**over):
    from kagura_engineer.config import Config
    base = dict(profile="coding", memory_cloud_url="https://m",
                workspace_id="w", context_id="c")
    base.update(over)
    return Config(**base)


def test_resolve_wraps_cloud_when_failover_on(tmp_path, monkeypatch):
    from kagura_engineer.run import memory as mem_mod
    # Avoid importing the real SDK: stub the cloud client constructor.
    monkeypatch.setattr(mem_mod.KaguraCloudClient, "from_config",
                        classmethod(lambda cls, cfg: _FakeInner()))
    client = mem_mod.resolve_memory_client(_cfg(memory_failover=True))
    assert isinstance(client, FailoverMemoryClient)


def test_resolve_bare_cloud_when_failover_off(tmp_path, monkeypatch):
    from kagura_engineer.run import memory as mem_mod
    fake = _FakeInner()
    monkeypatch.setattr(mem_mod.KaguraCloudClient, "from_config",
                        classmethod(lambda cls, cfg: fake))
    client = mem_mod.resolve_memory_client(_cfg(memory_failover=False))
    assert client is fake                                # not wrapped


def test_resolve_local_unchanged(tmp_path):
    from kagura_engineer.run import memory as mem_mod
    from kagura_engineer.run.local_memory import LocalMemoryClient
    cfg = _cfg(memory_backend="local", local_memory_path=str(tmp_path / "m.db"),
               memory_cloud_url="", workspace_id="", context_id="")
    client = mem_mod.resolve_memory_client(cfg)
    assert isinstance(client, LocalMemoryClient)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/run/test_failover_memory.py -k resolve -v`
Expected: FAIL (`resolve_memory_client` returns the bare cloud client, not `FailoverMemoryClient`).

- [ ] **Step 3: Write minimal implementation**

Replace `resolve_memory_client` in `src/kagura_engineer/run/memory.py` (lines 224-233) with:

```python
def resolve_memory_client(cfg: Config) -> MemoryClient:
    """Pick the memory backend from config: ``local`` → the offline SQLite
    ``LocalMemoryClient`` (no network, no API key); anything else → the Kagura
    Memory Cloud SDK client, wrapped in a ``FailoverMemoryClient`` (unless
    ``memory_failover`` is off) so critical writes survive a Cloud outage. The
    orchestrators call this for their default (non-injected) memory client so the
    backend is one config switch away."""
    if cfg.memory_backend == "local":
        from .local_memory import LocalMemoryClient

        return LocalMemoryClient(cfg.local_memory_path)
    cloud = KaguraCloudClient.from_config(cfg)
    if not cfg.memory_failover:
        return cloud
    from .failover_memory import FailoverMemoryClient, default_wal_path

    return FailoverMemoryClient(cloud, default_wal_path(cfg.context_id))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/run/test_failover_memory.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add src/kagura_engineer/run/memory.py tests/run/test_failover_memory.py
git commit -m "feat(run): wrap cloud client in FailoverMemoryClient via resolve_memory_client"
```

---

## Task 6: `run_idea` drains at start (hasattr-guarded, never fails the run)

**Files:**
- Modify: `src/kagura_engineer/run/__init__.py` (in `run_idea`, just before `# 0. guard`)
- Test: `tests/run/test_orchestrator.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/run/test_orchestrator.py` (the `_FakeMemory` and `_patch_boundaries` helpers already exist in that file):

```python
# --- failover: drain the WAL at run start ----------------------------------

def test_run_drains_failover_client_at_start(monkeypatch):
    _patch_boundaries(monkeypatch, phases={
        "start": PhaseInvocation("start", 0, "", "", "green", None),
        "implement": PhaseInvocation("implement", 0, "", "", "green", None),
        "ship": PhaseInvocation("ship", 0, "", "", "green", "https://x/pull/9"),
    })
    shas = iter(["before", "after"])
    monkeypatch.setattr("kagura_engineer.run.head_rev", lambda wt: next(shas))

    class _DrainMem(_FakeMemory):
        def __init__(self):
            super().__init__(); self.drained = 0
        def drain(self):
            self.drained += 1
            return 0

    mem = _DrainMem()
    report = run_idea(_cfg(), 42, memory=mem, repo_root=Path("/repo"))
    assert report.status is RunStatus.OK
    assert mem.drained == 1                              # drained exactly once at start


def test_run_drain_failure_does_not_fail_run(monkeypatch):
    _patch_boundaries(monkeypatch, phases={
        "start": PhaseInvocation("start", 0, "", "", "green", None),
        "implement": PhaseInvocation("implement", 0, "", "", "green", None),
        "ship": PhaseInvocation("ship", 0, "", "", "green", "https://x/pull/9"),
    })
    shas = iter(["before", "after"])
    monkeypatch.setattr("kagura_engineer.run.head_rev", lambda wt: next(shas))

    class _BoomDrain(_FakeMemory):
        def drain(self):
            raise RuntimeError("drain blew up")

    report = run_idea(_cfg(), 42, memory=_BoomDrain(), repo_root=Path("/repo"))
    assert report.status is RunStatus.OK                 # drain failure is non-fatal
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/run/test_orchestrator.py -k "drain" -v`
Expected: FAIL — `test_run_drains_failover_client_at_start` fails (`mem.drained == 0`, drain never called).

- [ ] **Step 3: Write minimal implementation**

In `src/kagura_engineer/run/__init__.py`, inside `run_idea`, immediately before the `# 0. guard` comment/line, add:

```python
    # Failover: replay any writes buffered during a prior Cloud outage before we
    # start. hasattr-guarded (only FailoverMemoryClient has drain), and fully
    # best-effort — a drain failure must never fail the run; records stay in the
    # WAL for the next attempt.
    drainer = getattr(mem, "drain", None)
    if drainer is not None:
        try:
            drainer()
        except Exception:  # noqa: BLE001 — drain is best-effort
            _log.exception("run failover drain failed (non-fatal)")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/run/test_orchestrator.py -k "drain" -v`
Expected: PASS (both).

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -q`
Expected: PASS (all).

- [ ] **Step 6: Commit**

```bash
git add src/kagura_engineer/run/__init__.py tests/run/test_orchestrator.py
git commit -m "feat(run): drain failover WAL at run start (best-effort)"
```

---

## Task 7: README positioning correction

**Files:**
- Modify: `README.md:29` (Plan 5+ row) and `README.md:123-130` (Cloud-only section)

- [ ] **Step 1: Update the Plan 5+ table row**

Open `README.md`, find line 29 (the `Plan 5+` row). Replace it with a row that
distinguishes shipped-with-approximation from genuinely-planned:

```markdown
| Plan 5+ | failover memory (Cloud-primary + local WAL) — **done**; rich graph/feedback/Sleep work on both backends (local = approximation, Cloud = Hebbian/neural-graph/server-Sleep); memory auto-store — 📋 planned |
```

(Keep the existing column layout of the table — match the surrounding rows' pipe
structure; adjust the status column to the table's format.)

- [ ] **Step 2: Update the Cloud-only prose (lines ~123-130)**

Replace the paragraph that says reinforcement / Sleep consolidation / memory
auto-store / worktree runs "require Memory Cloud" and that the local backend
"covers the basic grounding loop only — the Plan 5+ features stay Cloud-only"
with:

```markdown
**Memory Cloud is the primary store and the moat.** The local SQLite backend is
the offline/dev tier: it implements the same `MemoryClient` interface with
*approximations* of the rich features (tag-overlap `explore` for graph,
importance-bump `feedback`, `decay` for Sleep-adjacent maintenance), while Memory
Cloud provides the full Hebbian reinforcement, neural-graph, and server-side Sleep
consolidation.

When the cloud backend is active, critical writes (savepoint `remember` and
`set_state`) that fail during a Cloud outage are buffered to a local write-ahead
log and **replayed to Cloud on the next run** — so Cloud stays the source of
truth without losing a run's progress to a transient outage.

The one genuinely planned Plan 5+ item is **memory auto-store / failure-mode
learning** (see `docs/superpowers/plans/2026-06-08-memory-auto-store.md`).
```

- [ ] **Step 3: Verify the README still renders (quick scan)**

Run: `grep -n "Plan 5+\|failover\|memory auto-store\|moat" README.md`
Expected: the new lines appear; no leftover "Plan 5+ features stay Cloud-only".

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs(readme): reflect failover memory + correct Plan 5+ positioning"
```

---

## Task 8: Final verification

- [ ] **Step 1: Run the full test suite**

Run: `python -m pytest -q`
Expected: PASS (all; failover suite + orchestrator drain tests + config).

- [ ] **Step 2: Sanity-check the wiring in a REPL**

Run:
```bash
python - <<'PY'
from kagura_engineer.config import Config
from kagura_engineer.run.failover_memory import FailoverMemoryClient, default_wal_path
print("default WAL:", default_wal_path("ctx-abc"))
print("field default:", Config(profile="p", memory_cloud_url="u", workspace_id="w", context_id="c").memory_failover)
PY
```
Expected: prints a `.../.kagura/engineer/wal/ctx-abc.jsonl` path and `True`.

- [ ] **Step 3: Open the PR**

```bash
git push -u origin feat/failover-memory
gh pr create --base main --title "feat(run): failover memory — Cloud-primary write durability with local WAL" --body "Implements docs/superpowers/specs/2026-06-08-failover-memory-design.md. FailoverMemoryClient buffers critical cloud writes (remember savepoint + set_state) to a local WAL on a Cloud outage and replays them at the next run start; reads stay Cloud-primary. Adds memory_failover config (default on). Corrects README Plan 5+ positioning."
```

---

## Self-Review Notes (author)

- **Spec coverage:** config flag (T1), reads passthrough + WAL path (T2), critical-write buffering + best-effort non-buffering (T3), drain replay + partial-failure retention (T4), resolve wrapping + opt-out + local-unchanged (T5), run-start drain + non-fatal (T6), README positioning (T7), verification (T8). All spec sections mapped.
- **Idempotency:** `set_state` replay = last-write-wins (inner just re-applies); `remember` duplicate only in timeout-after-commit (documented in spec; no dedup key — YAGNI). No task needed beyond the docstring already in the spec.
- **Type consistency:** `FailoverMemoryClient(inner, wal_path)`, `default_wal_path(context_id)`, `drain() -> int`, WAL record `{seq, op, context_id, kwargs}` used identically across T2-T6.
- **Reads list** matches the `MemoryClient` Protocol (load_pinned, recall, recall_detailed, explore, get_state) + writes (remember, set_state, feedback, pin, unpin) + close — full interface covered.
