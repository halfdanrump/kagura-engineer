from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from kagura_brain import claude as brain_claude

from .._http import build_request
from .._launch import run_text
from ..setup.auth import AuthMethod, resolve_anthropic_auth
from ..setup.memory_auth import MemoryAuthMethod, resolve_memory_cloud_auth
from ..setup.ollama import model_present
from .result import CheckResult, Status

_TIMEOUT = 5


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    # issue #78: route through run_text so a Windows `.cmd` shim (claude/codex via
    # npm) resolves through COMSPEC instead of raising WinError 2, and child output
    # is decoded UTF-8/replace so a cp932 console can't crash the reader thread.
    return run_text(cmd, capture_output=True, timeout=_TIMEOUT)


def check_git() -> CheckResult:
    if shutil.which("git") is None:
        return CheckResult(
            "git",
            Status.FAIL,
            "git not found on PATH",
            "install git via your package manager (brew/apt/dnf/pacman) and re-run doctor",
        )
    try:
        proc = _run(["git", "rev-parse", "--is-inside-work-tree"])
    except (OSError, subprocess.SubprocessError) as exc:
        return CheckResult("git", Status.FAIL, f"git invocation failed: {exc}", None)
    if proc.returncode == 0 and proc.stdout.strip() == "true":
        return CheckResult("git", Status.OK, "inside a git work tree")
    return CheckResult(
        "git",
        Status.WARN,
        "not inside a git work tree",
        "cd into the target repo before running",
    )


def check_claude_code() -> CheckResult:
    if shutil.which("claude") is None:
        return CheckResult(
            "claude-code",
            Status.FAIL,
            "claude not found on PATH",
            "install Claude Code (https://claude.ai/download) and re-run doctor",
        )
    try:
        proc = _run(["claude", "--version"])
    except (OSError, subprocess.SubprocessError) as exc:
        return CheckResult(
            "claude-code", Status.FAIL, f"claude invocation failed: {exc}", None
        )
    if proc.returncode != 0:
        return CheckResult(
            "claude-code",
            Status.FAIL,
            f"`claude --version` exited {proc.returncode}",
            "reinstall/repair Claude Code",
        )
    version = proc.stdout.strip() or "unknown"
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key is not None and key == "":
        return CheckResult(
            "claude-code",
            Status.FAIL,
            f"v{version}, ANTHROPIC_API_KEY is set to empty string",
            "unset it to fall back to your `claude login` subscription "
            "(recommended), or set a real value (`export ANTHROPIC_API_KEY=sk-ant-...`)",
        )
    if key:
        return CheckResult("claude-code", Status.OK, f"v{version}, auth=api_key")
    return CheckResult(
        "claude-code",
        Status.WARN,
        f"v{version}, auth=subscription (unverified)",
        "run `claude` once interactively to confirm subscription login",
    )


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
    except (OSError, subprocess.SubprocessError) as exc:
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


def check_gh() -> CheckResult:
    if shutil.which("gh") is None:
        return CheckResult(
            "gh", Status.FAIL, "gh not found on PATH", "install gh (https://cli.github.com/) and re-run doctor"
        )
    try:
        proc = _run(["gh", "auth", "status"])
    except (OSError, subprocess.SubprocessError) as exc:
        return CheckResult("gh", Status.FAIL, f"gh invocation failed: {exc}", None)
    if proc.returncode == 0:
        return CheckResult("gh", Status.OK, "authenticated")
    return CheckResult("gh", Status.FAIL, "not authenticated", "gh auth login")


def _http_json(url: str) -> dict:
    # build_request sets a User-Agent — Cloudflare 403s the stdlib default (see _http.py).
    with urllib.request.urlopen(build_request(url), timeout=_TIMEOUT) as resp:  # noqa: S310 (trusted config URL)
        return json.loads(resp.read())


def _http_reach(url: str) -> None:
    """Open url to confirm reachability; raises on connection/HTTP error. Body ignored."""
    with urllib.request.urlopen(build_request(url), timeout=_TIMEOUT) as resp:  # noqa: S310 (trusted config URL)
        resp.read()  # body discarded; open succeeding is sufficient proof of reachability


_CLAUDE_MODEL_NAMES = {"haiku", "sonnet", "opus"}


