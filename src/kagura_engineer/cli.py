from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path

import typer
import yaml

from . import __version__
from .config import (
    CLOUD_REQUIRED_FIELDS,
    ConfigError,
    ConfigLoad,
    load_config,
    load_config_lenient,
)
from .profile import render_lines as profile_lines
from .profile import resolve_profile
from .profile import to_dict as profile_dict
from .doctor.registry import overall_status, run_all
from .eval import ReviewFn, run_ab_eval
from .eval.render import print_table as eval_print_table
from .eval.render import to_json as eval_to_json
from .doctor.render import print_table, to_json
from .doctor.result import CheckResult, Status
from .run import STATUS_EXIT, run_idea
from .run.render import print_table as run_print_table
from .run.render import to_json as run_to_json
from .review import REVIEW_STATUS_EXIT, review_pr
from .review.loop import review_fix_loop
from .review.render import loop_to_json as review_loop_to_json
from .review.render import print_loop_table as review_print_loop_table
from .review.render import print_table as review_print_table
from .review.render import to_json as review_to_json
from .setup import STEP_NAMES, build_plan, run_plan
from .setup.render import print_table as setup_print_table
from .setup.render import to_json as setup_to_json
from .setup.result import StepResult, StepStatus
from .setup.scaffold import scaffold

app = typer.Typer(help="Autonomous coding harness over Claude Code + Kagura Memory.")

_CONFIG_OPT = typer.Option("repo.yaml", "--config", "-c", help="path to repo.yaml")


def _echo_profile(prof, json_out: bool, *, brain: bool = True) -> None:
    """issue #70: print the execution-profile header (suppressed under --json
    so stdout stays a single valid JSON document — the issue #12 rule)."""
    if json_out:
        return
    for line in profile_lines(prof, brain=brain):
        typer.echo(line)


