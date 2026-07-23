# po-formulas-software-dev-wts

Worktree-aware variant of `po-formulas-software-dev`, bootstrapped in
nanocorps-gi3. Today the flow bodies are byte-equivalent to their
parent — only the entry-point keys, project name, and inner Python
package (`po_formulas_wts/`) are renamed so both packs can co-exist
under one venv. The actual `cd <worktree>` build step lands in
nanocorps-tbw.

A formula pack for [`prefect-orchestration`](../../../prefect-orchestration)
(the `po` CLI). Registers two `-wts`-suffixed flows:

- **`software-dev-full-wts`** — actor-critic pipeline for working one
  beads issue end to end: triage → plan (⟲) → build → lint+tests
  (parallel) → regression-gate → review (⟲) → deploy-smoke → artifacts
  → verification (⟲) → ralph (⟲) → docs → demo → learn.
- **`software-dev-fast-wts`** — linear, no critics, no iterations.
  Plan → build → lint → test-unit → docs → close. 4–15 min wall vs
  30–60+ for full. Use for static-text changes, registry entries,
  single-component features, focused bug fixes. See section below.
- **`pre-pr-review-wts`** — sling-able cumulative review run BEFORE
  the PR-writer fires. Three independent pillars: full lint+test+build
  vs `origin/<merge_target>` baseline; cumulative-diff critic via the
  `pre-pr-reviewer` agent; real-env smoke test via `make dev-up` +
  `pre-pr-smoke-tester` agent. Writes
  `<rig>/.planning/pre-pr-review/<sanitized-branch>/validation-report.md`
  and stamps `metadata.validation = passed | blocked` on the epic so
  the PR-writer can gate. Invoke with `--epic-id <id>` or
  `--branch <name>` (mutually exclusive).
- **`epic-wts`** — end-to-end epic runner. It creates one shared
  worktree for the epic at `<rig-path>/.worktrees/wts-<sanitized-epic-id>/` on
  branch `wts-<sanitized-epic-id>`, runs every child
  `software-dev-full-wts` flow inside that worktree, reviews and
  finalizes the accumulated branch, then merges it into the configured
  target branch and removes the worktree after green gates.

`epic` and `minimal-task` are NOT registered by this pack — use the
parent `po-formulas-software-dev` registrations for those today.

## `software-dev-fast` — linear pipeline for focused changes

```bash
po run software-dev-fast \
  --issue-id <issue-id> \
  --rig <name> \
  --rig-path <path>
```

Single iteration per role, no critics, no triage / baseline / regression /
verify / ralph / deploy-smoke / demo-video. Linter and tester auto-fix
during their work; closes the seed regardless of verdict (the agents
already did their best on retryable errors).

**Per-role defaults**: planner / builder run on `sonnet` + `medium`
effort (the flow stamps `PO_MODEL=sonnet` + `PO_EFFORT=medium` at flow
entry). Linter / tester drop to `sonnet` + `low` effort via per-role
`agents/<role>/config.toml`. Override at the flow level with
`--model opus --effort high` on `po run`; per-role config still wins
where present.

**Use full when:** multi-file architecture change, schema migration,
public API change, anything where you want a critic to read the plan
before code lands. **Use fast when:** static-text changes, registry
entries, single-component features, doc-only changes, focused bug fixes.
When in doubt, full.

## `minimal-task` — lightweight pipeline for fanout demos

Pipeline shape:

```
triage → plan → build → lint → close
```

No baseline, no plan-critic, no parallel test layers, no
regression-gate, no review, no deploy-smoke, no review-artifacts, no
verification, no ralph, no docs, no demo, no learn.

**When to use.** High-fanout epics (e.g. the snake-bead 100-way demo)
where running the full actor-critic loop on every trivial child wastes
tokens. Each child is small enough that a single-pass plan + build is
plenty.

**Fail-out semantics.** Lint runs after each build. If the linter writes
`{"verdict": "fail"}` once, the flow does ONE more build iteration
(reading the lint summary as a `revision_note`) and lints again. If
that second lint also fails, the flow raises `RuntimeError` — no ralph
fallback. The bead stays `in_progress` and run-dir artifacts remain at
`<rig>/.planning/minimal-task/<issue>/` for forensics.

**Verdict file.** The linter prompt now additionally writes
`verdicts/lint-iter-<N>.json` with `{"verdict": "pass"|"fail",
"summary": "..."}`. `software-dev-full` ignores it; `minimal-task`
gates on it.

```bash
po run minimal-task \
  --issue-id <id> \
  --rig <name> \
  --rig-path <path>
```

## How it plugs in

The `po` core has **no knowledge of these formulas** — they register
themselves via entry points in this pack's `pyproject.toml`:

```toml
[project.entry-points."po.formulas"]
software-dev-full-wts = "po_formulas_wts.software_dev:software_dev_full"
software-dev-fast-wts = "po_formulas_wts.software_dev:software_dev_fast"
```

After `uv add` / `pip install`, `po list` shows them and `po run` dispatches.

## Install (editable, for local dev)