def _looks_like_claude_model(name: str) -> bool:
    """True if `name` is a Claude model (haiku/sonnet/opus/claude-*), which has
    no place in `review.models` (those are Ollama model names for the reviewer)."""
    base = name.split(":")[0].strip().lower()
    return base in _CLAUDE_MODEL_NAMES or base.startswith("claude")


def check_ollama(base_url: str, required: list[str]) -> CheckResult:
    try:
        data = _http_json(f"{base_url.rstrip('/')}/api/tags")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return CheckResult(
            "ollama", Status.FAIL, f"daemon unreachable: {exc}", "ollama serve"
        )
    if not isinstance(data, dict):
        return CheckResult(
            "ollama",
            Status.WARN,
            "unexpected /api/tags response shape",
            "verify the ollama_url points at an Ollama daemon",
        )
    # Skip entries that aren't dicts or lack a (truthy) `name`; otherwise a
    # malformed entry injects None into `have`, inflating the count and
    # crashing the base-name match via None.split().
    have = {
        name
        for m in (data.get("models") or [])
        if isinstance(m, dict) and (name := m.get("name"))
    }
    missing = [m for m in required if not model_present(m, have)]
    if missing:
        # `review.models` are Ollama model names for the reviewer. A Claude
        # model name (haiku/sonnet/opus/claude-*) there is a config mistake —
        # `ollama pull haiku` would fail, so guide instead of suggesting it.
        claude_like = [m for m in missing if _looks_like_claude_model(m)]
        ollama_missing = [m for m in missing if m not in claude_like]
        if claude_like:
            hint = (
                f"'{claude_like[0]}' is a Claude model, not an Ollama model — "
                "review.models lists Ollama model names for the reviewer; "
                "remove it or use an Ollama model"
            )
            if ollama_missing:
                hint += f". For the rest: ollama pull {' && ollama pull '.join(ollama_missing)}"
            return CheckResult(
                "ollama", Status.WARN, f"missing models: {', '.join(missing)}", hint
            )
        return CheckResult(
            "ollama",
            Status.WARN,
            f"missing models: {', '.join(missing)}",
            f"ollama pull {' && ollama pull '.join(missing)}",
        )
    return CheckResult("ollama", Status.OK, f"{len(have)} models available")


def check_haiku() -> CheckResult:
    # Empty string is a deliberate "unset" signal; surface it as a
    # config error so the user fixes it before re-running.
    raw = os.environ.get("ANTHROPIC_API_KEY")
    if raw is not None and raw == "":
        return CheckResult(
            "haiku",
            Status.FAIL,
            "ANTHROPIC_API_KEY is set to empty string",
            "unset or set a real value",
        )
    res = resolve_anthropic_auth()
    if res.method is AuthMethod.ENV_API_KEY:
        return CheckResult(
            "haiku",
            Status.OK,
            "env ANTHROPIC_API_KEY is set; no API probe in P1",
        )
    if res.method is AuthMethod.SUBSCRIPTION_CACHE:
        # Decorate the detail with cache age (informational, not gating).
        assert res.cache_path is not None
        try:
            age_days = (time.time() - res.cache_path.stat().st_mtime) / 86400
        except OSError:
            age_days = 0
        return CheckResult(
            "haiku",
            Status.OK,
            f"{res.detail}, {age_days:.0f}d old; no live probe in P1",
        )
    return CheckResult(
        "haiku",
        Status.WARN,
        "no API key; relies on Claude Code subscription path (P1: env presence only, no live probe)",
        "set ANTHROPIC_API_KEY or run `claude` once interactively to establish a subscription login",
    )


# The canonical fix for a missing Memory Cloud credential. Names BOTH
# supported sources (issue #6 acceptance: env key and `kagura auth login`
# are both honoured, env-first) so the hint matches what `run/memory.py`
# actually consumes — no more README/code mismatch.
_MEMORY_AUTH_HINT = (
    "export KAGURA_API_KEY=... or run `kagura auth login` to authenticate Memory Cloud"
)


