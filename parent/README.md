# po-formulas-software-dev

A formula pack for [`prefect-orchestration`](../../prefect-orchestration)
(the `po` CLI). Provides:

- **`software-dev-full`** — actor-critic pipeline for working one beads
  issue end to end: triage → plan (⟲) → build → lint+tests (parallel)
  → regression-gate → review (⟲) → deploy-smoke → artifacts →
  verification (⟲) → ralph (⟲) → docs → demo → learn.
- **`software-dev-fast`** — linear, no critics, no iterations. Plan →
  build → lint → test-unit → docs → close. 4–15 min wall vs 30–60+ for
  full. Use for static-text changes, registry entries, single-component
  features, focused bug fixes. See section below.
- **`software-dev-agentic`** — prompt-driven and minimal. One actor agent
  is told to open a worktree off `main`, implement the feature, run the
  repo's own tests / CI, and open a PR — looped against one critic that
  verifies goal accomplishment. See section below.
- **`epic`** — reads the open children of a beads epic, builds a Prefect
  DAG from their dependencies, and fans them out as concurrent
  `software-dev-full` sub-flows.
- **`minimal-task`** — see below.

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

## `software-dev-agentic` — one prompt-driven actor + one goal critic

```bash
po run software-dev-agentic \
  --issue-id <issue-id> \
  --rig <name> \
  --rig-path <path>
```

The prompt-over-code variant: essentially **one actor prompt + one
critic**. The actor agent is prompted (not orchestrator-wired) to open a
worktree off `main`, implement the feature there, run the repo's own
tests / CI, and **open a PR** when it's done. Then **exactly one critic
agent** verifies *goal accomplishment* — did the actor implement the
request faithfully? — and returns `pass` / `fail`. On `fail` the critic
writes a concrete fix list and the actor iterates against it.

Pipeline:

```
claim seed → loop(actor: worktree → build → test → PR  →  critic: pass | fail) → close
```

There is **no mechanical gate layer**: running tests and opening the PR
are the actor's job, and the goal-verifying critic is the only gate that
matters. The flow **never auto-merges** — the actor leaves a PR for human
review. The seed closes iff the critic passes, and the *flow* (not the
actor) performs the close (the actor only ever closes its own iter bead).
If it doesn't converge within `--iter-cap` iterations the flow raises and
leaves run-dir artifacts at `<rig>/.planning/software-dev-agentic/<issue>/`
for forensics.

**Knobs:**

| Flag | Default | Effect |
|---|---|---|
| `--iter-cap N` | `2` | Max actor→critic iterations before failing loud. |
| `--pack-path <path>` | `rig-path` | Code root the actor opens its worktree in, when the repo under test differs from the rig root. |

**Use when:** you want the agent to own the whole loop — including the
worktree and the PR — judged only on whether it accomplished the goal,
the minimal prompt-driven way.

### PR-sheriff hand-off (auto-merge)

The flow never merges, but after the critic passes it **announces the open
PR** to whichever managed sheriff owns the rig, which may then auto-merge.
`_dispatch_pr_sheriff` tries SoloCo's `soloco-sheriff` first, then
po-director's `pr-sheriff`; each `on_pr_opened` is best-effort and gates on
the workspace `merge_mode` (`auto` / `ai-approve-all`) plus its own applied
deployment, so the rig's owner fires and the first that dispatches wins
(never both). A non-auto workspace, an uninstalled pack, or an unreachable
Prefect just leaves the PR open for manual review.

Every outcome is logged so a stuck PR is debuggable from the run log alone:

```
agentic: PR sheriff dispatch — start (issue=<id> rig=<path>)
agentic: dispatched soloco-sheriff for <id>          # fired
agentic: soloco-sheriff declined <id> (...)          # gated out / unapplied
agentic: soloco-sheriff unavailable (...)            # pack not importable
agentic: soloco-sheriff dispatch skipped (...)       # on_pr_opened raised
agentic: no PR sheriff dispatched for <id> (tried: ...)   # nothing fired
```

A `dispatched` line means the problem (if a PR sits unmerged) is **downstream**
of this flow — the sheriff deployment run, its worker, or the merge itself —
not in the agentic dispatch.

> **Operational footgun — wrong-env worker poaching the pool.** The
> `soloco-sheriff` deployment runs on a shared work pool (`po`). If a Prefect
> worker started from an env *without* the po formula packs (e.g. the bare
> `prefect` tool venv rather than `prefect-orchestration`) also serves that
> pool, it can poach a dispatched run and then fail to import the flow, wedging
> the run in `Pending` forever while the PR sits unmerged. A `dispatched` log
> line with no resulting merge and a `Pending` sheriff run is the signature.
> Only run the pool's worker from the po-packs env (the managed
> `prefect-worker-po.service`), and don't start ad-hoc workers on `po` from
> other venvs.

