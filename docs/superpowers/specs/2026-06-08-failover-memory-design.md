# Failover Memory — Cloud-primary write durability with a local WAL

> [!NOTE]
> Historical design record. It may describe pre-implementation state, internal
> plan numbers, or behavior that has since changed. See the
> [current README](../../../README.md) and [document map](../../README.md).

**Date:** 2026-06-08
**Status:** Design approved (brainstorming) — pending spec review → writing-plans
**Context:** kagura-engineer `run`/`goal` memory layer

## Problem

Memory Cloud is the primary store and the product moat. The `run`/`goal`
orchestrator grounds on it (`recall`) and writes back to it (`remember` savepoint,
`set_state` done/halt markers, best-effort `feedback`). If Cloud blips **during a
multi-minute autonomous run** — after a successful `recall` but before the final
`persist` — those critical writes are lost: the savepoint and the resume/done
marker never reach Cloud, so the run's progress is not durably recorded.

Today `resolve_memory_client(cfg)` picks exactly one backend (`local` **or**
`cloud`) and there is no failover or buffering between them.

## Goal / non-goals

**Goal:** Cloud-障害 write durability. When a *critical* Cloud write fails, buffer
it to a durable local WAL and replay it to Cloud on the next run. Cloud stays the
single source of truth and the primary read path.

**Non-goals (YAGNI):**
- No offline reads / local read-mirror. Reads stay Cloud-primary; a `recall`
  failure remains a hard FAIL ("we do not run ungrounded"). This is deliberately
  *not* full offline operation.
- No local-first / latency optimization (Cloud is primary, not a write-behind cache).
- No buffering of best-effort writes (`feedback`, `pin`/`unpin`) — losing those on
  an outage is acceptable, matching the existing best-effort side-effect policy.

## Decisions (from brainstorming)

1. **Purpose:** write durability during Cloud outages (Cloud = primary/moat).
2. **Buffer scope:** critical writes only — `remember` (savepoint) and `set_state`.
3. **Replay trigger:** automatic drain at the **start of the next `run`/`goal`**
   (before `recall`), best-effort. No new user command required.
4. **Implementation shape:** a wrapping `FailoverMemoryClient` (bounded-composable),
   not changes inside the cloud adapter.

## Architecture

New module `src/kagura_engineer/run/failover_memory.py`:

```
class FailoverMemoryClient(MemoryClient):
    def __init__(self, inner: MemoryClient, wal_path: Path): ...
```

- `inner` is the real `KaguraCloudClient`.
- `wal_path` is a durable append-only JSONL file, keyed by `context_id`:
  `~/.kagura/engineer/wal/<context_id>.jsonl` (under the existing `~/.kagura`
  convention used by `credentials.json`; directory created lazily).
- `resolve_memory_client(cfg)` wraps the cloud client in `FailoverMemoryClient`
  when `cfg.memory_backend == "cloud"` **and** `cfg.memory_failover` is true.
  `local` backend is unchanged.

### Config

Add `memory_failover: bool = True` to `Config` (default on for the cloud backend;
allows opt-out and keeps tests explicit). Local-backend repos ignore it.

## Component behavior (the `MemoryClient` interface)

| Method | Behavior |
|---|---|
| `load_pinned`, `recall`, `recall_detailed`, `explore`, `get_state` | **Reads** — delegate to `inner`; exceptions **propagate** (preserves recall hard-FAIL + Cloud-primary). No local fallback. |
| `remember` | **Critical write** — try `inner.remember(...)`; on exception, append a WAL record and return a synthetic id `wal:<uuid>` (the caller — run persist — uses the return only for the savepoint log, not critically). |
| `set_state` | **Critical write** — try `inner.set_state(...)`; on exception, append a WAL record and return. |
| `feedback`, `pin`, `unpin` | **Best-effort** — delegate to `inner`; not buffered. Exceptions propagate to the caller's existing try/except (run already wraps `feedback`). |
| `close` | delegate to `inner.close()`. |
| `drain()` | **New method** — replay buffered WAL records to `inner` in `seq` order; drop each on success; stop on first failure and keep the rest for next time. |

### WAL record format (JSONL, one per line)

