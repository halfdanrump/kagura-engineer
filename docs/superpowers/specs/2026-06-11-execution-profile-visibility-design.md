# Execution-profile visibility (brain / reviewer / memory context) — design

> [!NOTE]
> Historical design record. It may describe pre-implementation state, internal
> plan numbers, or behavior that has since changed. See the
> [current README](../../../README.md) and [document map](../../README.md).

**Issue:** #70. **Date:** 2026-06-11. **Status:** approved, pre-implementation.

## Problem

`run`, `goal`, `review` and `eval` drive headless backends, and the operator
cannot see **which model/provider actually ran** or **which memory context was
actually shared**:

- The brain backend (`claude`/`codex` + optional `brain_endpoint`) is resolved by
  `select_brain()` (`run/brain_select.py`) but surfaced only as a `_log.warning`
  in the codex case. A claude run prints nothing.
- The reviewer model is `cfg.review.models[0]` (`review/__init__.py:84`), passed
  to `kagura-code-reviewer --model`; neither the model nor the Ollama provider
  appears in any output.
- Memory grounding uses `cfg.context_id` (`run/__init__.py:194-200`), but nothing
  shows which workspace/context a run recalled from or persisted to. A real
  incident motivates this: a wildcard config binding once silently routed recall
  to the wrong context (gh-issue-driven config, resolved 2026-06-07).
- `doctor` checks reachability/auth but never echoes the resolved profile, so
  there is no pre-flight way to confirm "this repo will use brain X, reviewer
  model Y, memory context Z".

## Design: one resolver, three outlets

### 1. `ExecutionProfile` SSOT

A new module `profile.py` (top-level, next to `config.py`) exposes:

```python
@dataclass(frozen=True)
class ExecutionProfile:
    brain_backend: str          # "claude" | "codex"
    brain_endpoint: str | None  # None -> "default" in rendering
    brain_mcp: bool             # the BrainCall policy (engineer's, not the lib's)
    reviewer_model: str | None  # cfg.review.models[0] or None ("reviewer default")
    ollama_url: str
    memory_backend: str         # "cloud" | "local"
    workspace_id: str           # "" for local
    context_id: str             # "" for local
    memory_mcp_config: str | None   # cfg.resolve_mcp_config(repo_root)
    memory_failover: bool

def resolve_profile(cfg: Config, env: Mapping[str, str], repo_root: Path) -> ExecutionProfile
```

`resolve_profile` is **pure** (no network, no subprocess). It reuses
`select_brain`'s resolution for backend/endpoint/MCP-policy — either by calling
`select_brain` (cheap; `kagura_brain.select` builds a handle without I/O) or by
factoring the pure parts out; implementation may choose, but the brain fields
MUST come from the same code path `run`/`review --fix` use, never re-derived,
so the display can never diverge from execution. The codex half-configured-pair
`ConfigError` behaviour is preserved.

A `render_lines(profile) -> list[str]` helper produces the human form used by
every text outlet (single formatting SSOT), e.g.:

```
brain: claude (endpoint: default, in-task MCP: on)
reviewer: qwen3-coder:480b @ http://localhost:11434
memory: cloud · workspace=ws_xxx · context=ea753f42 · failover=on · mcp=.mcp.json
```

and `to_dict(profile)` the JSON form.

### 2. Outlet A — `doctor`

- Print the profile block (the same `render_lines`) **above** the check table,
  so doctor answers "what would run" before "is it healthy".
- `doctor --json` gains a top-level `"profile"` object.
- **Live context verification** (the "正しく共有されているか" guarantee): extend
  the existing cloud-only check group with a `memory-context` check that calls
  the memory SDK's `get_context_info(cfg.context_id)` and reports the resolved
  context **name**:
  - OK: `context ea753f42 → "kagura-engineer Development"`
  - FAIL: id does not resolve / belongs to another workspace → hint
    `check config.context_id` (this is exactly the past-incident detector).
  - Backend=local: check is skipped (same mechanism as other cloud-only checks
    in `doctor/registry.py`).
  - Network/auth errors degrade to FAIL with the error string, mirroring
    `check_memory_cloud`'s existing error taxonomy.

### 3. Outlet B — startup header in `run` / `goal` / `review` / `eval`

- Each command, right after `load_config` succeeds and before phase work,
  emits `render_lines(profile)` through its existing progress/echo path
  (`progress = None if json_out else typer.echo` — so `--json` stdout stays a
  single valid JSON document, same rule as issue #12 phase streaming).
- `review` without `--fix` shows the reviewer + memory lines; the brain line is
  shown only when `--fix` is active (no brain runs otherwise — don't imply one).
- `goal` prints the header once up-front (the profile is per-config, not
  per-issue).

### 4. Outlet C — grounding evidence line (run-time proof)

The header proves intent; this proves what actually happened. In
`run/__init__.py` after the recall phase resolves
(`load_pinned` + `recall_detailed`), stream one line:

```
grounding: pinned 2 + recalled 5 from context ea753f42
```

(or `grounding: none (recall disabled)` in the eval control arm / local-empty
case). This is a progress line only — added via the existing `progress` sink,
so `--json` is unaffected.

### 5. JSON reports

`run`/`goal`/`review`/`eval` `--json` reports each gain a `"profile"` field
(the `to_dict` form). Renderers (`*/render.py to_json`) attach it; report
dataclasses carry the profile so the table renderers could also use it later.

## Non-goals (YAGNI)

- Surfacing the *underlying Anthropic model name* a claude run uses — that is
  owned by the operator's Claude Code config/subscription and is not visible to
  the harness pre-flight. The profile reports what the harness controls:
  backend, endpoint, MCP policy.
- A new `profile` subcommand (doctor + headers cover both "check before" and
  "evidence during").
- Echoing secrets: `memory_cloud_url` stays un-echoed in doctor detail strings
  (existing CSO decision, `doctor/checks.py:260`); API keys never appear.
  `workspace_id`/`context_id` are non-secret identifiers (already shown in
  scaffold templates and error messages).

## Error handling

- `resolve_profile` raises only `ConfigError` (the codex half-pair case), which
  every CLI entry already catches and exits 2 — no new failure mode.
- The `memory-context` doctor check never raises: SDK/network errors become a
  FAIL row with a hint, like every other check.
- A failing progress sink stays non-fatal (existing `run/__init__.py:143-145`
  pattern).

## Testing

- `resolve_profile` unit tests: claude default; codex + endpoint; MCP policy
  divergence (`enable_codex_mcp`); local backend zeroes cloud fields; purity
  (no I/O — fake env only).
- `render_lines`/`to_dict` golden tests (the formatting SSOT).
- doctor: profile block precedes table; `--json` carries `"profile"`;
  `memory-context` check OK/FAIL/skip-on-local via a fake SDK client.
- CLI header tests per command: header present on text path, absent on `--json`
  stdout; review-without-fix omits the brain line.
- grounding evidence line: emitted with correct counts; absent under `--json`;
  control-arm shows the disabled form.
- No real network/CLI in CI (fake clients), matching the existing test strategy.
