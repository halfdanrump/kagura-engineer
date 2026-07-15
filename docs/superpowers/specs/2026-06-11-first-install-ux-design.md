# First-install UX: doctor/setup without repo.yaml — design

> [!NOTE]
> Historical design record. It may describe pre-implementation state, internal
> plan numbers, or behavior that has since changed. See the
> [current README](../../../README.md) and [document map](../../README.md).

**Issue:** #71. **Date:** 2026-06-11. **Status:** approved, pre-implementation.

## Problem

A fresh `pip install kagura-engineer` followed by `kagura-engineer doctor` or
`kagura-engineer setup` exits 2 immediately:

```
doctor: invalid config 'repo.yaml': config not found: repo.yaml
setup: invalid config 'repo.yaml': config not found: repo.yaml
```

0.3.0 added `init` and the error now hints at it, but the chicken-and-egg
remains: **the two commands whose whole job is "get me to a healthy state"
refuse to run until the state is already half-built** (init → edit creds →
setup, three manual steps). Reported from a real first install on Windows
(0.2.x, where even the hint was absent).

## Design

Both commands learn to operate on a missing/incomplete config instead of
refusing. The user-visible contract: **`kagura-engineer setup` is the only
command a fresh checkout needs to type first.**

### Shared seam: lenient config loading

`config.py` gains:

```python
@dataclass(frozen=True)
class ConfigLoad:
    cfg: Config | None      # None when missing/unparseable/invalid
    error: str | None       # the ConfigError message when cfg is None
    missing: bool           # file-not-found specifically (drives auto-scaffold)

def load_config_lenient(path: str | Path) -> ConfigLoad
```

It wraps `load_config` and never raises. `load_config` itself is unchanged —
`run`/`goal`/`review`/`eval` keep the current hard requirement (they cannot do
anything useful without a valid config; their early `ConfigError` exit-2 is
correct and stays).

### `setup`: auto-scaffold + degraded plan

1. When the config file is **missing**, run the existing `scaffold()` helper
   (same code path as `init`, already idempotent) before anything else, and
   say so: `repo.yaml not found — scaffolding one (same as 'kagura-engineer init')`.
2. Re-load leniently. If the config is now (or was) **present but invalid**
   (typical: the fresh template's blank cloud creds), do not abort. Instead:
   - Prepend a synthetic **`config` step** to the report with status
     `NEEDS_USER`, whose detail carries the validation error and the creds
     hint (reusing `CLOUD_REQUIRED_FIELDS` / the `init` next-step wording —
     one wording SSOT, no drift).
   - Run the **config-independent steps** (git, claude, gh — those whose
     builders don't read cloud fields; see `setup/__init__.py:70-88`) normally.
   - Mark config-dependent steps (ollama needs `ollama_url`+`review.models`,
     memory-cloud auth, memory-mcp) as `SKIPPED` with reason
     `waiting on config`.
3. With a valid config, behaviour is unchanged byte-for-byte.
4. Exit codes keep the existing policy (`1` if any FAIL, else `2` if any
   NEEDS_USER): a fresh install lands on exit 2 with a table that *names the
   exact next action* instead of a one-line refusal.

Note `--dry-run`/`--fix`/`--no-input` compose: auto-scaffold is suppressed
under `--dry-run` (preview must not write); `--fix <config-dependent-step>`
with an invalid config reports that step `SKIPPED (waiting on config)`.

### `doctor`: degraded report instead of refusal

1. Load leniently. With a valid config: unchanged.
2. With a missing/invalid config: print the table anyway —
   - a synthetic **`config` FAIL row** first, detail = the ConfigError message,
     hint = `run 'kagura-engineer setup'` (missing) or the validation error
     (invalid);
   - then every **config-free check** (git repo, brain CLI presence, gh) runs
     normally. Cloud-only checks are omitted exactly as they already are for
     `memory_backend=local` (`doctor/registry.py:51-53`); checks needing other
     config values (ollama url) are omitted too.
3. Exit code: `1` (FAIL present) — replacing today's exit 2. Doctor's contract
   "non-zero when unhealthy" is preserved; scripts keying on the specific
   code 2 for config errors are not a documented contract.
4. `doctor --json`: the synthetic row appears as a normal check object, so the
   schema is unchanged.

### Registry/plan changes

The check/step registries gain a per-entry `needs_config` flag (or an explicit
split list, whichever reads cleaner in the existing registry style) so
"config-free subset" is declared data, not an if-ladder in the CLI. This is the
only structural change; check/step bodies are untouched.

## Non-goals (YAGNI)

- Interactive credential prompting in `setup` (creds entry stays a file edit;
  `--no-input` semantics unchanged).
- Auto-detecting workspace/context ids from the Memory Cloud account.
- Changing `init` (it remains the explicit/scriptable form; `setup` now merely
  subsumes it for the lazy path).
- Relaxing `run`/`goal`/`review`/`eval` (a hard, early ConfigError is the right
  behaviour mid-loop).

## Error handling

- `scaffold()` failures (unwritable dir) surface as a `config` FAIL row in the
  setup report, not a traceback.
- `load_config_lenient` never raises; every other ConfigError path is
  unchanged.

## Testing

- `load_config_lenient`: missing / unreadable / invalid-YAML / validation-fail
  / valid — field-by-field assertions on `ConfigLoad`.
- setup: missing config → scaffold ran + `config` NEEDS_USER + git/claude/gh
  executed + cloud steps SKIPPED; invalid-creds config → same minus scaffold;
  valid config → report identical to today (regression pin); `--dry-run` does
  not write; exit codes 2/1/0 matrix.
- doctor: missing config → `config` FAIL row + config-free checks ran + exit 1;
  `--json` schema unchanged; valid config → identical to today (regression pin).
- Windows-path smoke: none needed beyond existing platform handling (paths go
  through `Path` throughout).