```json
{"seq": 1, "op": "remember", "context_id": "…", "kwargs": {"summary": "…", "content": "…", "type": "savepoint", "tags": ["…"]}}
{"seq": 2, "op": "set_state", "context_id": "…", "kwargs": {"key": "run:16", "value": {"done": true, "pr_url": null}}}
```

Append is `flush()`+`os.fsync()` so a crash mid-outage does not lose a buffered write.

## Data flow

1. `run_idea` start → drain before the guard/recall phases, called via a
   `hasattr(mem, "drain")` guard (mirrors the existing `hasattr(mem, "close")`
   pattern) so only `FailoverMemoryClient` participates — `drain()` is not forced
   onto the `MemoryClient` Protocol, and `local`/bare-cloud clients are untouched.
   The call is wrapped so a drain failure never fails the run (remaining records
   stay in the WAL). `drain()` is a cheap no-op when the WAL is empty, so `goal`
   needs no separate drain: each per-issue `run_idea` drains, which also gives
   **intra-goal recovery** — if issue N's persist buffers during an outage, issue
   N+1's start drains and replays it.
2. Normal operation: `inner` writes succeed → WAL stays empty.
3. Outage after `recall`: `remember`/`set_state` raise inside `inner` → record
   appended to WAL → run finishes cleanly (persist is already best-effort).
4. Next run: `drain()` replays the WAL to Cloud → write lands in the moat.

```
run start ─► drain(WAL→Cloud, best-effort) ─► guard ─► recall(Cloud, hard-FAIL)
        ─► start/implement/ship ─► persist: remember/set_state
                                       │ Cloud ok ──► done
                                       └ Cloud down ─► append WAL ─► (next run drains)
```

## Error handling & idempotency

- `drain()` is best-effort and fully guarded — it never raises into the run.
- WAL writes are fsync-durable.
- Reads never fall back to local (Cloud-primary invariant preserved).
- **Idempotency:** records are only buffered when the Cloud call **confirmed-failed**
  (raised), so the write did not happen and replay is safe.
  - `set_state` is last-write-wins → replay is naturally idempotent.
  - `remember(savepoint)` could duplicate only in the rare timeout-after-commit
    case (server committed, client saw a timeout). A duplicate savepoint is
    low-harm; documented on the method. (No dedup key in v1 — YAGNI.)

## Testing

Unit tests with a fake `inner` whose writes can be toggled to raise:

- write success → WAL stays empty; return value is the inner's id.
- `remember`/`set_state` failure → WAL gains exactly one record; the call returns
  without raising (`remember` returns a `wal:` id).
- `feedback` failure is **not** buffered (propagates / not in WAL).
- `drain()` replays records to `inner` in `seq` order; WAL emptied on full success.
- `drain()` partial failure (inner raises on record N) → records < N dropped,
  ≥ N retained.
- reads delegate; a read failure **propagates** (recall hard-FAIL preserved).
- `set_state` replay is idempotent (last value wins).
- `resolve_memory_client`: cloud backend → returns `FailoverMemoryClient`;
  `memory_failover=False` → returns the bare cloud client; local unchanged.
- orchestrator: `run_idea`/`run_milestone` call `drain()` once at start (monkeypatched),
  and a drain exception does not fail the run.

WAL durability uses a real temp file (`tmp_path`); the inner is faked, so no
network. Consistent with the offline test suite.

## README / positioning (follow-up, same PR or adjacent)

Correct the Plan 5+ row and Cloud section to reflect reality:
- **Memory Cloud is primary and the moat.** The local backend is the offline/dev
  tier; cloud writes are **failover-buffered locally and synced back** on the next
  run (resilience), not a separate feature tier.
- Stop labeling feedback/graph(`explore`)/Sleep(`decay`) as "Cloud-only / planned":
  they exist on the local backend as **approximations** (cloud has the rich
  Hebbian/neural-graph/server-Sleep versions).
- The only genuinely *planned* Plan 5+ item is **memory auto-store / failure-mode
  learning** (`docs/superpowers/plans/2026-06-08-memory-auto-store.md`, not yet
  implemented).

## Out of scope for this spec

- `memory auto-store / failure-mode learning` (separate plan, deferred).
- A manual `kagura-engineer memory sync` command (could be added later; auto-drain
  at run start covers the moat requirement).
- Bidirectional sync / offline reads / local read-mirror.
