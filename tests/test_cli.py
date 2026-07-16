import pytest
import yaml
from typer.testing import CliRunner

from kagura_engineer.cli import app, _written_backend_needs_creds
from kagura_engineer.config import CLOUD_REQUIRED_FIELDS
from kagura_engineer.doctor.result import CheckResult, Status
from kagura_engineer.review.envelope import ReviewEnvelope
from kagura_engineer.review.reviewer import ReviewerResult
from kagura_engineer.setup.result import SetupReport, StepResult, StepStatus

runner = CliRunner()


def _stub_setup_report(
    *, failed_count: int = 0, needs_user_count: int = 0
) -> SetupReport:
    """Build a SetupReport for the exit-code test matrix.

    `failed_count` and `needs_user_count` let each test pin which
    bucket is populated, so the exit-code decision tree can be
    exercised without bringing up a real run_plan.
    """
    return SetupReport(
        ran=[StepResult("gh", StepStatus.OK, "ok")],
        skipped=[StepResult("ollama-models", StepStatus.SKIPPED, "no models")],
        needs_user=[StepResult("claude-code", StepStatus.NEEDS_USER, "log in", "run `claude`")] * needs_user_count,
        failed=[StepResult("git", StepStatus.FAIL, "install failed", "manual fix")] * failed_count,
        duration_s=0.5,
    )


def test_help_lists_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("run", "doctor", "setup"):
        assert cmd in result.stdout


def test_version_flag_prints_version():
    from kagura_engineer import __version__

    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_doctor_json_all_ok(write_cfg, monkeypatch):
    monkeypatch.setattr(
        "kagura_engineer.cli.run_all",
        lambda cfg, **kw: [CheckResult("git", Status.OK, "ok")],
    )
    result = runner.invoke(app, ["doctor", "--config", str(write_cfg), "--json"])
    assert result.exit_code == 0
    assert '"overall": "ok"' in result.stdout


def test_doctor_prints_profile_block_above_table(write_cfg, monkeypatch):
    # issue #70: doctor answers "what would run" before "is it healthy" — the
    # resolved execution-profile block precedes the check table.
    monkeypatch.setattr(
        "kagura_engineer.cli.run_all",
        lambda cfg, **kw: [CheckResult("git", Status.OK, "ok")],
    )
    result = runner.invoke(app, ["doctor", "--config", str(write_cfg)])
    assert result.exit_code == 0
    assert "brain: claude" in result.stdout
    assert "memory: cloud" in result.stdout
    assert result.stdout.index("brain: claude") < result.stdout.index("doctor")


