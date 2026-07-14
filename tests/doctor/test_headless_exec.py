"""check_headless_exec — the opt-in live probe that a headless brain can
actually act where `run` runs (issue #93).

A headless run has no human to answer Claude Code's permission prompts, so a
repo that passes every static doctor check can still red-halt at runtime on
(1) a missing Bash allowlist, (2) an untrusted workspace — including the
`.kagura-runs` worktree's own trust, distinct from the repo's — or
(3) missing Edit/Write grants. The probe exercises both capabilities inside
an ephemeral run worktree and verifies the write on disk rather than trusting
the model's self-report.
"""
import subprocess
import types
from contextlib import contextmanager

from kagura_brain.core import BrainResult

from kagura_engineer.doctor import checks, registry
from kagura_engineer.doctor.result import CheckResult, Status

_PROBE_NAME = ".kagura-exec-probe-fixed.tmp"


def _result(returncode=0, stdout="", stderr="", timed_out=False):
    return BrainResult(
        returncode=returncode, stdout=stdout, stderr=stderr, timed_out=timed_out
    )


def _fix_workdir(monkeypatch, tmp_path, caveat=None):
    """Pin the probe workdir to tmp_path (no real git worktree in unit tests)
    and the probe filename to a known value."""

    @contextmanager
    def fake_workdir(repo):
        yield tmp_path, caveat

    monkeypatch.setattr(checks, "_probe_workdir", fake_workdir)
    monkeypatch.setattr(checks, "_exec_probe_name", lambda: _PROBE_NAME)


def _probe(
    monkeypatch, tmp_path, *,
    stdout="", returncode=0, timed_out=False, write_file=False, caveat=None,
):
    monkeypatch.setattr(checks.shutil, "which", lambda _: "/usr/bin/claude")
    _fix_workdir(monkeypatch, tmp_path, caveat)

    def fake_invoke(prompt, **kwargs):
        if write_file:
            (tmp_path / _PROBE_NAME).write_text("probe")
        return _result(returncode, stdout, timed_out=timed_out)

    monkeypatch.setattr(checks.brain_claude, "invoke", fake_invoke)
    return checks.check_headless_exec(tmp_path)


_BOTH_OK = "KAGURA_EXEC_PROBE bash=ok write=ok\n"


def test_fail_when_claude_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(checks.shutil, "which", lambda _: None)
    r = checks.check_headless_exec(tmp_path)
    assert r.status is Status.FAIL
    assert "claude" in r.detail


def test_codex_profile_is_skipped_not_probed(monkeypatch, tmp_path):
    # The probe exercises Claude Code permission walls; codex has its own
    # sandbox/approval model, so the profile is explicitly skipped (WARN),
    # never probed through the wrong launcher.
    called = []
    monkeypatch.setattr(checks.brain_claude, "invoke", lambda *a, **k: called.append(1))
    cfg = types.SimpleNamespace(brain_backend="codex")
    r = checks.check_headless_exec(tmp_path, cfg)
    assert r.status is Status.WARN
    assert "codex" in r.detail
    assert called == []


def test_ok_when_both_capabilities_pass_and_write_observed(monkeypatch, tmp_path):
    r = _probe(monkeypatch, tmp_path, stdout=_BOTH_OK, write_file=True)
    assert r.status is Status.OK
    assert "commands" in r.detail and "edit files" in r.detail
    assert "worktree" in r.detail


def test_fail_when_write_marker_ok_but_file_never_observed(monkeypatch, tmp_path):
    # The model can misreport or skip a tool call — a write=ok marker without
    # the file on disk must not produce a green pre-flight.
    r = _probe(monkeypatch, tmp_path, stdout=_BOTH_OK, write_file=False)
    assert r.status is Status.FAIL
    assert "never observed" in r.detail


def test_fail_when_bash_blocked(monkeypatch, tmp_path):
    r = _probe(
        monkeypatch, tmp_path,
        stdout="KAGURA_EXEC_PROBE bash=blocked write=ok\n", write_file=True,
    )
    assert r.status is Status.FAIL
    assert "commands" in r.detail
    assert ".claude/settings.json" in r.fix_hint
    assert ".kagura-runs" in r.fix_hint  # trust must cover the worktree parent too


def test_fail_when_write_blocked(monkeypatch, tmp_path):
    r = _probe(
        monkeypatch, tmp_path,
        stdout="KAGURA_EXEC_PROBE bash=ok write=blocked\n",
    )
    assert r.status is Status.FAIL
    assert "file edits" in r.detail
    assert ".claude/settings.json" in r.fix_hint


def test_prompt_uses_approval_requiring_command_and_unique_name(monkeypatch, tmp_path):
    # Read-only git is approval-free in every Claude Code mode, so a
    # `git status` probe would report bash=ok in the exact missing-allowlist
    # case; the probe must exercise a command that needs a real grant.
    seen = {}
    monkeypatch.setattr(checks.shutil, "which", lambda _: "/usr/bin/claude")
    _fix_workdir(monkeypatch, tmp_path)

    def fake_invoke(prompt, **kwargs):
        seen["prompt"] = prompt
        (tmp_path / _PROBE_NAME).write_text("probe")
        return _result(0, _BOTH_OK)

    monkeypatch.setattr(checks.brain_claude, "invoke", fake_invoke)
    checks.check_headless_exec(tmp_path)
    assert "gh auth status" in seen["prompt"]
    assert "git status" not in seen["prompt"]
    assert _PROBE_NAME in seen["prompt"]


def test_probe_names_are_unique_per_call():
    # Uniqueness is what makes the cleanup safe (it can never unlink a
    # pre-existing user file) and the observed-write check meaningful.
    assert checks._exec_probe_name() != checks._exec_probe_name()


