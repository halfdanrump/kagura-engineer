"""Renderers for `EvalReport` — the A/B moat table (rich) + JSON.

Mirrors run/render.py: a `to_json` for machine consumption
(the reproducible artifact the issue asks for) and a `print_table` for humans.
"""
from __future__ import annotations

import json

from rich.console import Console
from rich.table import Table

from ..profile import to_dict_or_none as _profile_dict
from .result import ArmRun, ArmStats, EvalReport

_VERDICT_ICON = {
    "improved": "✅",
    "regressed": "❌",
    "neutral": "➖",
    "inconclusive": "❔",
}


def _round(value: float | None) -> float | None:
    return round(value, 3) if value is not None else None


def _stats_to_dict(s: ArmStats) -> dict:
    return {
        "n": s.n,
        "pr_reached": s.pr_reached,
        "pr_rate": _round(s.pr_rate),
        "green": s.green,
        "yellow": s.yellow,
        "red": s.red,
        "unknown": s.unknown,
        "failed": s.failed,
        "green_rate": _round(s.green_rate),
        "reviewed": s.reviewed,
        "mean_findings": _round(s.mean_findings),
        "mean_blocking": _round(s.mean_blocking),
        "mean_fix_iterations": _round(s.mean_fix_iterations),
    }


def _arm_to_dict(a: ArmRun) -> dict:
    return {
        "issue": a.issue,
        "outcome": a.outcome,
        "pr_reached": a.pr_reached,
        "findings_total": a.findings_total,
        "findings_blocking": a.findings_blocking,
        "fix_iterations": a.fix_iterations,
    }


def to_json(report: EvalReport) -> str:
    up = report.uplift
    return json.dumps(
        {
            "issues": report.issues,
            "profile": _profile_dict(report.profile),
            "grounded": _stats_to_dict(report.grounded_stats),
            "control": _stats_to_dict(report.control_stats),
            "uplift": {
                "verdict": up.verdict,
                "pr_rate_delta": _round(up.pr_rate_delta),
                "green_rate_delta": _round(up.green_rate_delta),
                "mean_findings_delta": _round(up.mean_findings_delta),
                "mean_blocking_delta": _round(up.mean_blocking_delta),
                "mean_fix_iterations_delta": _round(up.mean_fix_iterations_delta),
            },
            "per_issue": {
                "grounded": [_arm_to_dict(a) for a in report.grounded_runs],
                "control": [_arm_to_dict(a) for a in report.control_runs],
            },
            "duration_s": round(report.duration_s, 3),
        },
        ensure_ascii=False,
    )


def _fmt(value: float | None, *, pct: bool = False) -> str:
    if value is None:
        return "—"
    return f"{value:.0%}" if pct else f"{value:.2f}"


def print_table(report: EvalReport) -> None:
    console = Console()
    g, c = report.grounded_stats, report.control_stats
    up = report.uplift

    console.print(
        f"{_VERDICT_ICON.get(up.verdict, '?')} memory-grounded uplift: "
        f"[bold]{up.verdict}[/bold] over {len(report.issues)} issue(s)"
    )

    table = Table(title="A/B: grounded (recall on) vs control (recall off)")
    table.add_column("signal")
    table.add_column("grounded", justify="right")
    table.add_column("control", justify="right")
    table.add_column("Δ (grounded−control)", justify="right")

    table.add_row("PR-reached rate", _fmt(g.pr_rate, pct=True),
                  _fmt(c.pr_rate, pct=True), _fmt(up.pr_rate_delta, pct=True))
    table.add_row("green-gate rate", _fmt(g.green_rate, pct=True),
                  _fmt(c.green_rate, pct=True), _fmt(up.green_rate_delta, pct=True))
    table.add_row("mean review findings", _fmt(g.mean_findings),
                  _fmt(c.mean_findings), _fmt(up.mean_findings_delta))
    table.add_row("mean blocking findings", _fmt(g.mean_blocking),
                  _fmt(c.mean_blocking), _fmt(up.mean_blocking_delta))
    table.add_row("mean re-fix iterations", _fmt(g.mean_fix_iterations),
                  _fmt(c.mean_fix_iterations), _fmt(up.mean_fix_iterations_delta))
    console.print(table)

    # Per-issue outcomes (grounded → control) for traceability.
    detail = Table(title="per-issue gate outcome")
    detail.add_column("issue")
    detail.add_column("grounded")
    detail.add_column("control")
    control_by_issue = {a.issue: a for a in report.control_runs}
    for a in report.grounded_runs:
        b = control_by_issue.get(a.issue)
        detail.add_row(f"#{a.issue}", a.outcome, b.outcome if b else "—")
    console.print(detail)
