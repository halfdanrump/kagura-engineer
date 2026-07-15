---
name: run
description: Use to drive a single GitHub issue to a pull request with the kagura-engineer harness — shells out to `kagura-engineer run` with an issue number (guard → recall → worktree → start → implement → ship → persist). HARNESS — this mutates the repo, creates a PR, and spends model budget; confirm with the user before launching.
---

# kagura-engineer: run

Thin wrapper around the `kagura-engineer run` CLI verb. It drives one GitHub issue
through the full memory-grounded loop to an open PR. It reimplements nothing — the
orchestration lives entirely in the CLI; this skill discovers config, gates on
`doctor`, shells out, and surfaces the result.

> ⚠️ **This is a Harness, not an atomic tool.** Before launching, make sure the user
> understands the consequences and has confirmed:
> - **Repo mutation** — creates a `run-<issue>` git worktree and commits there.
> - **PR creation** — opens a GitHub pull request via gh-issue-driven on success.
> - **Cost** — invokes the selected brain backend multiple times (start / implement / ship).
> - **HITL** — gates can halt (`blocked`, exit 2) and wait for a human decision.

**Announce:** "Using the kagura-engineer:run skill — this is a Harness that will create a PR."

## No-argument usage

If invoked without an issue number, do NOT guess or shell out — print this and stop:

```
kagura-engineer:run <issue-number>
  Drive one GitHub issue to a PR via the kagura-engineer harness.
  ⚠ Harness: creates a worktree + commits, opens a PR, spends model budget, gates on HITL.
  Example:  kagura-engineer:run 42
  New here? run kagura-engineer:doctor first to check the environment.
```

## Steps

1. **Validate the issue argument.** The CLI takes a single positive integer issue
   number. Reject anything that does not match `^[1-9][0-9]{0,8}$` before interpolating
   it into the shell — do not pass free-form text through.

2. **Discover config.** `repo.yaml` in CWD by default; pass `--config <path>` otherwise.

3. **Precondition — run doctor:**

   ```bash
   kagura-engineer doctor --json
   ```

   If `overall == "fail"` (exit 1), **stop**: surface the blocking checks and hand off
   to `kagura-engineer:setup`. Do not proceed to run. (`ok`/`warn` may proceed.)

4. **Confirm with the user** (the Harness warning above) unless they have already
   opted into an unattended run.

5. **Run:**

   ```bash
   kagura-engineer run <issue> --json
   ```

   Add `--unattended` to proceed without prompting on green/yellow gates
   (red/unknown still halt), or `--no-remember` to skip the memory savepoint.
   Without `--json`, phase progress streams live.

6. **Interpret the status.** `status == "ok"` (exit 0) → PR created (`pr_url`).
   `"blocked"` (exit 2) → a gate halted; show `resume_hint` and surface the verdict for
   a human decision. `"fail"` (exit 1) → hard error; show the failing phase's `detail`
   from the `phases` array (there is no top-level `detail` key).

7. **Surface & hand off.** Print the phase table and `pr_url`. On success, extract the
   PR number from `pr_url` and suggest `kagura-engineer:review <PR-number>`. On
   `blocked`/`fail`, surface the `resume_hint` so the user can retry after addressing
   the cause.