> **Note — the machine-gate variant was deliberately superseded.** An
> earlier design (a "brief → worker → pure-Python mechanical gate layer
> → HIGH/MEDIUM/LOW reviewer → close-iff-gates-green-and-review≥MEDIUM"
> 5-stage flow) was built and then intentionally dropped in favour of the
> prompt-over-code loop documented above. Re-introducing the mechanical
> gate layer is a conscious philosophy reversal, not a bug fix — it needs
> an explicit human decision, not a silent restore.

## `agentic-epic` — brainstorm → PRD → plan-critic → finalize → one shared-branch PR

`agentic-epic` takes one high-level epic goal and lands it as **one integration
branch `epic/<epic-id>` + one PR**, via these phases:

```bash
po run agentic-epic --epic-id <id> --rig <name> --rig-path <path>
```

0. **Brainstorm** (gated; `--brainstorm auto` by default) — for a *vague or
   complex* goal, an up-front two-role design debate (a **Product Visionary** and
   a **Technical Architect** spawned as subagents, debating sequentially over the
   real code until the Architect emits "NO MORE QUESTIONS") writes
   `<run_dir>/design.md` for the PRD to build on. The agent **self-skips** a
   well-scoped goal. Unattended — no human-approval gate. `--brainstorm never`
   omits the step; `--brainstorm always` forces the debate.
1. **PRD** — one agent turns the goal into a short PRD (`<run_dir>/prd.md`):
   problem statement, acceptance criteria (observable outcomes), and the concrete
   surfaces/files the work will touch. The surfaces list is the raw material for
   coupling detection.
2. **Decomposition** — a planner breaks the goal into child issues **by logical
   separable chunk** (one concern per child, sized to plan/build/test/document
   together — *there is no target or maximum child count*), each declaring the
   files it `touches` (the coupling evidence) plus any real `depends_on` edge.
   Written to `<run_dir>/plan.json`.
3. **Plan-critic loop** — a critic audits the *decomposition* with deep,
   **code-grounded** checks: it walks the PRD acceptance criteria one by one to
   confirm coverage, opens the cited files to verify coupling (same-file pairs
   must be ordered), checks sizing both directions by the logical-chunk rule,
   dependency correctness, buildability, no layer-decomposition, and ordering.
   Actor-critic until pass or `plan_iter_cap`, feeding the fix list back to the
   planner. This gates the exact defect that bites: two children editing the same
   file left unordered.
4. **Dispatch** — the flow creates the children (each stamped with its resolved
   formula), records the planner's `depends_on` as `blocks` edges, cuts the
   `epic/<id>` branch off `main`, and fans the children out through `graph_run`.
5. **Finalize** — once every planned child has integrated (and the epic has more
   than one child), ONE **finalize builder** (a `software-dev-agentic` worker run
   against the shared branch) does the epic-wide work no per-child run did: the
   full rig suite (`make test-unit` / `make test-e2e` or the documented `pytest`)
   + cross-child integration/smoke, README/roadmap/docs updates, and post-flight
   artifacts. Skipped for a 1-child epic. Then the epic **acceptance-critic**
   judges the assembled diff against the PRD and the single PR is opened — ready
   on PASS, draft (with the gap list) on FAIL.

Per-child mechanics in shared-branch mode:

- Each worker branches off the **current epic tip** — not `main` — and **pushes
  without opening a PR**. Independent children run in parallel off the same tip;
  coupled children stack, because a dependent starts only after its prerequisite
  is integrated and so branches off the advanced tip. Parallel across the DAG's
  width, stacked along its depth.
- **Integrate-on-pass:** when a child's critic passes, the flow merges that
  child's branch into `epic/<id>` (serialized by a file lock so parallel lanes
  never race the shared ref). The merge is clean because coupled children are
  `blocks`-ordered and never run concurrently; a conflict aborts cleanly and is
  reported (rare, not routine).
- **Finalize:** the draft PR is flipped to ready for human / PR-sheriff review.

No remote / no `gh` → the branch + commits are left for a human to PR (graceful
no-op, never a hard failure).

### Coupling → `blocks` edges (the planner declares them; the flow records them)

