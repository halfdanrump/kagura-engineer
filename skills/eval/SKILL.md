---
name: eval
description: Use to measure whether memory grounding actually improves PR quality — shells out to `kagura-engineer eval` with one or more issue numbers, running the SAME fixed issue set in two arms (recall ON vs OFF) and printing an A/B table. HARNESS — high cost; it runs the full loop twice per issue and mutates the repo. Confirm with the user before launching.
---

# kagura-engineer: eval

Thin wrapper around the `kagura-engineer eval` CLI verb (moat lever M3). It drives the
**same** fixed issue set through two arms — **grounded** (the normal `run` loop with
recall + pinned + graph-expanded memory) and **control** (the identical loop with
grounding disabled) — and prints an A/B table on objective signals already in the
pipeline: PR-reached rate, gate-verdict rate, and (with `--review`) review findings +
re-fix-loop iterations. It reimplements nothing — the orchestration lives in the CLI;
this skill discovers config, gates on `doctor`, shells out, and surfaces the result.

> ⚠️ **This is a Harness.** Before launching, confirm the user understands:
> - **Repo mutation × 2N** — the full run loop runs twice per issue (a worktree, commits,
>   and a PR per arm). With `--review` the auto-fix loop also mutates each arm's branch.
> - **Cost** — `run`'s selected brain budget, doubled across both arms of every issue.
> - **Disposable issue set** — run it on a pinned, throwaway issue set, not production work.

**Announce:** "Using the kagura-engineer:eval skill — this is a Harness that runs the loop twice per issue."

## No-argument usage

If invoked without an issue set, do NOT guess or shell out — print this and stop:

```
kagura-engineer:eval <issue> [<issue> ...]
  Measure memory-grounded uplift: run the same issues with recall ON vs OFF, print an A/B table.
  ⚠ Harness (high cost): the full run loop runs twice per issue; mutates the repo.
  Example:  kagura-engineer:eval 12 14 19 --review
  New here? run kagura-engineer:doctor first to check the environment.
```

## Steps

1. **Validate the issue arguments.** The CLI takes one or more issue *numbers*. Treat them
   as data: accept only positive integers (`^[0-9]+$` per token) and pass them as
   positionals after a `--` end-of-options separator. Do not pass unsanitized text.

2. **Discover config.** `repo.yaml` in CWD by default; pass `--config <path>` otherwise.

3. **Precondition — run doctor:**

   ```bash
   kagura-engineer doctor --json
   ```

   If `overall == "fail"` (exit 1), **stop** and hand off to `kagura-engineer:setup` — a
   blocked environment would fail both arms of every issue.

4. **Confirm with the user** (the Harness warning above) unless they have opted into an
   unattended measurement. Be explicit that the loop runs twice per issue.

5. **Run the eval:**

   ```bash
   kagura-engineer eval --json -- <issue> [<issue> ...]
   ```

   Add `--review` (before the `--`) to also measure review findings + re-fix-loop
   iterations per arm — this is more decisive but mutates each arm's branch and is slower.

6. **Interpret the result.** The report carries a `uplift.verdict`:
   `improved` (grounding measurably helped), `regressed` (grounding hurt), `neutral`
   (no measurable difference), or `inconclusive` (no issues / no comparable signals).
   The `grounded` vs `control` blocks carry the per-arm rates; `per_issue` has the
   per-issue gate outcomes for traceability.

7. **Surface honestly.** Print the A/B table and the verdict. Record the result whether or
   not it is positive — an honest negative is itself moat evidence about where grounding
   does and does not help. See `docs/moat/m3-memory-uplift-eval.md` for the full
   methodology and how to publish the result.
