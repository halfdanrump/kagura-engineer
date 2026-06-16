"""Structural guards for the Claude Code skill-plugin wrapper (issue #28).

These tests pin the *thin wrapper* contract:

- the plugin manifest exists and is well-formed,
- the plugin / marketplace version strings stay in lock-step with the package
  version (single source of truth = ``kagura_engineer.__version__``),
- every advertised CLI verb has a skill, each skill shells out to the
  ``kagura-engineer`` CLI rather than importing harness logic, and
- harness skills carry a cost / mutation / PR / HITL warning,
- the sdist build excludes the plugin-only directories so the PyPI artifact
  stays a pure Python package.

They are deliberately filesystem/manifest assertions — there is no business
logic to unit-test here, the whole point of the wrapper is that it has none.
"""

from __future__ import annotations

import json
import pathlib
import re
import tomllib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PLUGIN_JSON = REPO_ROOT / ".claude-plugin" / "plugin.json"
MARKETPLACE_JSON = REPO_ROOT / ".claude-plugin" / "marketplace.json"
SKILLS_DIR = REPO_ROOT / "skills"
PYPROJECT = REPO_ROOT / "pyproject.toml"

# Every CLI verb that gets a skill wrapper. init/doctor/setup are setup helpers
# (init scaffolds repo.yaml); run/review are the harness flows.
EXPECTED_SKILLS = ["init", "doctor", "setup", "run", "review", "eval"]
HARNESS_SKILLS = ["run", "review", "eval"]

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def _load_json(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _frontmatter(md_path: pathlib.Path) -> tuple[dict[str, str], str]:
    text = md_path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(text)
    assert match, f"no YAML frontmatter in {md_path}"
    fm: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            fm[key.strip()] = value.strip()
    return fm, text


# --- plugin.json ---------------------------------------------------------


def test_plugin_json_well_formed():
    assert PLUGIN_JSON.is_file(), "missing .claude-plugin/plugin.json"
    data = _load_json(PLUGIN_JSON)
    assert data["name"] == "kagura-engineer"
    # author must be an object (every kagura sibling uses {name, url})
    assert isinstance(data["author"], dict)
    assert data["author"].get("name")
    assert data["license"] == "Apache-2.0"
    assert "version" in data


def test_plugin_version_matches_package():
    from kagura_engineer import __version__

    assert _load_json(PLUGIN_JSON)["version"] == __version__


# --- marketplace.json ----------------------------------------------------


def _marketplace_entry() -> dict:
    mk = _load_json(MARKETPLACE_JSON)
    return next(p for p in mk["plugins"] if p["name"] == "kagura-engineer")


def test_marketplace_well_formed():
    assert MARKETPLACE_JSON.is_file(), "missing .claude-plugin/marketplace.json"
    entry = _marketplace_entry()
    # standalone install: the plugin lives at the marketplace repo root
    assert entry["source"] == "./"


def test_marketplace_version_matches_package():
    from kagura_engineer import __version__

    assert _marketplace_entry()["version"] == __version__


# --- skills --------------------------------------------------------------


@pytest.mark.parametrize("verb", EXPECTED_SKILLS)
def test_skill_present(verb):
    assert (SKILLS_DIR / verb / "SKILL.md").is_file(), f"missing skill: {verb}"


@pytest.mark.parametrize("verb", EXPECTED_SKILLS)
def test_skill_frontmatter(verb):
    fm, _ = _frontmatter(SKILLS_DIR / verb / "SKILL.md")
    # name must equal the directory → drives the kagura-engineer:<verb> namespace
    assert fm.get("name") == verb
    assert fm.get("description"), f"{verb}: description is required"


def test_skills_match_cli_verbs():
    """The skill set must track the CLI's commands. A verb added / removed / renamed
    in cli.py without a matching skill (or an EXPECTED_SKILLS update) is a drift bug —
    exactly the SKILL.md↔CLI divergence the thin-wrapper contract is meant to avoid.
    """
    from kagura_engineer.cli import app

    cli_verbs = sorted(
        (c.name or c.callback.__name__)
        for c in app.registered_commands
        if c.name or c.callback
    )
    assert sorted(EXPECTED_SKILLS) == cli_verbs, (
        f"skill set {sorted(EXPECTED_SKILLS)} != CLI verbs {cli_verbs} — "
        "add/rename the skill under skills/ or update EXPECTED_SKILLS"
    )


@pytest.mark.parametrize("verb", EXPECTED_SKILLS)
def test_skill_is_thin_wrapper(verb):
    _, text = _frontmatter(SKILLS_DIR / verb / "SKILL.md")
    # shells out to the CLI ...
    assert "kagura-engineer" in text, f"{verb}: must invoke the kagura-engineer CLI"
    # ... and never imports harness logic
    assert "import kagura_engineer" not in text, f"{verb}: must not import the package"


@pytest.mark.parametrize("verb", EXPECTED_SKILLS)
def test_skill_runs_doctor_precondition(verb):
    _, text = _frontmatter(SKILLS_DIR / verb / "SKILL.md")
    # the doctor skill IS the precondition; the rest must run it first. init is
    # the pre-doctor bootstrap (it scaffolds repo.yaml on a fresh checkout that
    # may have nothing set up yet) — doctor is a downstream verify, not its
    # precondition, so it is exempt alongside doctor.
    if verb in ("doctor", "init"):
        return
    assert "kagura-engineer doctor" in text, f"{verb}: must run doctor as a precondition"


@pytest.mark.parametrize("verb", EXPECTED_SKILLS)
def test_skill_has_usage_affordance(verb):
    _, text = _frontmatter(SKILLS_DIR / verb / "SKILL.md")
    # Every skill must show how to invoke it (a "Usage:" line or, for the
    # required-arg skills, a "No-argument usage" placeholder-help section).
    assert ("Usage:" in text) or ("No-argument usage" in text), (
        f"{verb}: no usage affordance"
    )


@pytest.mark.parametrize("verb", ["run"])
def test_required_arg_skill_has_placeholder_help(verb):
    _, text = _frontmatter(SKILLS_DIR / verb / "SKILL.md")
    # run takes a required argument — it must instruct "no arg → print
    # usage and stop" rather than guessing/shelling out, and show an example.
    assert "No-argument usage" in text, f"{verb}: missing placeholder-help section"
    assert "Example:" in text, f"{verb}: placeholder help should show an Example"


@pytest.mark.parametrize("verb", HARNESS_SKILLS)
def test_harness_skill_warns(verb):
    _, text = _frontmatter(SKILLS_DIR / verb / "SKILL.md")
    low = text.lower()
    assert "harness" in low, f"{verb}: must identify as a Harness"
    # at least one concrete consequence warning
    assert any(w in low for w in ("cost", "pr", "hitl", "mutat", "repo")), (
        f"{verb}: must warn about cost / repo-mutation / PR / HITL"
    )


# --- distribution boundary ----------------------------------------------


def test_sdist_excludes_plugin_dirs():
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    exclude = data["tool"]["hatch"]["build"]["targets"]["sdist"]["exclude"]
    assert "/skills" in exclude
    assert "/.claude-plugin" in exclude