def test_doctor_json_carries_profile(write_cfg, monkeypatch):
    import json
    monkeypatch.setattr(
        "kagura_engineer.cli.run_all",
        lambda cfg, **kw: [CheckResult("git", Status.OK, "ok")],
    )
    result = runner.invoke(app, ["doctor", "--config", str(write_cfg), "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["profile"]["brain_backend"] == "claude"
    assert data["profile"]["memory_backend"] == "cloud"


def test_doctor_exit_1_on_fail(write_cfg, monkeypatch):
    monkeypatch.setattr(
        "kagura_engineer.cli.run_all",
        lambda cfg, **kw: [CheckResult("gh", Status.FAIL, "no auth", "gh auth login")],
    )
    result = runner.invoke(app, ["doctor", "--config", str(write_cfg), "--json"])
    assert result.exit_code == 1


# --- setup: exit code contract -----------------------------------------


def test_setup_exit_0_on_clean_run(write_cfg, monkeypatch):
    monkeypatch.setattr("kagura_engineer.cli.run_plan", lambda *a, **kw: _stub_setup_report())
    result = runner.invoke(app, ["setup", "--config", str(write_cfg)])
    assert result.exit_code == 0


def test_setup_exit_1_on_failed(write_cfg, monkeypatch):
    monkeypatch.setattr(
        "kagura_engineer.cli.run_plan",
        lambda *a, **kw: _stub_setup_report(failed_count=1),
    )
    result = runner.invoke(app, ["setup", "--config", str(write_cfg)])
    assert result.exit_code == 1


def test_setup_exit_2_on_needs_user(write_cfg, monkeypatch):
    monkeypatch.setattr(
        "kagura_engineer.cli.run_plan",
        lambda *a, **kw: _stub_setup_report(needs_user_count=1),
    )
    result = runner.invoke(app, ["setup", "--config", str(write_cfg)])
    assert result.exit_code == 2


def test_setup_exit_1_when_both_failed_and_needs_user(write_cfg, monkeypatch):
    # Plan 2 design doc §1.6 contract:
    #   0 = all OK or SKIPPED
    #   1 = 1+ step FAIL
    #   2 = 1+ step NEEDS_USER (no FAIL present)
    # When BOTH are present, FAIL wins (1), not NEEDS_USER (2).
    monkeypatch.setattr(
        "kagura_engineer.cli.run_plan",
        lambda *a, **kw: _stub_setup_report(failed_count=1, needs_user_count=1),
    )
    result = runner.invoke(app, ["setup", "--config", str(write_cfg)])
    assert result.exit_code == 1


def test_setup_json_emits_setup_report(write_cfg, monkeypatch):
    monkeypatch.setattr("kagura_engineer.cli.run_plan", lambda *a, **kw: _stub_setup_report())
    result = runner.invoke(app, ["setup", "--config", str(write_cfg), "--json"])
    assert result.exit_code == 0
    import json
    data = json.loads(result.stdout)
    assert "ran" in data
    assert "needs_user" in data
    assert "is_blocked" in data
    assert data["is_blocked"] is False


def test_setup_dry_run_propagates(write_cfg, monkeypatch):
    captured = {}

    def _spy(*a, **kw):
        captured.update(kw)
        return _stub_setup_report()

    monkeypatch.setattr("kagura_engineer.cli.run_plan", _spy)
    result = runner.invoke(app, ["setup", "--config", str(write_cfg), "--dry-run"])
    assert result.exit_code == 0
    assert captured.get("dry_run") is True


def test_setup_no_input_propagates(write_cfg, monkeypatch):
    captured = {}

    def _spy(*a, **kw):
        captured.update(kw)
        return _stub_setup_report()

    monkeypatch.setattr("kagura_engineer.cli.run_plan", _spy)
    runner.invoke(app, ["setup", "--config", str(write_cfg), "--no-input"])
    assert captured.get("no_input") is True


def test_setup_fix_filter_propagates(write_cfg, monkeypatch):
    captured = {}

    def _spy(*a, **kw):
        captured.update(kw)
        return _stub_setup_report()

    monkeypatch.setattr("kagura_engineer.cli.run_plan", _spy)
    runner.invoke(app, ["setup", "--config", str(write_cfg), "--fix", "gh"])
    assert captured.get("only") == "gh"


def test_setup_full_propagates(write_cfg, monkeypatch):
    # `--full` opts the memory-mcp step into installing SDK hooks + skills;
    # default (flag absent) must stay `.mcp.json`-only (full=False).
    captured = {}

    def _spy(*a, **kw):
        captured.update(kw)
        return _stub_setup_report()

    monkeypatch.setattr("kagura_engineer.cli.run_plan", _spy)
    runner.invoke(app, ["setup", "--config", str(write_cfg), "--full"])
    assert captured.get("full") is True

    captured.clear()
    runner.invoke(app, ["setup", "--config", str(write_cfg)])
    assert captured.get("full") is False


def test_setup_unknown_fix_is_clean_error(write_cfg, monkeypatch):
    # An unknown --fix name is a user error, not a silent no-op.
    # The CLI must catch this BEFORE calling run_plan so the user
    # gets actionable feedback (otherwise build_plan returns [] and
    # the report looks like a clean run -> misleading).
    from kagura_engineer.setup import build_plan
    from kagura_engineer.cli import _check_fix_name  # helper exposed for test

    # Pass the FILTERED plan to the helper; if the filter returned [],
    # that's exactly the case the helper is meant to flag.
    assert _check_fix_name("bogus", build_plan(only="bogus")) is not None
    assert _check_fix_name(None, build_plan()) is None
    assert _check_fix_name("gh", build_plan(only="gh")) is None


# --- setup: first-install UX on missing/invalid config (issue #71) ----------
# setup is the only command a fresh checkout needs: a missing repo.yaml is
# auto-scaffolded, then setup runs a degraded plan (config NEEDS_USER + the
# config-free steps) instead of refusing.


def _spy_run_plan(captured):
    """A run_plan stub that records (cfg, config_step) and routes the config
    step into its real status bucket so the exit-code path is exercised."""
    def _spy(cfg, *, no_input, dry_run, only=None, full=False, config_step=None):
        captured["cfg"] = cfg
        captured["config_step"] = config_step
        failed = [config_step] if (config_step and config_step.status is StepStatus.FAIL) else []
        needs_user = [config_step] if (config_step and config_step.status is StepStatus.NEEDS_USER) else []
        return SetupReport(failed=failed, needs_user=needs_user, duration_s=0.0)
    return _spy


def test_setup_missing_config_auto_scaffolds_and_degrades(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr("kagura_engineer.cli.run_plan", _spy_run_plan(captured))
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["setup"])  # default --config repo.yaml
    # The fresh checkout's repo.yaml was scaffolded (same as `init`).
    assert (tmp_path / "repo.yaml").is_file()
    assert "scaffolding" in result.output.lower()
    # Degraded mode: run_plan called with no config + a synthetic config step.
    assert captured["cfg"] is None
    assert captured["config_step"] is not None
    assert captured["config_step"].status is StepStatus.NEEDS_USER
    # NEEDS_USER (no FAIL) → exit 2, with a table naming the next action.
    assert result.exit_code == 2
    assert "config" in result.output.lower()
    # The freshly-scaffolded template's blockers are its blank cloud creds —
    # the hint must point at them.
    assert "cloud credentials" in captured["config_step"].fix_hint


def test_setup_invalid_config_degrades_without_scaffold(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr("kagura_engineer.cli.run_plan", _spy_run_plan(captured))
    bad = tmp_path / "repo.yaml"
    bad.write_text("profile: coding\n")  # present but blank creds → invalid
    result = runner.invoke(app, ["setup", "--config", str(bad)])
    # An existing (invalid) file is not re-scaffolded.
    assert "scaffolding" not in result.output.lower()
    assert captured["cfg"] is None
    assert captured["config_step"] is not None
    assert result.exit_code == 2
    assert "config" in result.output.lower()


def test_setup_invalid_config_hint_says_fix_not_creds(tmp_path, monkeypatch):
    # An existing-but-invalid repo.yaml is not the blank-creds template: the
    # failure may be a syntax error or any validation problem, so the hint must
    # say "fix repo.yaml" (mirroring doctor's wording), not point at cloud
    # credentials the user may already have filled in.
    captured = {}
    monkeypatch.setattr("kagura_engineer.cli.run_plan", _spy_run_plan(captured))
    bad = tmp_path / "repo.yaml"
    bad.write_text("repo: [broken\n")  # YAML syntax error, unrelated to creds
    runner.invoke(app, ["setup", "--config", str(bad)])
    hint = captured["config_step"].fix_hint
    assert "fix repo.yaml" in hint
    assert "cloud credentials" not in hint


def test_setup_dry_run_suppresses_scaffold(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr("kagura_engineer.cli.run_plan", _spy_run_plan(captured))
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["setup", "--dry-run"])
    # Preview must not write: no repo.yaml created under --dry-run.
    assert not (tmp_path / "repo.yaml").exists()
    assert captured["cfg"] is None  # still degraded, just no scaffold
    assert captured["config_step"] is not None
    assert result.exit_code == 2


def test_setup_scaffold_failure_is_config_fail_row(tmp_path, monkeypatch):
    # An unwritable dir must surface as a `config` FAIL row, never a traceback.
    captured = {}
    monkeypatch.setattr("kagura_engineer.cli.run_plan", _spy_run_plan(captured))

    def _boom(*a, **k):
        raise OSError("read-only file system")

    monkeypatch.setattr("kagura_engineer.cli.scaffold", _boom)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["setup"])
    # The OSError was caught (only the clean typer.Exit/SystemExit remains).
    assert not isinstance(result.exception, OSError)
    assert captured["config_step"] is not None
    assert captured["config_step"].status is StepStatus.FAIL
    assert result.exit_code == 1  # FAIL present
    assert "config" in result.output.lower()


def test_setup_valid_config_does_not_scaffold(write_cfg, monkeypatch):
    # Regression pin: a valid config takes the unchanged path — no scaffold,
    # run_plan called with the real Config (cfg is not None).
    captured = {}
    monkeypatch.setattr("kagura_engineer.cli.run_plan", _spy_run_plan(captured))
    result = runner.invoke(app, ["setup", "--config", str(write_cfg)])
    assert "scaffolding" not in result.output.lower()
    assert captured["cfg"] is not None
    assert captured["config_step"] is None
    assert result.exit_code == 0


def _stub_run_report(status):
    from kagura_engineer.run.result import PhaseResult, RunReport, RunStatus
    return RunReport(
        issue=42,
        phases=[PhaseResult("guard", status, "x")],
        pr_url="https://x/pull/1" if status is RunStatus.OK else None,
        resume_hint=None if status is RunStatus.OK else "re-run",
    )


def test_run_exit_0_on_ok(write_cfg, monkeypatch):
    from kagura_engineer.run.result import RunStatus
    monkeypatch.setattr("kagura_engineer.cli.run_idea", lambda *a, **kw: _stub_run_report(RunStatus.OK))
    result = runner.invoke(app, ["run", "42", "--config", str(write_cfg)])
    assert result.exit_code == 0


def test_run_exit_1_on_fail(write_cfg, monkeypatch):
    from kagura_engineer.run.result import RunStatus
    monkeypatch.setattr("kagura_engineer.cli.run_idea", lambda *a, **kw: _stub_run_report(RunStatus.FAIL))
    result = runner.invoke(app, ["run", "42", "--config", str(write_cfg)])
    assert result.exit_code == 1


def test_run_exit_2_on_blocked(write_cfg, monkeypatch):
    from kagura_engineer.run.result import RunStatus
    monkeypatch.setattr("kagura_engineer.cli.run_idea", lambda *a, **kw: _stub_run_report(RunStatus.BLOCKED))
    result = runner.invoke(app, ["run", "42", "--config", str(write_cfg)])
    assert result.exit_code == 2


def test_run_json_emits_report(write_cfg, monkeypatch):
    import json
    from kagura_engineer.run.result import RunStatus
    monkeypatch.setattr("kagura_engineer.cli.run_idea", lambda *a, **kw: _stub_run_report(RunStatus.OK))
    result = runner.invoke(app, ["run", "42", "--config", str(write_cfg), "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["issue"] == 42 and data["status"] == "ok"


def test_run_no_remember_propagates(write_cfg, monkeypatch):
    from kagura_engineer.run.result import RunStatus
    captured = {}

    def _spy(cfg, issue, **kw):
        captured.update(kw); captured["issue"] = issue
        return _stub_run_report(RunStatus.OK)

    monkeypatch.setattr("kagura_engineer.cli.run_idea", _spy)
    runner.invoke(app, ["run", "7", "--config", str(write_cfg), "--no-remember"])
    assert captured["issue"] == 7
    assert captured.get("no_remember") is True


def test_run_streams_progress_to_stdout(write_cfg, monkeypatch):
    # issue #12: the run prints incremental phase progress (the sink it passes
    # to run_idea) to stdout, before the final table.
    from kagura_engineer.run.result import RunStatus

    def _fake(cfg, issue, *, progress=None, **kw):
        if progress is not None:
            progress("▶ start …")
        return _stub_run_report(RunStatus.OK)

    monkeypatch.setattr("kagura_engineer.cli.run_idea", _fake)
    result = runner.invoke(app, ["run", "42", "--config", str(write_cfg)])
    assert result.exit_code == 0
    assert "▶ start" in result.stdout


def test_run_json_does_not_stream_progress(write_cfg, monkeypatch):
    # In --json mode the progress sink must stay silent so stdout is clean JSON.
    import json
    from kagura_engineer.run.result import RunStatus

    def _fake(cfg, issue, *, progress=None, **kw):
        if progress is not None:
            progress("▶ start …")  # would corrupt the JSON if printed
        return _stub_run_report(RunStatus.OK)

    monkeypatch.setattr("kagura_engineer.cli.run_idea", _fake)
    result = runner.invoke(app, ["run", "42", "--config", str(write_cfg), "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)  # parses → no progress leaked into stdout
    assert data["issue"] == 42


def test_run_missing_config_clean_error(tmp_path):
    missing = tmp_path / "nope.yaml"
    result = runner.invoke(app, ["run", "42", "--config", str(missing)])
    assert result.exit_code == 2
    assert "config" in result.output.lower()


# --- doctor: degraded report on missing/invalid config (issue #71) ---------
# A fresh checkout has no repo.yaml. doctor must print a degraded table —
# a synthetic `config` FAIL row plus the config-free checks — and exit 1
# (non-zero when unhealthy), instead of the old one-line exit-2 refusal.


def test_doctor_missing_config_degraded_report(tmp_path, monkeypatch):
    # Stub run_all so the config-free checks don't shell out; we only assert
    # the synthetic config row + that run_all was invoked with None.
    seen = {}

    def _spy(cfg, **kw):
        seen["cfg"] = cfg
        return [CheckResult("git", Status.OK, "ok")]

    monkeypatch.setattr("kagura_engineer.cli.run_all", _spy)
    missing = tmp_path / "nope.yaml"
    result = runner.invoke(app, ["doctor", "--config", str(missing)])
    assert result.exit_code == 1  # FAIL present, not the old exit-2 refusal
    assert "config" in result.output.lower()
    assert seen["cfg"] is None  # config-free checks ran in degraded mode


def test_doctor_invalid_config_degraded_report(tmp_path, monkeypatch):
    monkeypatch.setattr("kagura_engineer.cli.run_all", lambda cfg, **kw: [])
    bad = tmp_path / "repo.yaml"
    bad.write_text("profile: coding\n")  # cloud backend, blank creds → invalid
    result = runner.invoke(app, ["doctor", "--config", str(bad)])
    assert result.exit_code == 1
    assert "config" in result.output.lower()


def test_doctor_malformed_yaml_degraded_report(tmp_path, monkeypatch):
    monkeypatch.setattr("kagura_engineer.cli.run_all", lambda cfg, **kw: [])
    bad = tmp_path / "repo.yaml"
    bad.write_text("profile: coding\n\tbad: tab\n")
    result = runner.invoke(app, ["doctor", "--config", str(bad)])
    assert result.exit_code == 1
    assert "config" in result.output.lower() or "yaml" in result.output.lower()


def test_doctor_degraded_json_has_config_check_object(tmp_path, monkeypatch):
    # The synthetic config row must appear as a normal check object so the
    # --json schema is unchanged (just one extra check).
    import json

    monkeypatch.setattr(
        "kagura_engineer.cli.run_all",
        lambda cfg, **kw: [CheckResult("git", Status.OK, "ok")],
    )
    missing = tmp_path / "nope.yaml"
    result = runner.invoke(app, ["doctor", "--config", str(missing), "--json"])
    assert result.exit_code == 1
    data = json.loads(result.stdout)
    assert data["overall"] == "fail"
    config_rows = [c for c in data["checks"] if c["name"] == "config"]
    assert len(config_rows) == 1
    row = config_rows[0]
    assert row["status"] == "fail"
    assert set(row) == {"name", "status", "detail", "fix_hint"}
    # The config row is first (the headline problem on a fresh checkout).
    assert data["checks"][0]["name"] == "config"


# --- review: exit code + JSON contract -------------------------------------


def _write_cfg_review(tmp_path):
    cfg = tmp_path / "repo.yaml"
    cfg.write_text(
        "profile: test\n"
        "memory_cloud_url: http://x\n"
        "workspace_id: w\n"
        "context_id: c\n"
    )
    return cfg


def test_review_green_exits_0(monkeypatch, tmp_path):
    import kagura_engineer.review as pkg
    monkeypatch.setattr(pkg, "resolve_head", lambda t: t, raising=True)
    monkeypatch.setattr(
        pkg, "run_reviewer",
        lambda **kw: ReviewerResult(0, "", "", ReviewEnvelope(parsed=True, verdict="green")),
        raising=True,
    )

    class _Mem:
        def load_pinned(self, c): return []
        def recall(self, c, q, *, k=5): return []
    monkeypatch.setattr(pkg, "resolve_memory_client", lambda cfg: _Mem(), raising=True)

    cfg = _write_cfg_review(tmp_path)
    result = runner.invoke(app, ["review", "HEAD", "-c", str(cfg)])
    assert result.exit_code == 0


def test_review_red_exits_2(monkeypatch, tmp_path):
    import kagura_engineer.review as pkg
    monkeypatch.setattr(pkg, "resolve_head", lambda t: t, raising=True)
    env = ReviewEnvelope(parsed=True, verdict="red", summary={"blocking": 1})
    monkeypatch.setattr(pkg, "run_reviewer", lambda **kw: ReviewerResult(1, "", "", env), raising=True)

    class _Mem:
        def load_pinned(self, c): return []
        def recall(self, c, q, *, k=5): return []
    monkeypatch.setattr(pkg, "resolve_memory_client", lambda cfg: _Mem(), raising=True)

    cfg = _write_cfg_review(tmp_path)
    result = runner.invoke(app, ["review", "HEAD", "-c", str(cfg)])
    assert result.exit_code == 2


def test_review_bad_config_exits_2(tmp_path):
    result = runner.invoke(app, ["review", "HEAD", "-c", str(tmp_path / "nope.yaml")])
    assert result.exit_code == 2


def test_review_json_flag_emits_json(monkeypatch, tmp_path):
    import json as _json
    import kagura_engineer.review as pkg
    monkeypatch.setattr(pkg, "resolve_head", lambda t: t, raising=True)
    monkeypatch.setattr(
        pkg, "run_reviewer",
        lambda **kw: ReviewerResult(0, "", "", ReviewEnvelope(parsed=True, verdict="green")),
        raising=True,
    )

    class _Mem:
        def load_pinned(self, c): return []
        def recall(self, c, q, *, k=5): return []
    monkeypatch.setattr(pkg, "resolve_memory_client", lambda cfg: _Mem(), raising=True)

    cfg = _write_cfg_review(tmp_path)
    result = runner.invoke(app, ["review", "HEAD", "-c", str(cfg), "--json"])
    assert result.exit_code == 0
    assert _json.loads(result.stdout)["verdict"] == "green"


# --- review --fix: Plan 4b auto-fix loop -----------------------------------


def _patch_loop_mem(monkeypatch):
    import kagura_engineer.review.loop as lp

    class _Mem:
        def load_pinned(self, c): return []
        def recall(self, c, q, *, k=5): return []
    monkeypatch.setattr(lp, "resolve_memory_client", lambda cfg: _Mem(), raising=True)


def _loop_review(monkeypatch, statuses):
    import kagura_engineer.review.loop as lp
    from kagura_engineer.review.result import ReviewReport
    it = iter(statuses)
    monkeypatch.setattr(
        lp, "review_pr",
        lambda *a, **kw: ReviewReport(target="HEAD", base="main", status=next(it),
                                      verdict="red", report_path="/tmp/r.json"),
        raising=True,
    )


def test_review_fix_green_after_fix_exits_0(monkeypatch, tmp_path):
    import kagura_engineer.review.loop as lp
    from kagura_engineer.review.fixer import FixerResult
    from kagura_engineer.review.result import ReviewStatus
    _patch_loop_mem(monkeypatch)
    _loop_review(monkeypatch, [ReviewStatus.BLOCKED, ReviewStatus.OK])
    monkeypatch.setattr(lp, "run_fixer", lambda repo, prompt, **kw: FixerResult(0, "fixed", ""), raising=True)
    cfg = _write_cfg_review(tmp_path)
    result = runner.invoke(app, ["review", "HEAD", "-c", str(cfg), "--fix"])
    assert result.exit_code == 0


def test_review_fix_still_red_exits_2(monkeypatch, tmp_path):
    import kagura_engineer.review.loop as lp
    from kagura_engineer.review.fixer import FixerResult
    from kagura_engineer.review.result import ReviewStatus
    _patch_loop_mem(monkeypatch)
    _loop_review(monkeypatch, [ReviewStatus.BLOCKED] * 10)
    monkeypatch.setattr(lp, "run_fixer", lambda repo, prompt, **kw: FixerResult(0, "", ""), raising=True)
    cfg = _write_cfg_review(tmp_path)
    result = runner.invoke(app, ["review", "HEAD", "-c", str(cfg), "--fix"])
    assert result.exit_code == 2


def test_review_fix_json_emits_iterations(monkeypatch, tmp_path):
    import json as _json
    import kagura_engineer.review.loop as lp
    from kagura_engineer.review.fixer import FixerResult
    from kagura_engineer.review.result import ReviewStatus
    _patch_loop_mem(monkeypatch)
    _loop_review(monkeypatch, [ReviewStatus.BLOCKED, ReviewStatus.OK])
    monkeypatch.setattr(lp, "run_fixer", lambda repo, prompt, **kw: FixerResult(0, "", ""), raising=True)
    cfg = _write_cfg_review(tmp_path)
    result = runner.invoke(app, ["review", "HEAD", "-c", str(cfg), "--fix", "--json"])
    assert result.exit_code == 0
    data = _json.loads(result.stdout)
    assert data["fixes_attempted"] == 1
    assert len(data["iterations"]) == 2


def test_review_fix_fixer_failure_exits_1(monkeypatch, tmp_path):
    import kagura_engineer.review.loop as lp
    from kagura_engineer.review.fixer import FixerResult
    from kagura_engineer.review.result import ReviewStatus
    _patch_loop_mem(monkeypatch)
    _loop_review(monkeypatch, [ReviewStatus.BLOCKED])
    monkeypatch.setattr(lp, "run_fixer", lambda repo, prompt, **kw: FixerResult(1, "", "boom"), raising=True)
    cfg = _write_cfg_review(tmp_path)
    result = runner.invoke(app, ["review", "HEAD", "-c", str(cfg), "--fix"])
    assert result.exit_code == 1


def test_run_unattended_flag_threads(monkeypatch, tmp_path):
    import kagura_engineer.cli as cli
    from kagura_engineer.run.result import RunReport
    seen = {}
    monkeypatch.setattr(cli, "run_idea",
                        lambda cfg, issue, **kw: (seen.update(kw) or RunReport(issue=issue)), raising=True)
    cfg = _write_cfg_review(tmp_path)
    r = runner.invoke(app, ["run", "5", "-c", str(cfg), "--unattended"])
    assert r.exit_code == 0
    assert seen.get("unattended") is True


# --- issue #70: execution-profile startup headers + JSON "profile" ----------


def test_run_prints_profile_header(write_cfg, monkeypatch):
    from kagura_engineer.run.result import RunStatus
    monkeypatch.setattr("kagura_engineer.cli.run_idea",
                        lambda *a, **kw: _stub_run_report(RunStatus.OK))
    result = runner.invoke(app, ["run", "42", "--config", str(write_cfg)])
    assert result.exit_code == 0
    assert "brain: claude" in result.stdout
    assert "memory: cloud" in result.stdout


def test_run_json_carries_profile_without_header(write_cfg, monkeypatch):
    import json
    from kagura_engineer.run.result import RunStatus
    monkeypatch.setattr("kagura_engineer.cli.run_idea",
                        lambda *a, **kw: _stub_run_report(RunStatus.OK))
    result = runner.invoke(app, ["run", "42", "--config", str(write_cfg), "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)  # parses → no header lines leaked
    assert data["profile"]["brain_backend"] == "claude"
    assert data["profile"]["context_id"]


def _patch_review_pr_green(monkeypatch):
    import kagura_engineer.cli as cli
    from kagura_engineer.review.result import ReviewReport, ReviewStatus
    monkeypatch.setattr(
        cli, "review_pr",
        lambda cfg, target, **kw: ReviewReport(target=target, base="main",
                                               status=ReviewStatus.OK, verdict="green"),
        raising=True,
    )


def test_review_header_omits_brain_line_without_fix(monkeypatch, tmp_path):
    # Plain `review` runs no brain — the header must not imply one.
    _patch_review_pr_green(monkeypatch)
    cfg = _write_cfg_review(tmp_path)
    result = runner.invoke(app, ["review", "HEAD", "-c", str(cfg)])
    assert result.exit_code == 0
    assert "reviewer:" in result.stdout
    assert "memory: cloud" in result.stdout
    assert "brain:" not in result.stdout


def test_review_fix_header_includes_brain_line(monkeypatch, tmp_path):
    import kagura_engineer.cli as cli
    from kagura_engineer.review.result import ReviewLoopReport, ReviewStatus
    monkeypatch.setattr(
        cli, "review_fix_loop",
        lambda cfg, target, **kw: ReviewLoopReport(target=target, base="main",
                                                   status=ReviewStatus.OK),
        raising=True,
    )
    cfg = _write_cfg_review(tmp_path)
    result = runner.invoke(app, ["review", "HEAD", "-c", str(cfg), "--fix"])
    assert result.exit_code == 0
    assert "brain: claude" in result.stdout


def test_review_json_carries_profile(monkeypatch, tmp_path):
    import json as _json
    _patch_review_pr_green(monkeypatch)
    cfg = _write_cfg_review(tmp_path)
    result = runner.invoke(app, ["review", "HEAD", "-c", str(cfg), "--json"])
    assert result.exit_code == 0
    data = _json.loads(result.stdout)
    assert data["profile"]["brain_backend"] == "claude"


def test_review_fix_json_carries_profile(monkeypatch, tmp_path):
    import json as _json
    import kagura_engineer.cli as cli
    from kagura_engineer.review.result import ReviewLoopReport, ReviewStatus
    monkeypatch.setattr(
        cli, "review_fix_loop",
        lambda cfg, target, **kw: ReviewLoopReport(target=target, base="main",
                                                   status=ReviewStatus.OK),
        raising=True,
    )
    cfg = _write_cfg_review(tmp_path)
    result = runner.invoke(app, ["review", "HEAD", "-c", str(cfg), "--fix", "--json"])
    assert result.exit_code == 0
    data = _json.loads(result.stdout)
    assert data["profile"]["brain_backend"] == "claude"


def _stub_eval_report():
    from kagura_engineer.eval.result import EvalReport
    return EvalReport(issues=[1], grounded_runs=[], control_runs=[])


def test_eval_prints_profile_header(monkeypatch, tmp_path):
    import kagura_engineer.cli as cli
    monkeypatch.setattr(cli, "run_ab_eval",
                        lambda *a, **kw: _stub_eval_report(), raising=True)
    cfg = _write_cfg_review(tmp_path)
    result = runner.invoke(app, ["eval", "1", "-c", str(cfg)])
    assert result.exit_code == 0
    assert "brain: claude" in result.stdout


def test_eval_json_carries_profile(monkeypatch, tmp_path):
    import json as _json
    import kagura_engineer.cli as cli
    monkeypatch.setattr(cli, "run_ab_eval",
                        lambda *a, **kw: _stub_eval_report(), raising=True)
    cfg = _write_cfg_review(tmp_path)
    result = runner.invoke(app, ["eval", "1", "-c", str(cfg), "--json"])
    assert result.exit_code == 0
    data = _json.loads(result.stdout)
    assert data["profile"]["brain_backend"] == "claude"


# --- init: scaffold repo.yaml + .gitignore (issue #35) ------------------------


def test_init_listed_in_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "init" in result.stdout


def test_init_nonexistent_dir_clean_error(tmp_path):
    # code-review #3: init --dir to a missing directory must give a clean error
    # (exit 2), not a raw FileNotFoundError traceback.
    missing = tmp_path / "nope"
    result = runner.invoke(app, ["init", "--dir", str(missing)])
    assert result.exit_code == 2
    assert not (missing / "repo.yaml").exists()


def test_init_scaffolds_repo_yaml_and_gitignore(tmp_path):
    result = runner.invoke(app, ["init", "--dir", str(tmp_path)])
    assert result.exit_code == 0
    assert (tmp_path / "repo.yaml").is_file()
    assert "repo.yaml" in (tmp_path / ".gitignore").read_text()


def test_init_is_idempotent_and_never_overwrites(tmp_path):
    (tmp_path / "repo.yaml").write_text("profile: mine\nmemory_backend: local\n")
    result = runner.invoke(app, ["init", "--dir", str(tmp_path)])
    assert result.exit_code == 0
    # existing repo.yaml is preserved verbatim
    assert (tmp_path / "repo.yaml").read_text() == "profile: mine\nmemory_backend: local\n"


def test_init_prints_cloud_creds_affordance_on_fresh_scaffold(tmp_path):
    # issue #43 item 2: the shipped template uses memory_backend: cloud with
    # empty creds, so it fails validation unedited. init must tell the user the
    # file won't validate until they fill the cloud credentials, instead of a
    # generic "edit then run setup" that hides the next required step.
    result = runner.invoke(app, ["init", "--dir", str(tmp_path)])
    assert result.exit_code == 0
    out = result.stdout.lower()
    assert "credential" in out
    assert "validate" in out
    # The hint names the cloud fields from the shared SSOT, not hand-typed prose,
    # so a future required field is listed automatically (issue #43, /code-review).
    for field in CLOUD_REQUIRED_FIELDS:
        assert field in result.stdout


def test_init_no_cloud_affordance_when_repo_yaml_exists(tmp_path):
    # When repo.yaml already exists, init left it unchanged — it must not claim
    # the file "won't validate" (the existing file may be a finished config).
    (tmp_path / "repo.yaml").write_text("profile: mine\nmemory_backend: local\n")
    result = runner.invoke(app, ["init", "--dir", str(tmp_path)])
    assert result.exit_code == 0
    assert "won't validate" not in result.stdout.lower()


def _cloud_yaml(tmp_path, **overrides):
    data = {
        "profile": "dev",
        "memory_backend": "cloud",
        "memory_cloud_url": "https://m",
        "workspace_id": "w",
        "context_id": "c",
    }
    data.update(overrides)
    p = tmp_path / "repo.yaml"
    p.write_text(yaml.safe_dump(data))
    return p


@pytest.mark.parametrize("blank", CLOUD_REQUIRED_FIELDS)
def test_written_backend_needs_creds_true_when_any_cloud_field_blank(tmp_path, blank):
    # issue #43: the affordance helper iterates the shared CLOUD_REQUIRED_FIELDS
    # constant (not a local hardcoded tuple), so a missing field in any of them
    # triggers the hint — and a future field added to the constant is covered.
    p = _cloud_yaml(tmp_path, **{blank: ""})
    assert _written_backend_needs_creds(p) is True


def test_written_backend_needs_creds_false_when_all_cloud_fields_filled(tmp_path):
    p = _cloud_yaml(tmp_path)
    assert _written_backend_needs_creds(p) is False


def test_written_backend_needs_creds_false_for_local_backend(tmp_path):
    p = tmp_path / "repo.yaml"
    p.write_text("profile: dev\nmemory_backend: local\n")
    assert _written_backend_needs_creds(p) is False