def check_memory_cloud(
    base_url: str,
    *,
    env: dict[str, str] | None = None,
    home: "Path | None" = None,
) -> CheckResult:
    # Resolve the credential FIRST (cheap, local). Unlike the old probe this
    # check no longer passes silently when the host is up but no credential
    # resolves — that is the exact first-run footgun from issue #6 (doctor
    # passes, `run` dies). env/home are injectable for tests; production reads
    # os.environ / Path.home() via the resolver's defaults.
    auth = resolve_memory_cloud_auth(env=env, home=home)

    # Extract host-only form so that any userinfo (basic auth) embedded in
    # `memory_cloud_url` is NOT echoed into the doctor detail string —
    # `doctor --json` is a common artefact in CI logs and chat pastes.
    # `urlparse(...).hostname` drops username:password@ automatically.
    from urllib.parse import urlparse
    try:
        host_only = urlparse(base_url).hostname or base_url
    except (ValueError, TypeError):
        host_only = base_url
    try:
        _http_reach(f"{base_url.rstrip('/')}/health")
    except urllib.error.HTTPError as exc:
        # An HTTP response (even 4xx/5xx) proves the host is reachable. A 4xx is
        # often the auth layer itself rejecting an absent/bad credential, so when
        # no credential resolves we point straight at the fix instead of deferring.
        if auth.method is MemoryAuthMethod.NONE:
            return CheckResult(
                "memory-cloud",
                Status.WARN,
                f"reachable but /health returned HTTP {exc.code}; no credential resolves",
                _MEMORY_AUTH_HINT,
            )
        return CheckResult(
            "memory-cloud",
            Status.WARN,
            f"reachable but /health returned HTTP {exc.code} (auth={auth.detail})",
            "verify the Memory Cloud endpoint / credential",
        )
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # ValueError covers a malformed/schemeless memory_cloud_url
        # ("unknown url type" / "Invalid IPv6 URL"); urlopen raises it
        # before any network attempt. Match check_ollama, which already
        # guards ValueError, so a bad URL FAILs cleanly instead of
        # crashing the whole doctor command (run_all has no isolation).
        return CheckResult(
            "memory-cloud",
            Status.FAIL,
            f"unreachable: {exc}",
            "check config.memory_cloud_url / network",
        )
    # Host is up. Now gate on the credential: a reachable host with no
    # resolvable credential is a WARN, not an OK.
    if auth.method is MemoryAuthMethod.NONE:
        return CheckResult(
            "memory-cloud",
            Status.WARN,
            f"reachable at {host_only}, but no Memory Cloud credential resolves",
            _MEMORY_AUTH_HINT,
        )
    return CheckResult(
        "memory-cloud", Status.OK, f"reachable at {host_only}; auth={auth.detail}"
    )


def check_memory_mcp(
    repo_dir: "Path",
    *,
    env: dict[str, str] | None = None,
    home: "Path | None" = None,
) -> CheckResult:
    """Verify the generated `<repo>/.mcp.json` exists and resolves to a usable
    memory MCP server (issue #36).

    Headless `claude -p` reaches the kagura-memory tools through this file. A
    missing/stale config means in-task recall silently degrades to no MCP tools,
    so this is a WARN (advisory: `kagura-engineer setup` regenerates it), not a
    hard FAIL. The credential is re-checked because a stdio config whose OAuth
    profile has since gone away will 401 every call.
    """
    from kagura_memory.setup_claude import detect_mcp_json_mode

    mode = detect_mcp_json_mode(Path(repo_dir))
    if mode in ("none", "absent"):
        return CheckResult(
            "memory-mcp",
            Status.WARN,
            "no usable .mcp.json; headless recall has no MCP memory tools",
            "run `kagura-engineer setup` to generate <repo>/.mcp.json",
        )

    auth = resolve_memory_cloud_auth(env=env, home=home)
    if auth.method is MemoryAuthMethod.NONE:
        return CheckResult(
            "memory-mcp",
            Status.WARN,
            f".mcp.json present ({mode}) but no Memory Cloud credential resolves",
            _MEMORY_AUTH_HINT,
        )
    return CheckResult(
        "memory-mcp", Status.OK, f".mcp.json present ({mode}); auth={auth.detail}"
    )


def _fetch_context_info(cfg) -> "object":
    """Live-resolve `cfg.context_id` via the memory SDK's `get_context_info`.

    The SDK is async; bridge on a throwaway loop (this is a one-shot doctor
    probe, not the run loop's persistent client). Errors propagate — the
    caller (check_memory_context) degrades them to FAIL.
    """
    import asyncio

    import kagura_memory

    from ..run.memory import _mcp_url

    sdk = kagura_memory.KaguraClient(
        api_key=os.environ.get("KAGURA_API_KEY"),
        mcp_url=_mcp_url(cfg.memory_cloud_url),
    )
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(sdk.get_context_info(cfg.context_id))
    finally:
        try:
            closer = getattr(sdk, "close", None)
            if closer is not None:
                loop.run_until_complete(closer())
        except Exception:  # noqa: BLE001 — teardown is best-effort
            pass
        loop.close()


