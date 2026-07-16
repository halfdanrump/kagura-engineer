# Changelog

All notable changes to **kagura-engineer** are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While the project is in `0.x`, minor versions may carry breaking changes.

## [Unreleased]

## [0.5.0] — 2026-06-16

### Removed

- **BREAKING:** the milestone-harness `goal` is removed — the `kagura-engineer
  goal` CLI subcommand and the `kagura-engineer:goal` skill no longer exist. The
  harness now drives a single issue (`run`) or a PR review (`review`); to drive a
  whole milestone, loop `run` over the issues yourself. `goal` only looped `run`,
  so `run`/`review`/`doctor`/`setup`/`init`/`eval` are unaffected. (#89)

## [0.4.1] — 2026-06-13

### Fixed

- Native Windows: `doctor`/`setup` can now launch the npm `claude` `.cmd` shim
  (a COMSPEC-routing launcher fixes the `WinError 2`), `--json` no longer crashes
  under a cp932 console (UTF-8 stdout + subprocess decoding), and the
  `kagura-brain` floor is raised to `>=0.4.1`. (#78)
- The implement-phase commit no longer keeps a stray `@` as its subject line —
  the orchestrator scrubs it, reading the message as UTF-8 so it also works on a
  cp932 console. (#79, #88)
- A green `ship` that opened no PR is now recovered: the orchestrator pushes the
  branch and opens the PR itself rather than failing a complete, gate-green run. (#80)
- `failover_memory` imports and runs on native Windows — the Unix-only `fcntl`
  import is guarded and the WAL lock degrades to a no-op there. (#82)
- The local-memory privacy test is skipped on native Windows, which uses ACLs
  rather than POSIX mode bits. (#83)

## [0.4.0] — 2026-06-12

### Added

- `run` and `goal` now record which code-review provider/model the run actually
  reviewed with — the brain's in-phase `/code-review` — and surface it in both
  the human summary line and the `--json` `review` object. A run that halted
  before the implement phase reviewed nothing, shown as `review: none ran` /
  `"review": null` so it stays distinguishable. (#74)
- The in-loop `/code-review` autonomous execution is now controllable from
  `repo.yaml` with `auto` / `always` / `never`, letting operators tune whether
  the implement phase runs an inner review pass. (#75)

## [0.3.3] — 2026-06-11

### Fixed

- Windows (cp932 / non-UTF-8 locale): `init`, `doctor`, `setup`, and the review
  loop no longer crash with `UnicodeDecodeError` when a text file (`.gitignore`,
  `repo.yaml`, the reviewer envelope, credential caches) contains bytes that are
  valid UTF-8 but invalid in the OS-default codec. All text file reads and writes
  now pin `encoding="utf-8"` instead of the locale default. Reported from a
  PowerShell `kagura-engineer init` on Japanese Windows.

## [0.3.2] — 2026-06-11

### Added

- Execution-profile visibility (#70): `doctor` now prints the resolved execution
  profile (brain backend/endpoint + in-task-MCP policy, reviewer model + Ollama
  URL, memory backend/workspace/context/failover/MCP) above the check table, and
  `run`/`goal`/`review`/`eval` print it as a startup header (suppressed under
  `--json`). A new cloud-only `memory-context` doctor check live-resolves
  `context_id` to its context **name** via the memory SDK, catching a
  wildcard/stale binding that points recall at the wrong context. Every `--json`
  report gains a `profile` field, and `run` emits a grounding-evidence line
  (`grounding: pinned N + recalled M from context …`) after the recall phase.

### Changed

- First-install UX (#71): `doctor` and `setup` no longer refuse on a missing or
  invalid `repo.yaml`. `setup` auto-scaffolds the template (as `init` does) and
  reports a synthetic `config` NEEDS_USER step while still running the
  config-free steps; `doctor` prints a degraded report — a `config` FAIL row plus
  every config-free check — instead of exiting early. `kagura-engineer setup` is
  now the only command a fresh checkout needs to type first.

## [0.3.1] — 2026-06-11

### Added

- `eval` command (#57, moat lever M3): an A/B harness that measures whether memory
  grounding measurably improves PR quality. It drives the **same** fixed issue set
  through two arms — grounded (the normal `run` loop) and control (`run_idea` with
  the new `ground=False` switch, no grounding injected) — and prints an A/B table
  on objective signals already in the pipeline: PR-reached rate, gate-verdict rate,
  and (with `--review`) review findings + re-fix-loop iterations. Emits a
  reproducible JSON artifact and an `improved`/`regressed`/`neutral`/`inconclusive`
  uplift verdict. `kagura-engineer eval <issue> [<issue> …] [--review] [--json]`.
  See `docs/moat/m3-memory-uplift-eval.md`.
- Brain backend selection (#51): `brain_backend: claude | codex` (+ optional
  `brain_endpoint`, e.g. `ollama-cloud`) in repo.yaml routes the run loop through
  the chosen kagura-brain adapter; the API key comes from `KAGURA_BRAIN_API_KEY`,
  never repo.yaml.
- `enable_codex_mcp` config seam (#68): formalizes the codex in-task-MCP
  policy-vs-capability divergence as config data ("capable but disabled by
  policy", default off). The flag-on path forwards the resolved MCP config to
  codex with backend-correct tool ids, fails cleanly on a bad config file, and
  logs its known caveats (no per-call tool allow-list on codex; not yet
  smoke-verified end-to-end).

### Changed

- Adopted `kagura_brain.select()` and retired the consumer-side `brain_select`
  core (#63) — the engineer keeps only the `supports_mcp` policy shim over the
  library's `BrainHandle`.

### Fixed

- `goal`/`run`: a green-reviewed, CI-green PR is no longer reported as "fail" —
  the ship guard cross-checks GitHub for the PR instead of trusting a missing
  verdict marker (#64).
- `review --fix`: the loop closes the memory client it creates (no more hang at
  exit) (#56).
- WAL `drain()` now serializes with `flock` — concurrent runs no longer
  duplicate-replay or drop records — and tolerates a corrupt tail (#55).
- `parse_verdict` anchors marker extraction to the tail of stdout, so an echoed
  marker mid-transcript can no longer spoof the gate verdict (#54).
- `select_brain` fails fast (clean `ConfigError`) on a half-configured BYO pair
  (`KAGURA_BRAIN_API_KEY` without `brain_endpoint`), instead of a mid-run
  traceback at the first invoke.

### Security

- WAL and local SQLite files are created `0600`/`0700` (no longer
  umask-dependent world-readable) — memory payloads no longer leak to other
  local users (#53).

## [0.3.0] — 2026-06-09

### Added

- `init` command (#35): scaffolds a commented `repo.yaml` template (never
  overwrites an existing one) and idempotently adds `repo.yaml` to `.gitignore`,
  so a fresh checkout starts configured and the workspace/context IDs stay out
  of git by default. `kagura-engineer init [--dir <path>]`.
- `setup` now generates `<repo>/.mcp.json` via the Kagura Memory SDK 0.30
  (secretless stdio / OAuth-profile form, or `url`+`Bearer` for `KAGURA_API_KEY`),
  so a child `claude -p` can use Memory Cloud in-task without a hand-authored
  config. (#36)
- `config` rejects unknown `repo.yaml` keys via `extra="forbid"` on the Config
  model — a typo'd key now fails loudly instead of being silently ignored. (#46)

### Changed

- Migrated the shared headless `claude -p` launcher dependency from
  `kagura-claude-harness` to its renamed, restructured successor `kagura-brain`
  (engineer uses the `claude` adapter; runtime behavior unchanged). (#48)
- Adopted the shared harness as the single `claude -p` launcher seam, removing
  kagura-engineer's own argv construction (the unhardened #34 twin). (#40)
- `setup` (memory-mcp step) now adds the generated secret files (`.mcp.json`
  and, under `--full`, `.kagura.json`) to `.gitignore` **before** writing them —
  both bake in a bearer key / api_key, so the ignore rules are established first
  (fail-secure: a write failure can never leave an un-ignored secret on disk).
  Reuses the new `init` scaffold helper rather than duplicating the gitignore
  logic (#35).
- `config`: a missing `repo.yaml` now points the user at `kagura-engineer init`
  instead of only reporting the absence (#35).

### Fixed

- `run`: persist the child's stdout on the silent "green ship, no PR" FAIL path
  so the failure is diagnosable instead of vanishing. (#38)

## [0.2.1] — 2026-06-09

### Fixed

- `doctor` / `setup`: the Memory Cloud `/health` probe now sends a
  `User-Agent` (`kagura-engineer/<version>`). Cloudflare blocks the stdlib
  default `Python-urllib/x` signature with HTTP 403 (CF error 1010), which made
  a perfectly healthy Memory Cloud host look unreachable.
- `doctor`: a Claude model name (`haiku` / `sonnet` / `opus` / `claude-*`) in
  `review.models` no longer suggests the impossible `ollama pull <name>` — it
  explains that `review.models` lists **Ollama** model names for the reviewer.

### Changed

- Skill wrappers now carry usage help: `kagura-engineer:run` and `:goal` print
  a usage block (with an example) when invoked without their required argument
  instead of guessing; `:review` / `:doctor` / `:setup` show a one-line usage.

## [0.2.0] — 2026-06-09

### Added

- **Claude Code skill-plugin wrapper** (`.claude-plugin/` + `skills/`) — `kagura-engineer`
  is now installable and discoverable as a plugin. Five thin skills
  (`kagura-engineer:doctor` / `:setup` / `:run` / `:review` / `:goal`) shell out to the
  CLI; referenceable by the `kagura-plugins` marketplace as a Tier-2 Harness. (#28, #30)
- **Failover memory** — Cloud-primary write durability backed by a local WAL, so a
  transient Memory Cloud outage no longer drops savepoints. (#27)
- **Incremental phase progress** streamed to stdout during `run` (suppressed under
  `--json`). (#12)
- `doctor` / `setup` now guide the Memory Cloud credential, with the README reconciled. (#6)

### Changed

- Aligned the feedback-weight contract across the cloud and local memory backends. (#21)
- Dropped the stale README "Status" table and added a "Releasing" note documenting the
  version single-source-of-truth (`__init__.py`, mirrored by the plugin manifests). (#31)
- `repo.yaml` is gitignored as per-checkout config. (#7)

### Fixed

- Added a dedicated `implement` phase between `start` and `ship`. (#9)
- Bridged the async kagura-memory SDK from the sync `MemoryClient`, and close the client
  so cloud `run`/`goal` exit cleanly. (#1, #14)
- A green `ship` that produced no PR URL now fails instead of reporting success. (#18)
- Made the native `## Verdict:` fallback phase-aware, used when the `KAGURA_VERDICT=`
  marker is absent. (#3)
- Mapped cloud feedback weight to `helpful` so reinforcement works. (#16)
- Surfaced headless `claude` stdout on failure with an `ANTHROPIC_API_KEY` hint, tightened
  the auth-failure regex, and documented `--json` progress suppression. (#19)

### Internal

- Added a skill↔CLI verb-drift guard test so a new/renamed CLI verb without a matching
  skill fails CI. (#31)

## [0.1.0] — 2026-06-08

First public release. `kagura-engineer` is a `0.x` autonomous coding harness over
[Claude Code](https://claude.ai/code) and Kagura Memory — not a finished actor.

### Added

- `doctor` — diagnose the dependency chain (git, claude, gh, ollama, Memory Cloud)
  with a backend-aware memory check.
- `setup` — install missing dependencies via the platform package manager and
  bootstrap auth; idempotent and re-runnable.
- `run` — memory-grounded agent loop driving an issue toward a PR, with
  cheap-resume savepoints and grounding enriched by recall + explore neighbours.
- `review` — launch the separate `kagura-code-reviewer` console script, read its
  JSON envelope, and gate on the verdict (`red`→halt, `green`/`yellow`→proceed).
  Available as the optional `review` extra (`pip install "kagura-engineer[review]"`);
  degrades to a clean FAIL gate when the reviewer is absent.
- `review --fix` — auto review/fix loop (Plan 4b).
- `goal <milestone>` — drive a whole milestone, with an optional `--unattended` flag.
- `LocalMemoryClient` — offline SQLite memory backend (no API key required),
  with local pinning (pin/unpin), recall tag/importance filters, graph `explore`,
  and decay maintenance clamped to `[0, 1]`.
- `memory_mcp_config` — additively attach the memory MCP server to the headless
  `claude` subprocess.
- `--version` flag on the top-level CLI.
- `memory_backend: local` no longer requires the Memory Cloud fields
  (`memory_cloud_url`/`workspace_id`/`context_id`); an offline `repo.yaml` is
  just `profile` + `memory_backend: local`. They stay required for the (default)
  cloud backend.

[Unreleased]: https://github.com/kagura-ai/kagura-engineer/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/kagura-ai/kagura-engineer/compare/v0.4.1...v0.5.0
[0.4.1]: https://github.com/kagura-ai/kagura-engineer/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/kagura-ai/kagura-engineer/compare/v0.3.3...v0.4.0
[0.3.3]: https://github.com/kagura-ai/kagura-engineer/compare/v0.3.2...v0.3.3
[0.3.2]: https://github.com/kagura-ai/kagura-engineer/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/kagura-ai/kagura-engineer/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/kagura-ai/kagura-engineer/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/kagura-ai/kagura-engineer/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/kagura-ai/kagura-engineer/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/kagura-ai/kagura-engineer/releases/tag/v0.1.0
