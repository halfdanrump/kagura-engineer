"""check_headless_exec — the opt-in live probe that a headless `claude -p`
can actually act in the target repo (issue #93).

A headless run has no human to answer Claude Code's permission prompts, so a
repo that passes every static doctor check can still red-halt at runtime on
(1) a missing Bash allowlist, (2) an untrusted workspace, or (3) missing
Edit/Write grants. The probe asks a headless claude to run one command and
write one temp file, then reports exactly which capability is blocked.
"""
from kagura_brain.core import BrainResult

from kagura_engineer.doctor import checks, registry
from kagura_engineer.doctor.result import Status


def _result(returncode=0, stdout="", stderr="", timed_out=False):
    return BrainResult(
        returncode=returncode, stdout=stdout, stderr=stderr, timed_out=timed_out
    )


def _probe(monkeypatch, tmp_path, *, stdout="", returncode=0, timed_out=False):
    monkeypatch.setattr(checks.shutil, "which", lambda _: "/usr/bin/claude")

    def fake_invoke(prompt, **kwargs):
        return _result(returncode, stdout, timed_out=timed_out)

    monkeypatch.setattr(checks.brain_claude, "invoke", fake_invoke)
    return checks.check_headless_exec(tmp_path)


def test_fail_when_claude_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(checks.shutil, "which", lambda _: None)
    r = checks.check_headless_exec(tmp_path)
    assert r.status is Status.FAIL
    assert "claude" in r.detail


def test_ok_when_both_capabilities_pass(monkeypatch, tmp_path):
    r = _probe(
        monkeypatch, tmp_path,
        stdout="some preamble\nKAGURA_EXEC_PROBE bash=ok write=ok\n",
    )
    assert r.status is Status.OK
    assert "commands" in r.detail and "file" in r.detail


def test_fail_when_bash_blocked(monkeypatch, tmp_path):
    r = _probe(
        monkeypatch, tmp_path,
        stdout="KAGURA_EXEC_PROBE bash=blocked write=ok\n",
    )
    assert r.status is Status.FAIL
    assert "commands" in r.detail
    assert ".claude/settings.json" in r.fix_hint
    assert "trust" in r.fix_hint


def test_fail_when_write_blocked(monkeypatch, tmp_path):
    # The late-failing wall: Bash allowlisted but Edit/Write not granted —
    # start passes, implement red-halts. The probe must catch it pre-flight.
    r = _probe(
        monkeypatch, tmp_path,
        stdout="KAGURA_EXEC_PROBE bash=ok write=blocked\n",
    )
    assert r.status is Status.FAIL
    assert "file" in r.detail
    assert ".claude/settings.json" in r.fix_hint


def test_last_marker_wins(monkeypatch, tmp_path):
    # The probe prompt is echoed in some transcripts; only the LAST marker
    # line is the probe's answer (mirrors the run gate's trailing-marker rule).
    r = _probe(
        monkeypatch, tmp_path,
        stdout=(
            "KAGURA_EXEC_PROBE bash=<ok|blocked> write=<ok|blocked>\n"
            "doing things...\n"
            "KAGURA_EXEC_PROBE bash=ok write=ok\n"
        ),
    )
    assert r.status is Status.OK


def test_warn_when_no_marker(monkeypatch, tmp_path):
    r = _probe(monkeypatch, tmp_path, stdout="I did some things but forgot.\n")
    assert r.status is Status.WARN
    assert "marker" in r.detail


def test_fail_when_claude_exits_nonzero(monkeypatch, tmp_path):
    r = _probe(monkeypatch, tmp_path, stdout="", returncode=1)
    assert r.status is Status.FAIL
    assert "exited 1" in r.detail


def test_fail_on_timeout(monkeypatch, tmp_path):
    # kagura_brain reports a timeout as a result flag, not an exception.
    r = _probe(monkeypatch, tmp_path, returncode=1, timed_out=True)
    assert r.status is Status.FAIL
    assert "timed out" in r.detail


def test_probe_tmp_file_is_cleaned_up(monkeypatch, tmp_path):
    leftover = tmp_path / ".kagura-exec-probe.tmp"

    monkeypatch.setattr(checks.shutil, "which", lambda _: "/usr/bin/claude")

    def fake_invoke(prompt, **kwargs):
        leftover.write_text("probe")
        return _result(0, "KAGURA_EXEC_PROBE bash=ok write=ok\n")

    monkeypatch.setattr(checks.brain_claude, "invoke", fake_invoke)
    r = checks.check_headless_exec(tmp_path)
    assert r.status is Status.OK
    assert not leftover.exists()


def test_probe_tmp_file_cleaned_up_even_on_launch_failure(monkeypatch, tmp_path):
    leftover = tmp_path / ".kagura-exec-probe.tmp"

    monkeypatch.setattr(checks.shutil, "which", lambda _: "/usr/bin/claude")

    def fake_invoke(prompt, **kwargs):
        leftover.write_text("probe")
        raise OSError("launch failed")

    monkeypatch.setattr(checks.brain_claude, "invoke", fake_invoke)
    r = checks.check_headless_exec(tmp_path)
    assert r.status is Status.FAIL
    assert not leftover.exists()


# --- registry wiring -------------------------------------------------------


def test_run_all_excludes_probe_by_default(monkeypatch):
    called = []
    monkeypatch.setattr(
        checks, "check_headless_exec",
        lambda repo: called.append(repo),
    )
    results = registry.run_all(None)
    assert called == []
    assert all(r.name != "headless-exec" for r in results)


def test_run_all_includes_probe_when_opted_in(monkeypatch):
    from kagura_engineer.doctor.result import CheckResult

    monkeypatch.setattr(
        registry.checks, "check_headless_exec",
        lambda repo: CheckResult("headless-exec", Status.OK, "probed"),
    )
    results = registry.run_all(None, exec_probe=True)
    assert results[-1].name == "headless-exec"
    assert results[-1].status is Status.OK


def test_run_all_probe_crash_degrades_to_fail(monkeypatch):
    def boom(repo):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(registry.checks, "check_headless_exec", boom)
    results = registry.run_all(None, exec_probe=True)
    assert results[-1].name == "headless-exec"
    assert results[-1].status is Status.FAIL
