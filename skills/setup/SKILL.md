---
name: setup
description: Use when kagura-engineer:doctor reports a blocked or failing environment — scaffolds missing config, installs and authenticates harness prerequisites, and generates Memory Cloud MCP config by shelling out to `kagura-engineer setup`, then re-checks with doctor.
---

# kagura-engineer: setup

Thin wrapper around the `kagura-engineer setup` CLI verb. It repairs the environment
that `kagura-engineer:doctor` diagnoses. It reimplements nothing — it shells out to the
installed CLI and surfaces the result.

**Announce:** "Using the kagura-engineer:setup skill to prepare the harness environment."

Usage: `kagura-engineer:setup` — no arguments. Add `--fix <step>` to repair one step, `--dry-run` to preview.

> Note: when `repo.yaml` is missing, `setup` scaffolds it automatically and reports
> the blank Cloud fields as `NEEDS_USER`. Fill in the backend choice and any required
> workspace/context ids, then re-run. Setup may prompt for credentials unless run with
> `--no-input`.

## Steps

1. **Discover config.** Pass `--config <path>` if `repo.yaml` is not in CWD.

2. **(Optional) Preview.** For a dry run that changes nothing:

   ```bash
   kagura-engineer setup --dry-run --json
   ```

3. **Run setup:**

   ```bash
   kagura-engineer setup --json
   ```

   To repair a single step only, use `--fix <step>` where `<step>` is one of
   `git`, `claude-code`, `gh`, `ollama`, `ollama-models`, `memory-cloud`,
   `memory-mcp`. Add `--full` to the full run or the `memory-mcp` step to also
   install memory hooks and skills; the default generates `.mcp.json` only.

   Exit-code contract: `0` = all steps ok/skipped · `1` = any step failed
   (**takes priority over 2**) · `2` = a step needs user input with no failure
   (or a config / unknown `--fix` error).

4. **Surface the result.** Print the `ran` / `skipped` / `needs_user` / `failed`
   groups from the JSON, leading with `failed` and `needs_user`.

5. **Re-check and hand off.** Re-run the diagnosis to confirm the repair:

   ```bash
   kagura-engineer doctor --json
   ```

   When `overall` is `ok`/`warn`, hand back to the caller so it can proceed to
   `kagura-engineer:run` or `kagura-engineer:review`.
