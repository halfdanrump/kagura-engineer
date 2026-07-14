from __future__ import annotations

import logging
from pathlib import Path

from ..config import Config
from . import checks
from .result import CheckResult, Status

_log = logging.getLogger(__name__)

_WORST = {Status.OK: 0, Status.WARN: 1, Status.FAIL: 2}

# Each check is a no-arg thunk so the orchestrator can wrap it in a generic
# try/except. A buggy or partial check must not abort the entire doctor run —
# surface the failure as a FAIL CheckResult so the user sees the full picture.
#
# The third tuple element is `needs_config` (issue #71): True when the check
# reads config fields the fresh-checkout default cannot supply (ollama_url,
# cloud creds, mcp). On a missing/invalid config `run_all(None)` runs only the
# config-free subset — the classification is declared data here, not an
# if-ladder in the CLI. The config-free checks all ignore `c` (or tolerate
# `c is None`, see brain-cli), so they run identically with or without a config.
_CHECKS: list[tuple[str, callable, bool]] = [
    ("git", lambda c: checks.check_git(), False),
    # The brain backend's CLI: codex when selected, claude otherwise. This
    # occupies the same slot the unconditional claude-code check used to —
    # ordering of every other check is unchanged. With no config (c is None)
    # the backend is unknown, so default to the claude check (the Config
    # default) rather than crashing — brain-CLI presence is config-free.
    ("brain-cli", lambda c: (
        checks.check_codex()
        if (c is not None and c.brain_backend == "codex")
        else checks.check_claude_code()
    ), False),
    ("gh", lambda c: checks.check_gh(), False),
    ("ollama", lambda c: checks.check_ollama(c.ollama_url, required=c.review.models), True),
    ("haiku", lambda c: checks.check_haiku(), False),
    # "memory" is the generic group/crash-fallback label; the concrete check
    # emits the backend-specific display name ("memory-cloud" / "memory-local")
    # on success — "memory" only appears if the check itself raises.
    ("memory", lambda c: (
        checks.check_local_memory(c.local_memory_path)
        if c.memory_backend == "local"
        else checks.check_memory_cloud(c.memory_cloud_url)
    ), True),
    ("gh-issue-driven", lambda c: checks.check_gh_issue_driven(), False),
]

# Cloud-only checks: appended to the plan only when the backend is the Cloud.
# The offline SQLite backend has no MCP memory server, so the generated
# .mcp.json check (issue #36) is meaningless for it. Config-dependent by
# definition (the backend is a config field), so omitted from run_all(None).
_CLOUD_ONLY_CHECKS: list[tuple[str, callable, bool]] = [
    ("memory-mcp", lambda c: checks.check_memory_mcp(Path.cwd()), True),
    # issue #70: live-resolve config.context_id to its context NAME so a
    # wildcard/stale binding pointing recall at the wrong context is caught
    # pre-flight. Meaningless for the local backend (no cloud context).
    ("memory-context", lambda c: checks.check_memory_context(c), True),
]


def run_all(cfg: Config | None, *, exec_probe: bool = False) -> list[CheckResult]:
    """Run the dependency checks.

    With a valid `cfg`, runs the full plan as before. With `cfg is None`
    (a missing/invalid config — issue #71), runs only the config-free subset
    so `doctor` can still report a useful degraded picture instead of refusing.

    `exec_probe` (issue #93) appends the headless-exec live probe — opt-in
    because it is the only check that spends tokens and a full model
    round-trip (~30 s+). Config-free: it probes the cwd repo, not the config.
    """
    results: list[CheckResult] = []
    plan = list(_CHECKS)
    if cfg is not None and cfg.memory_backend == "cloud":
        plan += _CLOUD_ONLY_CHECKS
    if exec_probe:
        # cfg is forwarded so the probe honours the execution profile (codex →
        # skip, BYO endpoint → configured route); needs_config stays False so
        # the probe still runs in degraded mode with the default claude brain.
        plan.append(
            ("headless-exec", lambda c: checks.check_headless_exec(Path.cwd(), c), False)
        )
    for name, fn, needs_config in plan:
        if cfg is None and needs_config:
            continue
        try:
            results.append(fn(cfg))
        except Exception as exc:  # noqa: BLE001 — see docstring above
            _log.exception("doctor check %r raised", name)
            results.append(
                CheckResult(
                    name,
                    Status.FAIL,
                    f"check raised {type(exc).__name__}: {exc}",
                    "this is a doctor bug; please report it",
                )
            )
    return results


def overall_status(results: list[CheckResult]) -> Status:
    if not results:
        return Status.OK
    return max(results, key=lambda r: _WORST[r.status]).status