_CONTEXT_ID_HINT = "check config.context_id (and that it belongs to your workspace)"


def check_memory_context(cfg, *, fetch=_fetch_context_info) -> CheckResult:
    """Verify `cfg.context_id` resolves to a real, accessible context and show
    its NAME (issue #70) — the wrong-context-recall incident detector.

    Cloud-only (the registry skips it for memory_backend=local). Never raises:
    SDK/network/auth errors degrade to FAIL with the error string, mirroring
    check_memory_cloud's taxonomy. `fetch` is injectable for tests.
    """
    try:
        info = fetch(cfg)
    except Exception as exc:  # noqa: BLE001 — degrade every SDK leak to FAIL
        return CheckResult(
            "memory-context",
            Status.FAIL,
            f"could not resolve context {cfg.context_id}: {exc}",
            _CONTEXT_ID_HINT,
        )
    detail_ctx = getattr(info, "context", None)
    name = (
        getattr(detail_ctx, "display_name", None)
        or getattr(detail_ctx, "name", None)
    )
    if not name:
        return CheckResult(
            "memory-context",
            Status.FAIL,
            f"context {cfg.context_id} resolved without a usable name",
            _CONTEXT_ID_HINT,
        )
    return CheckResult(
        "memory-context", Status.OK, f'context {cfg.context_id} → "{name}"'
    )


def check_local_memory(path: str) -> CheckResult:
    """Verify the offline SQLite memory backend (Plan 5) can be created and
    written at `path` — the local counterpart to the cloud reachability probe."""
    db = Path(path)
    try:
        db.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return CheckResult(
            "memory-local",
            Status.FAIL,
            f"cannot create directory {db.parent}: {exc}",
            "set config.local_memory_path to a writable location",
        )
    try:
        conn = sqlite3.connect(str(db))
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS _doctor_probe (x INTEGER)")
            conn.execute("DROP TABLE _doctor_probe")
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return CheckResult(
            "memory-local",
            Status.FAIL,
            f"cannot open/write SQLite db at {db}: {exc}",
            "set config.local_memory_path to a writable location",
        )
    return CheckResult("memory-local", Status.OK, f"writable at {db}")


def check_gh_issue_driven() -> CheckResult:
    """Verify the gh-issue-driven plugin is installed.

    `run` (Plan 3) drives gh-issue-driven via headless claude; without the
    plugin the run would die deep inside a session. This is a blocking
    check (Status.FAIL ⇒ CheckResult.is_blocking), so `run`'s guard can
    refuse to start. Plugin root is overridable via KAGURA_PLUGINS_DIR
    for tests.
    """
    root = Path(os.environ.get("KAGURA_PLUGINS_DIR") or str(Path.home() / ".claude" / "plugins"))
    hits = [p for p in root.glob("**/gh-issue-driven") if p.is_dir()] if root.exists() else []
    if hits:
        return CheckResult("gh-issue-driven", Status.OK, "plugin installed")
    return CheckResult(
        "gh-issue-driven",
        Status.FAIL,
        "gh-issue-driven plugin not found",
        "install the gh-issue-driven Claude Code plugin (run requires it)",
    )


# --- headless-exec probe (issue #93) ---------------------------------------
#
# A headless `claude -p` cannot answer Claude Code's permission prompts, so
# every capability a run needs must be pre-granted (the repo's
# `.claude/settings.json` allowlist + the workspace-trust flag). None of the
# static checks can see those grants, and the walls fail one at a time at
# runtime — Bash first (start), Edit/Write later (implement). The probe asks a
# real headless claude, in the target repo, to exercise both capabilities and
# report which one is blocked. Opt-in (`doctor --exec-probe`): it is the only
# check that spends tokens and a model round-trip.
#
# Launch goes through kagura_brain.claude.invoke — the single hardened
# `claude -p` launcher (#40): env scrub, Windows shim resolution, timeout-as-
# result. Constructing a bare claude argv here would revive the unhardened
# twin the #40 migration removed (test_launcher_seam guards it).

