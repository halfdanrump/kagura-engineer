# kagura-engineer

> Part of the Kagura Memory Cloud offering. Licensed under
> [Apache-2.0](LICENSE) — © 2026 Kagura AI.

An autonomous, memory-grounded coding harness that drives a GitHub issue to a
pull request and reviews pull requests. The actor runs through `kagura-brain`
using Claude Code (the default) or Codex; grounding comes from Kagura Memory
Cloud or an offline SQLite store.

The current CLI provides `init`, `doctor`, `setup`, `run`, `review`, and `eval`.
This is a `0.x` project, so minor releases may include breaking changes; see
[CHANGELOG.md](CHANGELOG.md) before upgrading.

---

## Install

Requires **Python ≥ 3.11**.

### As a tool (recommended)

Published on [PyPI](https://pypi.org/project/kagura-engineer/):

```bash
# uv can also fetch a suitable Python runtime
uv tool install kagura-engineer

# or pipx
pipx install kagura-engineer

# or pip
pip install kagura-engineer
```

The standalone `review` command invokes
[`kagura-code-reviewer`](https://github.com/kagura-ai/kagura-code-reviewer).
Install both tools with the `review` extra:

```bash
uv tool install "kagura-engineer[review]"
```

Without the reviewer, `review` returns a clean FAIL gate.

To install an unreleased commit directly from this public repository:

```bash
uv tool install git+https://github.com/kagura-ai/kagura-engineer.git
```

Pin a release when reproducibility matters, for example
`pip install "kagura-engineer==X.Y.Z"`.

### For development

```bash
git clone https://github.com/kagura-ai/kagura-engineer.git
cd kagura-engineer
pip install -e ".[dev]"
```

### As a Claude Code plugin

The repository also ships a thin Claude Code skill-plugin wrapper
(`.claude-plugin/` and `skills/`). Its skills shell out to the installed CLI;
the harness logic remains in `src/kagura_engineer`.

Install the CLI first, then add this repository as a marketplace source. The
plugin is also referenced by the public
[`kagura-plugins`](https://github.com/kagura-ai/kagura-plugins) marketplace.

---

## Configuration

Run `kagura-engineer init` to create a git-ignored, per-checkout `repo.yaml`.
`setup` does the same automatically when the file is missing. Operational
commands accept `--config` / `-c` to use another path.

A cloud-backed configuration can use the following fields:

```yaml
profile: dev                                      # free-form execution-profile label

brain_backend: claude                             # claude | codex (default: claude)
# brain_endpoint: https://gateway.example         # optional URL or supported alias
# enable_codex_mcp: false                         # opt in to Codex in-task MCP wiring

memory_backend: cloud                             # cloud | local (default: cloud)
memory_cloud_url: https://memory.kagura-ai.com
workspace_id: ws_xxxxxxxx
context_id: 00000000-0000-0000-0000-000000000000
memory_failover: true                             # buffer failed critical writes locally
# memory_mcp_config: path/to/.mcp.json            # optional; <repo>/.mcp.json is auto-discovered

local_memory_path: .kagura/memory.db              # used only with memory_backend: local
ollama_url: http://localhost:11434

review:
  models: [qwen2.5-coder:7b]                      # Ollama reviewer models
  max_loops: 3
  code_review: auto                               # auto | always | never
  effort: medium                                  # low | medium | high
```

Unknown keys are rejected. `run`, `review`, and `eval` require a valid config
and return exit code `2` for a configuration error. `doctor` and `setup` are
lenient so they can diagnose or bootstrap a fresh checkout.

### Memory

For `memory_backend: cloud`, `memory_cloud_url`, `workspace_id`, and
`context_id` are required. Authentication is resolved in this order:

1. `KAGURA_API_KEY`, suitable for CI and other non-interactive environments.
2. The OAuth profile written by `kagura auth login`.

For `memory_backend: local`, the Cloud fields and credentials may be omitted.
The local backend uses SQLite and keyword-overlap recall; it does not provide
the Cloud service's full graph and consolidation behavior.

With Cloud failover enabled, critical writes that fail during an outage are
buffered in a local write-ahead log and replayed on a later run. Cloud remains
the source of truth.

For the Cloud backend, `setup` generates `<repo>/.mcp.json` for in-task memory
access. Claude receives that MCP configuration automatically. Codex keeps
in-task MCP disabled by default; set `enable_codex_mcp: true` to opt in. Codex
cannot use the same per-call memory-tool allow-list as Claude, so its own
approval and sandbox policy remains the confinement boundary.

### Brain authentication

- With `brain_backend: claude` and no custom endpoint, authenticate the Claude
  Code CLI normally (`claude` / `claude login`).
- With `brain_backend: codex` and no custom endpoint, install and authenticate
  the Codex CLI normally.
- A custom `brain_endpoint` must be paired with
  `KAGURA_BRAIN_API_KEY`. Configure both or neither; the key is never stored in
  `repo.yaml`.

The standalone reviewer uses its configured Ollama models. A brain subprocess
is needed by `review` only when `--fix` is enabled.

### Headless permissions (`run` / `doctor --exec-probe`)

A headless `claude -p` has **no human to answer Claude Code's permission
prompts** — a tool call that would normally pop an "allow?" dialog just hangs
and gets reported as blocked, and the run red-halts. Every capability a run
needs must therefore be granted *up front*, in two places:

1. **The repo's `.claude/settings.json` allowlist** — commit one with the
   commands and tools a run actually uses. `Edit`/`Write` are **separate
   permissions from the Bash patterns**: with only a Bash allowlist, `start`
   passes (it only runs commands) and `implement` red-halts (it has to write
   code). A working baseline:

   ```json
   {
     "permissions": {
       "allow": [
         "Edit", "Write", "NotebookEdit",
         "Bash(git status *)", "Bash(git diff *)", "Bash(git log *)",
         "Bash(git add *)", "Bash(git commit *)", "Bash(git checkout *)",
         "Bash(git push *)", "Bash(git fetch *)",
         "Bash(gh auth status)", "Bash(gh issue view *)",
         "Bash(gh pr create *)", "Bash(gh pr view *)", "Bash(gh api *)",
         "Bash(pytest *)", "Bash(uv run *)"
       ]
     }
   }
   ```

2. **Workspace trust** — the allowlist is honoured only for directories the
   *human* has trusted in Claude Code (`hasTrustDialogAccepted` in
   `~/.claude.json`). Trust for the repo and trust for the `.kagura-runs/<repo>/`
   worktree area are **separate** — and since each run gets a fresh
   `run-<issue>` worktree path, trust must cover the worktree *parent*, not
   one specific run dir. Open `claude` once in the repo **and** once under
   `.kagura-runs/<repo>/` and accept the trust dialog. This step is
   deliberately human-only: an agent must not be able to widen its own
   permissions.

`doctor --exec-probe` verifies both end-to-end, in the context `run` actually
executes in: it creates an ephemeral git worktree under `.kagura-runs/<repo>/`
(so the committed allowlist and the worktree-area trust are both exercised),
launches the resolved headless brain there, asks it to run one
approval-requiring command from the baseline (`gh auth status` — read-only git
is approval-free and would prove nothing) and write one uniquely-named temp
file, verifies the write **on disk** rather than trusting the model's
self-report, then removes the worktree. It reports exactly which capability is
blocked — before a real run burns a dispatch discovering it. With
`brain_backend: codex` the probe is skipped (Codex uses its own
sandbox/approval model, not Claude Code permissions).

---

## Commands

### `kagura-engineer init`

Creates a commented `repo.yaml` and adds it to `.gitignore`. It is idempotent
and never overwrites an existing config.

```bash
kagura-engineer init
kagura-engineer init --dir path/to/repo
```

### `kagura-engineer doctor`

Checks the dependency chain and prints a table or JSON report. Each check is
isolated, so one failure does not abort the remaining checks.

| Check | Verifies |
|---|---|
| `git` | Git is available and the current directory is a work tree |
| selected brain CLI | `claude` or `codex`, according to `brain_backend` |
| `gh` | GitHub CLI is installed and authenticated |
| `ollama` | The daemon is reachable and configured reviewer models are present |
| `haiku` | An Anthropic auth source resolves for the Haiku-dependent lane |
| `memory-cloud` / `memory-local` | The selected memory backend is usable |
| `memory-mcp` / `memory-context` | Cloud MCP config and live context binding are valid |
| `gh-issue-driven` | The workflow plugin used by `run` is installed |
| `headless-exec` (opt-in) | `doctor --exec-probe` verifies commands and file edits in an ephemeral run worktree; see [headless permissions](#headless-permissions-run--doctor---exec-probe) |

```bash
kagura-engineer doctor
kagura-engineer doctor --json
kagura-engineer doctor -c path/to/repo.yaml
kagura-engineer doctor --exec-probe   # + live headless-permissions probe (spends tokens, ~30 s+)
```

Exit codes: `0` for OK/WARN only; `1` when any check fails. A missing or invalid
config appears as a synthetic `config` FAIL row rather than an early exit.

### `kagura-engineer setup`

Installs missing dependencies and bootstraps authentication. The operation is
idempotent and safe to re-run.

The canonical step order is:

```text
git → claude-code → gh → ollama → ollama-models → memory-cloud → memory-mcp
```

```bash
kagura-engineer setup                  # full run
kagura-engineer setup --dry-run        # preview without side effects
kagura-engineer setup --fix gh         # run one step
kagura-engineer setup --no-input       # never prompt
kagura-engineer setup --full           # also install memory hooks and skills
kagura-engineer setup --json
```

Valid `--fix` targets are `git`, `claude-code`, `gh`, `ollama`,
`ollama-models`, `memory-cloud`, and `memory-mcp`. Exit codes: `0` for
OK/SKIPPED; `1` when any step fails; `2` when user action is required or the
config/target is invalid.

When Codex is selected, install and authenticate its CLI separately; the
current setup plan still provisions the Claude Code step used by the default
backend.

### `kagura-engineer run`

Drives one GitHub issue through guard, memory recall, isolated worktree,
`start → implement → ship`, gate, and persistence phases. A successful run
opens or recovers a pull request and saves a resumable checkpoint.

```bash
kagura-engineer run 42
kagura-engineer run 42 --no-remember
kagura-engineer run 42 --unattended
kagura-engineer run 42 --json
```

Exit codes: `0` when a PR is reached; `1` for a hard failure; `2` when a guard
or gate blocks the run. Re-run the same issue to resume from persisted state.

The report records the execution profile, grounding evidence, and the actual
in-phase code-review provider/model when a review ran. `review.code_review`
controls that inner review: `auto` lets the actor decide from diff risk,
`always` forces it, and `never` disables it. `review.effort` supplies the
review effort hint.

### `kagura-engineer review`

Invokes `kagura-code-reviewer` on a branch or PR, consumes its JSON envelope,
stores the full report in `.kagura/review.json`, and gates on the verdict.
Recalled memory is passed as untrusted, reference-only context.

```bash
kagura-engineer review                 # HEAD against main
kagura-engineer review feat/x
kagura-engineer review 42              # PR #42
kagura-engineer review --base develop
kagura-engineer review --json
kagura-engineer review --fix
```

Exit codes: `0` for green/yellow or nothing to review; `1` when review or a fix
cannot run; `2` for blocking red findings.

With `--fix`, the selected brain fixes blocking findings, commits them, and
re-runs the reviewer up to `review.max_loops` times. The loop modifies the
currently checked-out branch; check out the PR branch before using
`review <PR#> --fix`.

### `kagura-engineer eval`

Runs a fixed issue set twice—one memory-grounded arm and one control arm—and
reports PR, gate, and optional review uplift metrics.

```bash
kagura-engineer eval 12 14 19
kagura-engineer eval 12 14 19 --review
kagura-engineer eval 12 14 19 --json
```

This launches the full run loop twice per issue and mutates isolated branches;
`--review` also runs fix loops. Use a pinned, disposable issue set. The
measurement procedure is documented in
[docs/moat/m3-memory-uplift-eval.md](docs/moat/m3-memory-uplift-eval.md).

---

## Project layout

```text
kagura-engineer/
├── README.md
├── CHANGELOG.md
├── docs/
│   ├── README.md             # document status and navigation
│   ├── moat/                 # current operational evaluation procedures
│   ├── plan/                 # historical implementation plans
│   └── superpowers/          # historical design and planning records
├── src/kagura_engineer/
│   ├── cli.py                # init / doctor / setup / run / review / eval
│   ├── config.py             # repo.yaml schema and loading
│   ├── profile.py            # resolved execution-profile reporting
│   ├── mcp.py                # in-task memory MCP policy
│   ├── _launch.py · _http.py # platform-safe process and HTTP helpers
│   ├── doctor/               # dependency checks and reporting
│   ├── setup/                # scaffolding, install, auth, MCP setup
│   ├── run/                  # actor loop, memory, worktrees, gates, failover
│   ├── review/               # reviewer integration and auto-fix loop
│   └── eval/                 # grounded-versus-control A/B harness
└── tests/
```

Historical plans describe the state and intent at the time they were written;
they are not the current product specification. See [docs/README.md](docs/README.md)
for the document map.

---

## Development

```bash
pip install -e ".[dev]"
pytest
```

`pyproject.toml` sets `pythonpath = ["src"]`, so tests can import the package
without an editable install.

### Releasing

The canonical version is `src/kagura_engineer/__init__.py`, read by Hatch. The
Claude Code plugin manifests mirror it, and the test suite enforces equality.
Update the version assertion and all three version-bearing files together.

---

## Related public repositories

Only publicly accessible repositories are listed here.

| Repo | Role |
|---|---|
| [`memory-cloud`](https://github.com/kagura-ai/memory-cloud) | Persistent memory service and MCP server |
| [`kagura-memory-python-sdk`](https://github.com/kagura-ai/kagura-memory-python-sdk) | Memory SDK used by the Cloud client and MCP setup |
| [`kagura-code-reviewer`](https://github.com/kagura-ai/kagura-code-reviewer) | Standalone reviewer invoked by `review` |
| [`kagura-plugins`](https://github.com/kagura-ai/kagura-plugins) | Public Claude Code plugin marketplace |

---

## License

[Apache License 2.0](LICENSE) — © 2026 Kagura AI. See `LICENSE` for the full
text and `NOTICE` for attribution.