def test_last_marker_wins(monkeypatch, tmp_path):
    # The prompt echoes a template of the marker line in some transcripts;
    # only the LAST marker line is the probe's answer.
    r = _probe(
        monkeypatch, tmp_path,
        stdout=(
            "KAGURA_EXEC_PROBE bash=<ok|blocked> write=<ok|blocked>\n"
            "doing things...\n" + _BOTH_OK
        ),
        write_file=True,
    )
    assert r.status is Status.OK


def test_warn_when_no_marker(monkeypatch, tmp_path):
    r = _probe(monkeypatch, tmp_path, stdout="I did some things but forgot.\n")
    assert r.status is Status.WARN
    assert "marker" in r.detail


def test_fail_when_brain_exits_nonzero(monkeypatch, tmp_path):
    r = _probe(monkeypatch, tmp_path, stdout="", returncode=1)
    assert r.status is Status.FAIL
    assert "exited 1" in r.detail


def test_fail_on_timeout(monkeypatch, tmp_path):
    # kagura_brain reports a timeout as a result flag, not an exception.
    r = _probe(monkeypatch, tmp_path, returncode=1, timed_out=True)
    assert r.status is Status.FAIL
    assert "timed out" in r.detail


def test_worktree_fallback_caps_ok_at_warn(monkeypatch, tmp_path):
    # If the ephemeral worktree can't be created, the probe degrades to the
    # repo dir — but an unexercised worktree context must not read as fully
    # healthy, so a would-be OK is capped at WARN naming the gap.
    r = _probe(
        monkeypatch, tmp_path,
        stdout=_BOTH_OK, write_file=True, caveat="worktree creation failed (boom)",
    )
    assert r.status is Status.WARN
    assert "NOT exercised" in r.detail


def test_worktree_fallback_still_fails_on_blocked(monkeypatch, tmp_path):
    r = _probe(
        monkeypatch, tmp_path,
        stdout="KAGURA_EXEC_PROBE bash=blocked write=blocked\n",
        caveat="worktree creation failed (boom)",
    )
    assert r.status is Status.FAIL


def test_probe_tmp_file_is_cleaned_up(monkeypatch, tmp_path):
    _probe(monkeypatch, tmp_path, stdout=_BOTH_OK, write_file=True)
    assert not (tmp_path / _PROBE_NAME).exists()


def test_probe_tmp_file_cleaned_up_even_on_launch_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(checks.shutil, "which", lambda _: "/usr/bin/claude")
    _fix_workdir(monkeypatch, tmp_path)

    def fake_invoke(prompt, **kwargs):
        (tmp_path / _PROBE_NAME).write_text("probe")
        raise OSError("launch failed")

    monkeypatch.setattr(checks.brain_claude, "invoke", fake_invoke)
    r = checks.check_headless_exec(tmp_path)
    assert r.status is Status.FAIL
    assert not (tmp_path / _PROBE_NAME).exists()


def test_cleanup_never_touches_other_files(monkeypatch, tmp_path):
    # Regression for the fixed-filename data-loss finding: a user file that
    # happens to live in the probe dir must survive the probe untouched.
    bystander = tmp_path / ".kagura-exec-probe.tmp"  # the old fixed name
    bystander.write_text("user data")
    _probe(monkeypatch, tmp_path, stdout=_BOTH_OK, write_file=True)
    assert bystander.read_text() == "user data"


# --- ephemeral worktree (real git) ------------------------------------------


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True
    )


def test_probe_workdir_creates_and_removes_real_worktree(tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "f.txt").write_text("x")
    _git(repo, "add", "f.txt")
    _git(
        repo, "-c", "user.email=t@t", "-c", "user.name=t",
        "commit", "-qm", "seed",
    )
    with checks._probe_workdir(repo) as (workdir, caveat):
        assert caveat is None
        assert workdir != repo
        # Same layout `run` uses: sibling .kagura-runs/<repo-name>/.
        assert workdir.parent == tmp_path / ".kagura-runs" / "myrepo"
        # The repo's tracked files (e.g. .claude/settings.json in real repos)
        # are checked out into the probe context.
        assert (workdir / "f.txt").read_text() == "x"
    assert not workdir.exists()


def test_probe_workdir_falls_back_outside_a_git_repo(tmp_path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    with checks._probe_workdir(plain) as (workdir, caveat):
        assert workdir == plain
        assert caveat is not None and "worktree" in caveat


# --- registry wiring ---------------------------------------------------------


def test_run_all_excludes_probe_by_default(monkeypatch):
    called = []
    monkeypatch.setattr(
        checks, "check_headless_exec",
        lambda repo, cfg=None: called.append(repo),
    )
    results = registry.run_all(None)
    assert called == []
    assert all(r.name != "headless-exec" for r in results)


def test_run_all_includes_probe_and_forwards_cfg(monkeypatch):
    seen = {}

    def fake_check(repo, cfg=None):
        seen["cfg"] = cfg
        return CheckResult("headless-exec", Status.OK, "probed")

    monkeypatch.setattr(registry.checks, "check_headless_exec", fake_check)
    results = registry.run_all(None, exec_probe=True)
    assert results[-1].name == "headless-exec"
    assert results[-1].status is Status.OK
    assert seen["cfg"] is None  # degraded mode forwards None, not a crash


def test_run_all_probe_crash_degrades_to_fail(monkeypatch):
    def boom(repo, cfg=None):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(registry.checks, "check_headless_exec", boom)
    results = registry.run_all(None, exec_probe=True)
    assert results[-1].name == "headless-exec"
    assert results[-1].status is Status.FAIL
