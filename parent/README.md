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
- **`software-dev-agentic`** — inverted decomposition. One worker agent
  owns the whole plan → build → lint → test loop, then pure-Python
  machine gates adjudicate and one reviewer rates HIGH/MEDIUM/LOW. See
  section below.
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

## `software-dev-agentic` — agent-owned loop + machine gates + 1 reviewer

```bash
po run software-dev-agentic \
  --issue-id <issue-id> \
  --rig <name> \
  --rig-path <path>
```

Inverts the actor-critic decomposition. Rather than splitting
plan/build/lint/test across many roles, **one worker agent owns the
whole loop** (it may spawn its own subagents). After the worker turn a
**pure-Python mechanical gate layer** (no LLM) adjudicates the result,
then **exactly one reviewer agent** rates the work `HIGH/MEDIUM/LOW`.

Pipeline:

```
claim seed → baseline → loop(worker → machine gates → reviewer) → close
```

The seed closes **iff** the machine gates are green **AND** the reviewer
is `>= MEDIUM` — and the *flow* performs the close, never the worker (the
worker only ever closes its own iter bead). If it doesn't converge within
`--iter-cap` iterations the flow raises and leaves run-dir artifacts at
`<rig>/.planning/software-dev-agentic/<issue>/` for forensics.

**Machine gates** (all must pass, written to `verdicts/`):

| Check | What it asserts |
|---|---|
| `diff_clean` | Working tree committed and real work landed since the baseline ref. |
| `anti_mock` | No `unittest.mock` / `MagicMock` / `@patch` etc. added to non-`tests/` (production) files. |
| `lint` | Lint clean (reads the worker's teed `gate-lint.txt`, or re-runs the resolved lint command). |
| `tests` | Unit suite passes (reads teed `gate-tests.txt`, or re-runs). |
| `regression` | Passed/failed counts didn't regress vs the baseline. |

Gates diff `baseline_ref..HEAD` (not `HEAD~1..HEAD`), so a multi-commit
worker turn is fully covered.

**Knobs:**

| Flag | Default | Effect |
|---|---|---|
| `--iter-cap N` | `2` | Max worker→gate→review iterations before failing loud. |
| `--test-cmd "..."` | auto | Explicit gate test command (else auto-detects a Makefile `test-unit` target, falling back to the worker's teed output). |
| `--lint-cmd "..."` | auto | Explicit gate lint command (else auto-detects a Makefile `lint` target). |
| `--pack-path <path>` | `rig-path` | Code root to diff/gate when the repo under test differs from the rig root. |

**Use when:** you want the agent to own decomposition end to end but
still need machine-enforced quality bars (committed diff, no leaked
mocks, lint/tests green, no regression) plus a single quality review —
without the full multi-critic ceremony.

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
