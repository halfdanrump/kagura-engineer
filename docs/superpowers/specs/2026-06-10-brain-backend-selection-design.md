# Brain backend selection (codex / Ollama Cloud) — design

> [!NOTE]
> Historical design record. It may describe pre-implementation state, internal
> plan numbers, or behavior that has since changed. See the
> [current README](../../../README.md) and [document map](../../README.md).

**Issue:** #51 (deferred from #48). **Date:** 2026-06-10. **Status:** approved, pre-implementation.

## Problem

`kagura-engineer` drives a headless LLM ("the brain") through `kagura_brain`, but
wires only **one** backend: it imports `from kagura_brain import claude as brain`
and calls `brain.invoke(...)` at two sites — `run/workflow.py:313` (the run loop's
phases) and `review/fixer.py:75` (the review-fix loop). `kagura-brain` 0.2.0 also
ships a **codex** adapter (with an Ollama Cloud / BYO-endpoint preset) and a
provider-neutral **doctor**, but engineer cannot select them.

This spec adds **backend selection** so a run can target `claude` (default,
unchanged) or `codex` (incl. Ollama Cloud), while preserving engineer's
memory-grounding guarantees.

## Key facts that shape the design

1. **Adapter asymmetry (load-bearing).** `claude.invoke(..., mcp_config=, allowed_tools=)`
   supports MCP memory tools; `codex.invoke(..., endpoint=, api_key=, local_provider=)`
   does **not** take MCP params today. So a codex run has no *in-task* MCP recall.
2. **Grounding is layered, and the baseline is backend-agnostic.** Engineer already
   does **out-of-band** recall via the Python SDK (`run/__init__.py:169
   mem.recall_detailed(...)`, savepoint at `:296 mem.remember(...)`) and injects the
   result into the prompt as a string. This works for **any** backend, codex
   included. The *in-task* MCP layer is an enhancement on top, not the only path.
3. **Two unrelated "proxies".** `kagura-mcp` (the kagura-memory SDK's stdio MCP
   proxy, OAuth-refreshing) supplies the **memory tools** and is independent of the
   model endpoint. An **Anthropic/OpenAI LLM gateway** (LiteLLM / ollama-code) routes
   **model traffic** — it is NOT in the memory SDK and engineer does not build one.
4. **Protocol split for "ollama via claude".** `claude.invoke(endpoint=)` injects
   `ANTHROPIC_BASE_URL`, so its endpoint must be **Anthropic-compatible**. Ollama
   Cloud (`https://ollama.com/v1`) is **OpenAI-compatible** → reachable by codex
   directly, or by claude only through an Anthropic-compatible gateway (out of scope).
5. **Codex CAN do MCP natively** (`codex mcp add`, `~/.codex/config.toml
   [mcp_servers]`, `codex 0.133.0`), but the kagura-brain codex adapter does not wire
   it yet. Closing the asymmetry is a **bounded kagura-brain task**, filed separately.

## Scope

1. **Dependency bump** `kagura-brain>=0.2.0,<0.3` (currently pinned `<0.2`, which
   *excludes* 0.2.0). This is a prerequisite — and a security one: engineer on
   `<0.2` also lacks brain #11's `CLAUDE_*` scrub. The #50 publish gate text is
   corrected from "≥0.1.1" to "≥0.2.0".
2. **Config**: add `brain_backend` and `brain_endpoint` (below). API key resolves
   from an **env var**, never `repo.yaml`.
3. **Adapter factory**: a single dispatch point that maps `Config` → the right
   `kagura_brain` adapter + per-backend kwargs, confining the MCP asymmetry to one
   place and staying forward-compatible with a future codex-MCP wiring.
4. **doctor guard**: the run/loop guard checks the **selected** backend's CLI
   (`claude.check()` vs `codex.check()`) via kagura-brain doctor, instead of always
   checking `claude`.
5. **(Deferred, separate brain issue)**: wire MCP into the codex adapter so a codex
   run regains *in-task* grounding. Engineer's factory is shaped so this needs no
   engineer rework.

### Non-goals (YAGNI)

- `local_provider` / codex `--oss` local mode. Coding runs need capable models;
  Ollama Cloud covers that. A local daemon is still reachable later via
  `brain_endpoint=http://localhost:11434/v1` without a schema change.
