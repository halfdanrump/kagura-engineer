---
name: init
description: Use to bootstrap a fresh checkout for the kagura-engineer harness — scaffolds a commented repo.yaml template and adds it to .gitignore by shelling out to `kagura-engineer init`. Run this before setup/run/review when no repo.yaml exists yet. Idempotent and never overwrites.
---

# kagura-engineer: init

Thin wrapper around the `kagura-engineer init` CLI verb. It scaffolds the per-checkout
`repo.yaml` template and adds it to `.gitignore` so the workspace/context IDs never land
in git. It reimplements nothing — it shells out to the installed CLI and surfaces the result.

**Announce:** "Using the kagura-engineer:init skill to scaffold repo.yaml."

> Safe to run anytime: it is **idempotent** and **never overwrites** an existing
> `repo.yaml`. No repo mutation beyond writing `repo.yaml` (if absent) and appending one
> line to `.gitignore`. Not a Harness — no model budget, no PR, no HITL gate.

Usage: `kagura-engineer:init` — no arguments. Add `--dir <path>` to scaffold a repo other
than the current directory.

## Steps

1. **Run init:**

   ```bash
   kagura-engineer init
   ```

   Pass `--dir <path>` to scaffold a different repo root (default: current directory).

2. **Surface the result.** Report whether `repo.yaml` was created (or left unchanged
   because it already existed) and whether `repo.yaml` was added to `.gitignore`.

3. **Hand off.** Tell the user to edit `repo.yaml` (profile, backend choice,
   workspace/context ids), then run `kagura-engineer:setup` to provision the environment
   and `kagura-engineer:doctor` to verify it.
