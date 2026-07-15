---
name: review
description: Use to review a git ref, branch, or PR with the local kagura-engineer reviewer — shells out to `kagura-engineer review [target]`, returning a structured verdict and findings. With `--fix` it is a HARNESS that edits files and commits; without `--fix` it is read-only.
---

# kagura-engineer: review

Thin wrapper around the `kagura-engineer review` CLI verb. It runs the multi-angle
reviewer over a target (working tree, branch, or PR number) and returns a structured
verdict. It reimplements nothing — it discovers config, optionally gates on `doctor`,
shells out, and surfaces the report.

> ⚠️ **Two modes — know which you are in:**
> - **Default (read-only)** — reviews the diff and reports. No repo mutation.
> - **`--fix` (Harness)** — on a red verdict it invokes the selected brain backend to
>   edit blocking findings and **commit** them, then re-reviews (bounded by
>   `config.review.max_loops`).
>   This mutates the repo and spends model budget — confirm with the user first.

**Announce:** "Using the kagura-engineer:review skill" (add "— with --fix, this is a Harness that commits" when fixing).

Usage: `kagura-engineer:review [target]` — target defaults to `HEAD`; may be a git ref, branch, or PR number. Add `--fix` to auto-fix blocking findings (Harness).

## Steps

1. **Validate the target.** The optional positional `TARGET` defaults to `HEAD`; it may
   be a git ref, branch name, or PR number. Validate before shell interpolation against a
   **first-char-anchored** allow-list — a positive integer `^[1-9][0-9]{0,8}$` for a PR
   number, or `^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$` for a ref/branch (leading `-` rejected
   so it cannot be parsed as an option) — and pass it after a `--` separator (step 4). Do
   not pass free-form text.

2. **Discover config.** `repo.yaml` in CWD by default; pass `--config <path>` otherwise.

3. **Precondition — run doctor:**

   ```bash
   kagura-engineer doctor --json
   ```

   `review` itself has no phase-0 guard, but running doctor first surfaces a missing
   reviewer (ollama / model) before you wait on a review. If `overall == "fail"`, hand
   off to `kagura-engineer:setup`; otherwise proceed.

4. **Review** (read-only). Options go before the `--` separator, the validated target
   after it, so a target beginning with `-` can never be parsed as a flag:

   ```bash
   kagura-engineer review --base <ref> --json -- <target>
   ```

   Or, to auto-fix blocking findings (**confirm the Harness warning first**):

   ```bash
   kagura-engineer review --fix --json -- <target>
   ```

5. **Interpret the status.** `ok` (exit 0) → green/yellow or nothing to review.
   `blocked` (exit 2) → red verdict with blocking findings; show them.
   `fail` (exit 1) → could not review (reviewer missing, timeout, fix failed).

6. **Surface & hand off.** Print the `verdict` and `findings` (dimension / severity /
   file:line / title); the full report is at `.kagura/review.json`. On a blocked verdict,
   offer `--fix` or surface the findings for manual resolution.