_EXEC_PROBE_MARKER = "KAGURA_EXEC_PROBE"
_EXEC_PROBE_TMP = ".kagura-exec-probe.tmp"
# A model round-trip plus two tool calls, not a 5 s CLI check.
_EXEC_PROBE_TIMEOUT = 180

_EXEC_PROBE_PROMPT = (
    "You are a non-interactive permissions probe for this repository. Do "
    "exactly this, in order, and nothing else:\n"
    "1. Run the command `git status` with your Bash tool.\n"
    f"2. Create a file named {_EXEC_PROBE_TMP} containing the single word "
    "probe, using your file-writing tool.\n"
    "If a tool call is blocked, denied, or requires approval, record that "
    "capability as blocked and continue to the next step. Do not attempt "
    "workarounds or alternative tools.\n"
    "Finally print exactly one line, last:\n"
    f"{_EXEC_PROBE_MARKER} bash=<ok|blocked> write=<ok|blocked>\n"
)

_EXEC_PROBE_HINT = (
    "pre-grant headless permissions: add the needed Bash(...) patterns plus "
    "Edit/Write to permissions.allow in <repo>/.claude/settings.json, and "
    "trust the repo (and the .kagura-runs worktree dir) in Claude Code — a "
    "headless run cannot answer approval prompts; see README § Headless "
    "permissions"
)


def _parse_exec_probe_marker(stdout: str) -> dict[str, str] | None:
    """The capability map from the LAST marker line, or None if absent.

    Last-wins mirrors the run gate's trailing-marker rule: the prompt itself
    contains a template of the marker line, and some transcripts echo it, so
    the first occurrence may be the template rather than the answer. A
    template echo (`bash=<ok|blocked>`) parses as blocked-ish values, which is
    exactly why only the last line counts.
    """
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith(_EXEC_PROBE_MARKER):
            continue
        caps: dict[str, str] = {}
        for token in line[len(_EXEC_PROBE_MARKER):].split():
            key, sep, value = token.partition("=")
            if sep:
                caps[key] = value
        return caps
    return None


def check_headless_exec(repo: Path) -> CheckResult:
    """Live-probe that a headless claude can act in `repo` (issue #93).

    Runs `claude -p` in the repo asking it to (a) run one harmless command and
    (b) write one temp file, then parses the reported capability map. Catches
    all three permission walls (Bash allowlist, workspace trust, Edit/Write)
    before a real run burns a dispatch on them.
    """
    name = "headless-exec"
    if shutil.which("claude") is None:
        return CheckResult(
            name,
            Status.FAIL,
            "claude not found on PATH; cannot probe",
            "fix the brain-cli check first, then re-run with --exec-probe",
        )
    try:
        res = brain_claude.invoke(
            _EXEC_PROBE_PROMPT, cwd=repo, timeout=_EXEC_PROBE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return CheckResult(name, Status.FAIL, f"probe launch failed: {exc}", None)
    finally:
        # The probe file is written by the child; remove it no matter how the
        # probe ended so repeated doctor runs never dirty the work tree.
        try:
            (repo / _EXEC_PROBE_TMP).unlink()
        except OSError:
            pass
    if res.timed_out:
        return CheckResult(
            name,
            Status.FAIL,
            f"probe timed out after {_EXEC_PROBE_TIMEOUT}s",
            "a headless claude that hangs usually awaits a permission "
            "approval nobody can give; " + _EXEC_PROBE_HINT,
        )
    if res.returncode != 0:
        return CheckResult(
            name,
            Status.FAIL,
            f"claude exited {res.returncode}: {res.detail() or '(no output)'}",
            None,
        )
    caps = _parse_exec_probe_marker(res.stdout or "")
    if caps is None:
        return CheckResult(
            name,
            Status.WARN,
            "probe ran but printed no marker line; capability state unknown",
            "re-run with --exec-probe; if it persists, probe manually with "
            "`claude -p` in the repo",
        )
    blocked = []
    if caps.get("bash") != "ok":
        blocked.append("commands (Bash)")
    if caps.get("write") != "ok":
        blocked.append("file edits (Write)")
    if blocked:
        return CheckResult(
            name,
            Status.FAIL,
            "headless claude is blocked on: " + ", ".join(blocked),
            _EXEC_PROBE_HINT,
        )
    return CheckResult(
        name,
        Status.OK,
        "headless claude can run commands and edit files in this repo",
    )
