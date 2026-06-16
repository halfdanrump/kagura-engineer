---
name: doctor
description: Use before any kagura-engineer run/review, or when the harness reports a blocked environment — diagnoses the local setup (git, claude, gh, ollama, memory backend, gh-issue-driven plugin) by shelling out to `kagura-engineer doctor` and reports what must be fixed.
---

# kagura-engineer: doctor

Thin wrapper around the `kagura-engineer doctor` CLI verb. It is the **precondition
gate** that the run / review skills run before doing any work — and you can
invoke it on its own to check the environment.

**Announce:** "Using the kagura-engineer:doctor skill to check the harness environment."

Usage: `kagura-engineer:doctor` — no arguments. Pass `--config <path>` only if `repo.yaml` is not in the current directory.

This skill reimplements nothing. It shells out to the installed CLI and surfaces the
result. If `kagura-engineer` is not on PATH, tell the user to install it
(`uv tool install kagura-engineer` or `pip install kagura-engineer`) and stop.

## Steps

1. **Discover config.** The CLI reads `repo.yaml` from the current directory by
   default. If the user named a different file, pass it through with `--config`.
   There is no search path — `repo.yaml` must be in CWD or given explicitly.

2. **Run the check (machine-readable):**

   ```bash
   kagura-engineer doctor --json
   ```

   Capture stdout (a single JSON document) and the exit code.

3. **Interpret the exit-code contract:**

   | Exit | `overall` | Meaning |
   |------|-----------|---------|
   | 0    | `ok` / `warn` | Safe to proceed. `warn` items are advisory (e.g. missing ollama model, cloud credential unverified). |
   | 1    | `fail`    | **Must stop.** At least one blocking check failed. |
   | 2    | —         | Config error (`repo.yaml` missing/invalid) before checks ran. |

4. **Surface the result.** Print the per-check `name` / `status` / `detail` /
   `fix_hint` table from the JSON. Lead with the blocking (`fail`) checks.

5. **Hand off.** If `overall == "fail"`, suggest `kagura-engineer:setup` to repair the
   environment, then re-run this skill. If `overall` is `ok`/`warn`, the caller may
   proceed to `kagura-engineer:run` or `kagura-engineer:review`.