```bash
cd /home/ryan-24/Desktop/Code/personal/nanocorps/software-dev-wts/po-formulas
uv sync            # installs core (editable) + this pack (editable)
source .venv/bin/activate
po list  # should show software-dev-full-wts and software-dev-fast-wts
```

## Run

From any rig (must have `.beads/` initialized):

```bash
cd /path/to/your-rig-root

# Single issue
po run software-dev-full \
  --issue-id sr-8yu.3 \
  --rig site \
  --rig-path /home/ryan-24/Desktop/Code/personal/nanocorps/seam-recruiting/site

# Full epic — fan out all ready children as a DAG
po run epic \
  --epic-id sr-8yu \
  --rig site \
  --rig-path /home/ryan-24/Desktop/Code/personal/nanocorps/seam-recruiting/site

# Discovery flags (prefect-orchestration-h5s):
#   --discover {parent-child,ids,deps,both}   # default: parent-child
#   --child-ids a,b,c                          # explicit override; bypasses discovery
po run epic --epic-id sr-8yu --rig site --rig-path ... --discover both
po run epic --epic-id sr-8yu --rig site --rig-path ... \
            --child-ids sr-8yu-feat-a,sr-8yu-feat-b,sr-8yu-feat-c

# Dry run (no Claude calls, no edits — exercises DAG only)
po run software-dev-full --dry-run --issue-id sr-8yu.3 ...
```

### Epic WTS shared worktree lifecycle

`epic-wts` owns the worktree lifecycle for its children. At the start
of an epic run it creates or reuses:

```text
<rig-path>/.worktrees/wts-<sanitized-epic-id>/  on branch  wts-<sanitized-epic-id>
```

The epic bead is stamped with `metadata.work_dir`, `metadata.branch`,
and `metadata.merge_target_branch`. Each child `software-dev-full-wts`
run receives the same values and stamps its own metadata with
`work_dir`, `branch`, `merge_target_branch`, and `epic_id`. That durable
metadata lets `po retry <child-id>` re-enter the existing epic worktree
instead of creating a per-child `<rig>/.worktrees/wts-<child-id>/`.

Child flows skip their standalone worktree setup and per-child merge
when an epic-managed worktree is present. Their commits land directly
on `wts-<sanitized-epic-id>`. If a child, review, finalize, smoke, or
CI step fails, the shared worktree is intentionally preserved for
inspection and retry.

After `epic_finalize_wts` passes local gates and remote CI for the epic
branch, it merges `wts-<sanitized-epic-id>` into
`metadata.merge_target_branch` (default `main`) and removes the shared
worktree. Parallel `epic-wts` runs against the same rig are isolated by
their distinct sanitized epic ids, so they use different worktree paths
and branches.

### Epic discovery modes

`epic_run` resolves the children of `--epic-id` using one of four
strategies, controlled by `--discover` (default `parent-child`):

| Mode | What it does |
|---|---|
| `parent-child` | **Default.** Walk ONLY parent-child edges rooted at `--epic-id` to collect the child set. `blocks` edges are still used to topo-order *within* the set, but they do NOT widen it — so dispatching an epic that another epic `blocks`-depends on no longer pulls that sibling epic (and its children) into the run. |
| `ids` | Probe `<epic>.1`, `<epic>.2`, … (legacy dot-suffix naming convention). Fast; no `bd dep` graph needed. |
| `deps` | Walk the `bd dep` graph (parent-child **+ blocks** edges) rooted at `--epic-id`. The blocks walk widens the discovered set. |
| `both` | Run the `deps` and `ids` walkers, union with stable de-dup (deps order first, then dot-suffix-only ids appended). |

> **Why `parent-child` is the default (po-formulas-software-dev-e9s):**
> `deps` / `both` walk `blocks` edges *up* to widen discovery. When EpicB
> `blocks`-depends on EpicA, dispatching EpicA with a blocks-walking mode
> discovered EpicB + all of EpicB's children and started them in parallel —
> inverting the dependency. `parent-child` keeps `blocks` for ordering only.
> Pass `--discover both` to opt back into the blocks-aware union.

`--child-ids a,b,c` is the escape hatch: it skips discovery, validates
each id exists and is open, and dispatches them in topo order built
from their `bd dep --type=blocks` edges. Out-of-set blockers are
dropped (only edges between the listed ids matter).

## Concurrency (the "worker pool")

Prefect handles this natively; two orthogonal knobs:

```bash
# Total concurrent flow runs (= max issues in flight)
prefect work-pool create po --type process --concurrency-limit 4

# Per-role caps across the whole deployment
prefect concurrency-limit create critic 2
prefect concurrency-limit create builder 3
# Tasks are already tagged with role names in this pack's flow.
```

## Authoring your own pack

Copy this pack's structure:

1. A `po_formulas/your_flow.py` with a Prefect `@flow`.
2. A `prompts/` dir with role `.md` templates.
3. Entry points in `pyproject.toml` under `[project.entry-points."po.formulas"]`.

After `uv add` into any project, `po list` shows your formula alongside
every other installed pack.