def _configure_stdio_utf8() -> None:
    """Force UTF-8 on stdout/stderr so the `--json` contract (and em-dash / 日本語
    fix-hints) survive a cp932 console (issue #78). Without this,
    `typer.echo(to_json(...))` raises UnicodeEncodeError on a Japanese Windows
    console even though the checks ran. Best-effort: a stream that lacks
    `reconfigure` (already wrapped, or redirected to a plain buffer) is left as-is.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (OSError, ValueError):  # pragma: no cover - stream not reconfigurable
            pass


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def _main(
    version: bool = typer.Option(
        False,
        "--version",
        help="Show the kagura-engineer version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """Autonomous coding harness over Claude Code + Kagura Memory."""
    # issue #78: pin UTF-8 on stdio before any command writes JSON / fix-hints,
    # so a cp932 console can't turn an em-dash into a UnicodeEncodeError traceback.
    _configure_stdio_utf8()


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def _degraded_config_check(load: ConfigLoad) -> CheckResult:
    """Synthetic `config` FAIL row for doctor's degraded report (issue #71).

    On a missing/invalid config doctor no longer refuses; it prepends this row
    (the headline problem on a fresh checkout) and then runs the config-free
    checks. The fix hint points at `setup`, the one command a fresh checkout
    needs to type first.
    """
    hint = (
        "run `kagura-engineer setup` to scaffold repo.yaml and bootstrap"
        if load.missing
        else "fix repo.yaml (or run `kagura-engineer setup`)"
    )
    return CheckResult("config", Status.FAIL, load.error or "config unavailable", hint)


@app.command()
def doctor(
    config: str = _CONFIG_OPT, json_out: bool = typer.Option(False, "--json")
) -> None:
    """Check the dependency chain.

    On a missing/invalid config (a fresh checkout) doctor degrades instead of
    refusing: a synthetic `config` FAIL row plus the config-free checks, exiting
    non-zero (1) so "unhealthy → non-zero" is preserved (issue #71). With a
    valid config it also resolves and prints the execution profile up-front, so
    doctor answers "what would run" before "is it healthy" (issue #70).
    """
    load = load_config_lenient(config)
    prof = None
    if load.cfg is None:
        # Degraded report: headline config row first, then config-free checks.
        results = [_degraded_config_check(load), *run_all(None)]
    else:
        # issue #70: resolve the execution profile up-front. resolve_profile
        # raises only ConfigError (codex half-pair); degrade like an invalid
        # config rather than crashing on it.
        try:
            prof = resolve_profile(load.cfg, os.environ, Path.cwd())
        except ConfigError as exc:
            degraded = ConfigLoad(cfg=None, error=str(exc), missing=False)
            results = [_degraded_config_check(degraded), *run_all(None)]
        else:
            _echo_profile(prof, json_out)
            results = run_all(load.cfg)
    if json_out:
        typer.echo(to_json(
            results, profile=profile_dict(prof) if prof is not None else None
        ))
    else:
        print_table(results)
    if overall_status(results) is Status.FAIL:
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------


def _check_fix_name(only: str | None, plan: list[str]) -> str | None:
    """Return an error message string if `only` is given but does not
    match any registered step, otherwise None. Kept as a small pure
    helper so tests can exercise it without spinning up typer.
    """
    if only is None:
        return None
    if not plan:
        return (
            f"setup: unknown --fix target {only!r}; "
            f"valid names: {', '.join(STEP_NAMES)}"
        )
    return None


def _setup_config_step(
    load: ConfigLoad, scaffold_error: str | None = None, *, scaffolded: bool = False
) -> StepResult:
    """Synthetic `config` step for setup's degraded plan (issue #71).

    Normally NEEDS_USER (not FAIL): the user must supply something before the
    config-dependent steps can run. What that something is depends on how we
    got here: on the fresh-checkout path (`missing`, or `scaffolded` — the
    file was just written from the template, so its blockers are the blank
    cloud creds) the hint names the credentials, derived from
    CLOUD_REQUIRED_FIELDS — the same SSOT the `init` next-step wording uses,
    so the two never drift. A pre-existing repo.yaml that fails to load could
    be broken in any way (syntax, validation), so the hint says to fix it —
    mirroring doctor's `_degraded_config_check` wording — and the detail field
    carries the actual error.

    When auto-scaffold itself failed (an unwritable dir), the step is FAIL
    instead — a hard error the user must clear before anything can proceed.
    """
    if scaffold_error is not None:
        return StepResult(
            "config",
            StepStatus.FAIL,
            scaffold_error,
            fix_hint="make the directory writable, then re-run `kagura-engineer setup`",
        )
    if load.missing or scaffolded:
        creds = ", ".join(CLOUD_REQUIRED_FIELDS)
        hint = (
            f"fill in the cloud credentials ({creds}) in repo.yaml, "
            "then re-run `kagura-engineer setup`"
        )
    else:
        hint = "fix repo.yaml, then re-run `kagura-engineer setup`"
    return StepResult(
        "config",
        StepStatus.NEEDS_USER,
        load.error or "config unavailable",
        fix_hint=hint,
    )


def _written_backend_needs_creds(repo_yaml_path: Path) -> bool:
    """True when the freshly-scaffolded repo.yaml won't validate without creds.

    The shipped template defaults to ``memory_backend: cloud`` with blank cloud
    fields, which fails ``Config`` validation until filled (issue #43 item 2).
    Read the file we just wrote and report whether it is a cloud backend missing
    any of its required credentials, so the ``init`` next-step hint is accurate
    and survives a future change to the template's default backend. Any parse
    error degrades to ``False`` (fall back to the plain hint) — this is a UX
    affordance, never a hard gate.
    """
    try:
        data = yaml.safe_load(repo_yaml_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return False
    if not isinstance(data, dict):
        return False
    if data.get("memory_backend", "cloud") != "cloud":
        return False
    return any(not data.get(field) for field in CLOUD_REQUIRED_FIELDS)


@app.command()
def init(
    directory: str = typer.Option(
        ".", "--dir", "-d", help="repo root to scaffold (default: current directory)"
    ),
) -> None:
    """Scaffold a repo.yaml template and add it to .gitignore.

    Idempotent and never overwrites an existing repo.yaml — safe to re-run. Run
    this first in a new checkout, then edit repo.yaml and run `setup`.
    """
    root = Path(directory)
    if not root.is_dir():
        typer.echo(f"init: directory does not exist: {root}", err=True)
        raise typer.Exit(code=2)
    result = scaffold(root)
    if result.repo_yaml_created:
        typer.echo(f"created {result.repo_yaml_path}")
    else:
        typer.echo(f"{result.repo_yaml_path} already exists — left unchanged")
    if result.gitignore_updated:
        typer.echo(f"added 'repo.yaml' to {result.gitignore_path}")
    else:
        typer.echo("'repo.yaml' already in .gitignore")

    # issue #43 item 2: the shipped template uses memory_backend: cloud with
    # blank creds, so the freshly-written file fails Config validation until the
    # user fills them in. Surface that next step explicitly instead of a generic
    # "edit then run setup" that hides why `setup`/`doctor` would error. Keyed
    # off the backend we just wrote, so a (future) local-default template — or an
    # already-validating pre-existing file — gets the plain message.
    if result.repo_yaml_created and _written_backend_needs_creds(result.repo_yaml_path):
        # Derive the field list from the shared SSOT so a new required cloud
        # field is named here automatically (no hand-typed prose to drift).
        creds = ", ".join(CLOUD_REQUIRED_FIELDS)
        typer.echo(
            f"\nNext: fill in the cloud credentials ({creds}) in repo.yaml — "
            "it won't validate until you do — then run `kagura-engineer setup`."
        )
    else:
        typer.echo("\nNext: edit repo.yaml, then run `kagura-engineer setup`.")


@app.command()
def setup(
    config: str = _CONFIG_OPT,
    fix: str | None = typer.Option(
        None, "--fix", help="run only the named step (e.g. --fix git)"
    ),
    no_input: bool = typer.Option(
        False, "--no-input", help="never prompt; fail loudly on user-action steps"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="preview all steps without executing; exit 0/1/2 on feasibility"
    ),
    full: bool = typer.Option(
        False, "--full",
        help="also install memory hooks + skills into the repo (interactive Claude "
             "Code wiring); default generates .mcp.json only",
    ),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Resolve dependencies and bootstrap a healthy dev environment.

    Exit codes (Plan 2 design doc §1.6):

        0 — all steps OK or SKIPPED
        1 — at least one step FAIL (hard error)
        2 — at least one step NEEDS_USER (interactive action required)
        2 — also used for config / unknown --fix errors
    """
    load = load_config_lenient(config)

    # First-install UX (issue #71): a missing repo.yaml is the fresh-checkout
    # case. Auto-scaffold it (same code path as `init`) so `setup` is the only
    # command a new checkout has to type — then re-load. Suppressed under
    # --dry-run, where a preview must not write to disk.
    scaffold_error: str | None = None
    scaffolded = False
    if load.cfg is None and load.missing and not dry_run:
        try:
            scaffold(Path(config).parent)
        except OSError as exc:
            # An unwritable dir must surface as a config FAIL row, not a traceback.
            scaffold_error = f"could not scaffold repo.yaml: {exc}"
        else:
            typer.echo(
                f"{config} not found — scaffolding one (same as 'kagura-engineer init')"
            )
            # After the re-load `missing` flips False (the file now exists),
            # but the right hint is still the template's blank creds — carry
            # the fact that we just scaffolded into the synthetic config step.
            scaffolded = True
            load = load_config_lenient(config)

    # Validate --fix before running anything.
    err = _check_fix_name(fix, build_plan(only=fix))
    if err is not None:
        typer.echo(err, err=True)
        raise typer.Exit(code=2)

    if load.cfg is not None:
        report = run_plan(
            load.cfg, no_input=no_input, dry_run=dry_run, only=fix, full=full
        )
    else:
        # Degraded plan: a synthetic config step naming the next action, the
        # config-free steps run, the config-dependent ones SKIPPED.
        report = run_plan(
            None,
            no_input=no_input,
            dry_run=dry_run,
            only=fix,
            full=full,
            config_step=_setup_config_step(load, scaffold_error, scaffolded=scaffolded),
        )

    if json_out:
        typer.echo(setup_to_json(report))
    else:
        setup_print_table(report)

    # Exit-code policy:
    #   - 1 wins over 2: if any FAIL, the user has a hard error to
    #     fix and NEEDS_USER items are secondary
    #   - 2 only when there is NEEDS_USER but NO FAIL
    if report.failed:
        raise typer.Exit(code=1)
    if report.needs_user:
        raise typer.Exit(code=2)


# ---------------------------------------------------------------------------
# run (Plan 3 — memory-grounded agent loop)
# ---------------------------------------------------------------------------


@app.command()
def run(
    issue: int = typer.Argument(..., help="GitHub issue number to drive to a PR"),
    config: str = _CONFIG_OPT,
    no_remember: bool = typer.Option(
        False, "--no-remember", help="skip memory persist (recall still happens)"
    ),
    unattended: bool = typer.Option(
        False, "--unattended",
        help="dial HITL down: delegated phases proceed on green/yellow without "
             "asking (red/unknown still halt)",
    ),
    json_out: bool = typer.Option(
        False, "--json",
        help="emit the run report as JSON; phase progress streaming is "
             "suppressed so stdout stays a single valid JSON document",
    ),
) -> None:
    """Drive a GitHub issue to a PR via the memory-grounded agent loop.

    Exit codes: 0 = PR reached · 1 = hard fail · 2 = blocked
    (guard / gate halt — resumable by re-running).
    """
    try:
        cfg = load_config(config)
        prof = resolve_profile(cfg, os.environ, Path.cwd())
    except ConfigError as exc:
        typer.echo(f"run: invalid config '{config}': {exc}", err=True)
        raise typer.Exit(code=2)

    # issue #12: stream phase progress to stdout as the run advances, so a
    # long autonomous run is not a blank screen until the final table. Suppressed
    # under --json so stdout stays clean, machine-parseable JSON.
    progress = None if json_out else typer.echo
    # issue #70: announce the resolved execution profile before phase work.
    _echo_profile(prof, json_out)
    report = replace(
        run_idea(cfg, issue, no_remember=no_remember, unattended=unattended,
                 progress=progress),
        profile=prof,
    )

    if json_out:
        typer.echo(run_to_json(report))
    else:
        run_print_table(report)

    raise typer.Exit(code=STATUS_EXIT[report.status])


# ---------------------------------------------------------------------------
# review (Plan 4 — reviewer 連結; --fix = Plan 4b auto-review/fix loop)
# ---------------------------------------------------------------------------


@app.command()
def review(
    target: str = typer.Argument("HEAD", help="git ref, branch, or PR number to review as head"),
    base: str = typer.Option("main", "--base", help="base ref to diff against"),
    fix: bool = typer.Option(
        False, "--fix", help="auto-fix loop: on red, claude -p fixes findings and re-reviews"
    ),
    config: str = _CONFIG_OPT,
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Launch kagura-code-reviewer on a PR/branch and gate on its JSON verdict.

    With --fix, run the Plan 4b loop: on a red verdict, claude -p fixes the
    blocking findings and commits, then re-reviews — up to `review.max_loops`.

    Exit codes: 0 = green/yellow (or nothing to review) · 1 = could not
    review / a fix failed · 2 = red (blocking findings — resumable).
    """
    try:
        cfg = load_config(config)
        prof = resolve_profile(cfg, os.environ, Path.cwd())
    except ConfigError as exc:
        typer.echo(f"review: invalid config '{config}': {exc}", err=True)
        raise typer.Exit(code=2)

    # issue #70: without --fix no brain runs, so the brain line is shown only
    # when the fix loop is active.
    _echo_profile(prof, json_out, brain=fix)

    if fix:
        loop_report = replace(review_fix_loop(cfg, target, base=base), profile=prof)
        if json_out:
            typer.echo(review_loop_to_json(loop_report))
        else:
            review_print_loop_table(loop_report)
        raise typer.Exit(code=REVIEW_STATUS_EXIT[loop_report.status])

    report = replace(review_pr(cfg, target, base=base), profile=prof)

    if json_out:
        typer.echo(review_to_json(report))
    else:
        review_print_table(report)

    raise typer.Exit(code=REVIEW_STATUS_EXIT[report.status])


# ---------------------------------------------------------------------------
# eval (issue #57 — A/B: does memory grounding measurably improve PRs?)
# ---------------------------------------------------------------------------


def _pr_number(url: str | None) -> str | None:
    """Extract a GitHub PR number from a `.../pull/N` URL (for review targeting).

    Returns the number as a string (the `review` target type) or None when the URL
    is absent / not a recognisable pull URL — in which case that arm contributes no
    review-based signals (run-only signals still count)."""
    if not url:
        return None
    parts = url.rstrip("/").split("/")
    if len(parts) >= 2 and parts[-2] == "pull" and parts[-1].isdigit():
        return parts[-1]
    return None


@app.command()
def eval(
    issues: list[int] = typer.Argument(
        ..., help="the fixed issue set to run in both arms (e.g. eval 12 14 19)"
    ),
    config: str = _CONFIG_OPT,
    review: bool = typer.Option(
        False, "--review",
        help="also run the auto-review/fix loop on each arm's PR to measure review "
             "findings + re-fix iterations (mutates each arm's branch; slower)",
    ),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Measure memory-grounded uplift: run the same issues with recall ON vs OFF.

    Moat lever M3. Drives each issue through two arms — grounded (normal loop) and
    control (`run_idea` with grounding disabled) — and prints an A/B table on
    objective signals: PR-reached rate, gate-verdict rate, and (with `--review`)
    review findings + re-fix-loop iterations. Always exits 0 when the measurement
    completes; the verdict (improved/regressed/neutral) is informational, not a gate.

    WARNING: this launches the full run loop twice per issue and (with `--review`)
    the fix loop on top — it mutates the repo and is expensive. Run it on a pinned,
    disposable issue set; see docs/moat/m3-memory-uplift-eval.md for the procedure.
    """
    try:
        cfg = load_config(config)
        prof = resolve_profile(cfg, os.environ, Path.cwd())
    except ConfigError as exc:
        typer.echo(f"eval: invalid config '{config}': {exc}", err=True)
        raise typer.Exit(code=2)

    progress = None if json_out else typer.echo
    # issue #70: both arms share one profile — print it once up-front.
    _echo_profile(prof, json_out)

    # The grounded/control arms differ by exactly one variable: run_idea's `ground`
    # (memory READ). Memory WRITE is held constant OFF for BOTH arms via
    # no_remember=True — deliberately, not just for the control arm: it keeps the
    # measurement repeatable run-to-run (no savepoints/feedback accrue between
    # eval runs) and prevents cross-arm contamination through the shared
    # `run:<issue>` resume key (a grounded-arm done-state would otherwise make the
    # control arm resume instead of running clean). Grounding (the IV) is read;
    # persistence (held constant) is write — so disabling write on both arms is
    # correct, and the grounded arm's recall is unaffected.
    def _run_fn(issue: int, *, ground: bool):
        # issue #57: per-arm isolation — each arm runs in its own
        # run-<issue>-<arm> worktree/branch/resume-key (run_label), so the control
        # arm never reuses the grounded arm's worktree/branch/PR.
        return run_idea(cfg, issue, ground=ground, no_remember=True,
                        unattended=True, progress=progress,
                        run_label="grounded" if ground else "control")

    review_fn: ReviewFn | None = None
    if review:
        def _review_fn(run_report, grounded):
            target = _pr_number(run_report.pr_url)
            if target is None:
                return None
            return review_fix_loop(cfg, target, base="main")
        review_fn = _review_fn

    report = replace(
        run_ab_eval(issues, _run_fn, review_fn=review_fn, progress=progress),
        profile=prof,
    )

    if json_out:
        typer.echo(eval_to_json(report))
    else:
        eval_print_table(report)


if __name__ == "__main__":
    app()