The whole epic lands on **one** branch, so two children that edit the same file
collide if they run in parallel off the same tip. **Ordering is the planner's
judgment, not the flow's** — the planner declares a `depends_on` between any two
children that edit the same file (or where one needs another's output), and the
flow **records exactly those `depends_on` edges as bd `blocks` edges, inferring
nothing from `touches`**. Children with disjoint files and no declared
`depends_on` get **no** edge and fan out in parallel. `touches` is *evidence*,
not a wiring input: the plan-critic opens the cited files and verifies the
planner sequenced every same-file pair (a missing dep there is exactly what
causes an integration conflict downstream — and the fix is the dep, since there
is no deterministic auto-coupling and no auto-conflict-resolution).

This is the rule — *wire `blocks` edges only between children that actually
couple* — and it directly controls the parallel/serial shape (ZFC:
branch/worktree/merge mechanics are code; *which children couple* is the
planner's judgment, expressed as `depends_on` with `touches` as the evidence the
critic checks). Over-wiring serializes work that could run in parallel and pays
the slow tax while *still* risking conflicts; under-wiring lets two children edit
the same file off the same tip and collide. The plan-critic, not deterministic
code, is what guarantees the planner got it right.

**Per-child formula size** — a child may carry `"formula": "minimal-task"` in
the plan (or any registered formula) to run a lighter pipeline than the default
`software-dev-agentic` critic loop; trivial children (a one-line link, a single
registry entry) should use it.

Dry-run shows the planned shape without spawning workers or touching git:

```bash
po run agentic-epic --epic-id <id> --rig <name> --rig-path <path> --dry-run
# logs: PRD intent, the decomposition, coupled (shared-file) pairs, the resolved
# blocks edges, the epic/<id> branch + 1 draft PR (intended), and the
# parallel/serial lanes
```

Pass `--shared-branch=false` to fall back to the **legacy per-child-PR path**
(N independent `software-dev-agentic` runs, each its own worktree off `main` and
its own PR — no integration branch). Shared-branch is the default because for a
coupled epic N PRs are the worst of both worlds: parallel children collide on
merge, serial children are slow and *still* collide.

Mechanics live in `po_formulas/shared_branch.py` (pure git/gh transport + the
integration lock, no Prefect — unit-tested in `tests/test_shared_branch.py`).
Per-child base-off-tip is threaded through `software_dev_agentic`'s `epic_branch`
/ `parent_epic_id` kwargs (passed by `agentic_epic` via `graph_run`'s
`extra_formula_kwargs`); after a child passes its critic it runs a **merge-back
agent** (`agents/agentic-merge-back/`) that merges its own branch into the epic
branch under the lock — there is no deterministic `git merge` in the flow.
Ordering (which children are sequenced vs parallel) is the **planning agent's**
judgment, not inferred from `touches`. The brainstorm/PRD/decomposition/critique
prompts live under `po_formulas/agents/agentic-epic-{brainstorm,prd,planner,plan-critic,acceptance-critic}/`.

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
software-dev-full = "po_formulas.software_dev:software_dev_full"
epic = "po_formulas.epic:epic_run"
```

After `uv add` / `pip install`, `po list` shows them and `po run` dispatches.

## Install (editable, for local dev)

```bash
cd /home/ryan-24/Desktop/Code/personal/nanocorps/software-dev/po-formulas
uv sync            # installs core (editable) + this pack (editable)
source .venv/bin/activate
po list
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
#   --discover {ids,deps,both}    # default: both (deps + dot-suffix union)
#   --child-ids a,b,c             # explicit override; bypasses discovery
po run epic --epic-id sr-8yu --rig site --rig-path ... --discover deps
po run epic --epic-id sr-8yu --rig site --rig-path ... \
            --child-ids sr-8yu-feat-a,sr-8yu-feat-b,sr-8yu-feat-c

# Dry run (no Claude calls, no edits — exercises DAG only)
po run software-dev-full --dry-run --issue-id sr-8yu.3 ...
```

### Epic discovery modes

`epic_run` resolves the children of `--epic-id` using one of three
strategies, controlled by `--discover` (default `both`):

| Mode | What it does |
|---|---|
| `ids` | Probe `<epic>.1`, `<epic>.2`, … (gas-city naming convention). Fast; no `bd dep` graph needed. |
| `deps` | Walk the `bd dep` graph (parent-child + blocks edges) rooted at `--epic-id`. Works for any sub-graph; no naming convention required. |
| `both` | Run both walkers, union with stable de-dup (deps order first, then dot-suffix-only ids appended). Default. |

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