- A claude→Ollama Anthropic-compatible gateway (that's an ops/proxy concern).
- Per-phase or per-issue backend switching. One backend per run.

## Config schema

```yaml
# repo.yaml — non-secret only
brain_backend: claude          # Literal["claude","codex"], default "claude"
brain_endpoint: ollama-cloud   # optional str. claude → Anthropic-compatible URL;
                               # codex → "ollama-cloud" alias or an OpenAI-compatible URL
# brain_api_key is NOT written here — resolved from env (KAGURA_BRAIN_API_KEY)
```

- Defaults (`brain_backend="claude"`, `brain_endpoint=None`) reproduce **today's
  behaviour byte-for-byte** — existing repos are unaffected.
- `Config` keeps `extra="forbid"`; both fields get validated. A model-validator
  rejects invalid combinations (e.g. `brain_endpoint` set with no api_key in env when
  the backend requires one) with a clear `ConfigError`, mirroring the existing
  cloud-creds validator.
- Secret discipline matches memory-cloud auth: **URL/alias in config, secret in env.**

## Architecture

A new module `run/brain_select.py` exposes one function:

```python
def select_brain(cfg: Config, env: Mapping[str, str]) -> BrainCall
```

where `BrainCall` is a small frozen dataclass holding the chosen adapter's `invoke`
plus the resolved per-backend kwargs:

- **claude** → `kagura_brain.claude.invoke`, kwargs include `mcp_config`,
  `allowed_tools=MEMORY_TOOLS`, and `endpoint`/`api_key` when `brain_endpoint` is set.
- **codex** → `kagura_brain.codex.invoke`, kwargs include `endpoint` (alias-resolved)
  and `api_key` from env; **no** `mcp_config`/`allowed_tools` (asymmetry confined
  here). When a codex run drops MCP tools, `select_brain` emits a single `_log`
  warning so the degraded-grounding tradeoff is visible, not silent.

Both call sites change from `brain.invoke(prompt, ..., mcp_config=, allowed_tools=)`
to `call = select_brain(cfg, env); call.invoke(prompt, cwd=, timeout=, **call.kwargs)`.
The two sites stay nearly identical; the asymmetry lives only in `select_brain`.

`doctor` guard: replace the hard-coded claude check with the selected backend's
`check()` (claude/codex) so the guard fails fast when the chosen CLI is absent.

### Data flow

```
Config (repo.yaml) + env  →  select_brain()  →  BrainCall{invoke, kwargs}
                                                   │
run/workflow.py phase  ────────────────────────────┤→  kagura_brain.<adapter>.invoke
review/fixer.py loop   ────────────────────────────┘     (claude: +MCP / codex: -MCP)
out-of-band recall (Python SDK)  ── unchanged, backend-agnostic baseline grounding ──
```

## Error handling

- Unknown `brain_backend` / malformed `brain_endpoint`: `ConfigError` at load time
  (fail fast, before any run side-effect).
- Missing required api_key in env for an endpoint that needs one: `ConfigError`
  with a fix hint (`export KAGURA_BRAIN_API_KEY=…`), mirroring `_MEMORY_AUTH_HINT`.
- Selected CLI not launchable: surfaced by the doctor guard (existing blocking-check
  pattern), not by an opaque `invoke` OSError.
- `select_brain` itself is pure (Config+env → BrainCall); it never shells out.

## Testing

- `select_brain` unit tests: claude default (kwargs include MCP tools + endpoint
  passthrough); codex (endpoint alias resolved, api_key from env, **no** MCP kwargs);
  the degraded-grounding warning fires for codex; `ConfigError` on bad combos.
- Config tests: new fields parse; defaults reproduce current behaviour; `extra=forbid`
  still rejects typos; validator rejects secret-in-config and missing-env-key.
- Call-site tests: `run/workflow.py` and `review/fixer.py` route through
  `select_brain` and forward the right kwargs (fake adapter asserts received kwargs —
  note the **kwargs-swallow** convention: assert the codex fake does NOT receive
  `mcp_config`/`allowed_tools`).
- doctor-guard test: guard checks the selected backend's CLI, not always claude.
- No real CLI / network in CI (mock adapters and `check()`), mirroring brain's and
  engineer's existing test strategy.

## Sequencing

Per the issue, this ships **after** v0.4.0 (Trust before integration: publish and
earn trust in the claude core first). Designing now is fine; implementation lands in
v0.5.0+. The dependency bump (scope #1) is the one piece also needed *before* the
v0.4.0 publish for the security reason above, so it may land independently/earlier.
