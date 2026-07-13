# po-formulas-software-dev

Software-development formula pack(s) for [`prefect-orchestration`](https://github.com/ryanholtschneider2/prefect-agent-orchestration).

This monorepo ships **two installable packages side-by-side**:

| Subdir | Distribution | What it provides |
|---|---|---|
| [`parent/`](./parent) | `po-formulas-software-dev` | The canonical pipeline: `software-dev-full` / `software-dev-fast` / `software-dev-edit`, `epic`, `epic-finalize`, `minimal-task`, `pre-pr-review`, `pr-writer`, `code-health-review`, `skill-evals`, `prompt`, `graph`, plus 18 role prompts. |
| [`wts/`](./wts) | `po-formulas-software-dev-wts` | Worktree-aware fork. Same flow bodies as `parent/` with `*-wts` EP keys; standalone issue flows run in `git worktree`s at `<rig>/.worktrees/wts-<sanitized-id>/`, while `epic-wts` runs all children in one shared epic worktree at `<rig>/.worktrees/wts-<sanitized-epic-id>/` on branch `wts-<sanitized-epic-id>`. Adds `epic-finalize-wts` with an LLM spec-auditor and pytest coverage for the worktree machinery. (The base `software-dev-agentic` flow now opens its worktree via prompt, so there is no separate `-wts` variant.) |

Both packages register entry points under `po.formulas` / `po.deployments` / `po.doctor_checks` / `po.commands` and coexist in the same venv (the `-wts` suffix on every EP key prevents collisions).

## Install

From inside this repo:

```bash
# parent (canonical pack)
uv pip install -e parent

# wts (worktree-aware fork — install alongside parent if you want both)
uv pip install -e wts
```

`po list` shows the merged set of formulas once both are installed. Picking `software-dev-full` vs `software-dev-full-wts` selects which variant runs.

## Why two packages

`wts` was forked from `parent` to add git-worktree isolation. Standalone issue flows get their own working tree, and `epic-wts` creates one working tree for the whole epic so serialized children build on the same branch without per-child merge churn. In both cases `.beads/` and `.planning/` are symlinked back to the main rig so bd ops and run-dir artifacts stay authoritative in one place. The fork keeps the EP namespace clean — operators opt in by running `po run software-dev-full-wts` instead of `po run software-dev-full`. Once the worktree pattern soaks in across formulas the two will likely converge again, but for now keeping them as two distributions lets us iterate on `wts` without disturbing the canonical pack.

See [`parent/README.md`](./parent/README.md) and [`wts/README.md`](./wts/README.md) for per-package detail.

## Layout

```
po-formulas-software-dev/
├── README.md                  # this file
├── parent/                    # po-formulas-software-dev
│   ├── pyproject.toml
│   ├── po_formulas/
│   ├── overlay/
│   └── tests/
└── wts/                       # po-formulas-software-dev-wts
    ├── pyproject.toml
    ├── po_formulas_wts/       # includes worktree.py + epic_finalize.py
    ├── overlay/
    └── tests/                 # worktree and flow pytest coverage
```

Both subdirs ship their own `pyproject.toml`, `tests/`, and overlay. Cross-subdir imports are forbidden — each package stands on its own. Shared deps (`prefect-orchestration`) are sourced via each `pyproject.toml`'s `[tool.uv.sources]` block, with paths relative to that file.

## `software-dev-agentic` preview/demo knobs (per-rig)

The agentic flow can end a run with a **reachable preview** of the change,
surfaced as `po.preview_url` bead metadata so dashboard cards (orchestra) can
link it. Configure per-rig in `<rig>/.po-env` (the same KEY=VALUE file that
gates test layers):

| Var | Values | Effect |
|---|---|---|
| `PO_PREVIEW` | `local` \| `cloud` \| `off` | `off` (default): no preview work. `local`: the worker leaves its dev server running after the PR and reports `http://localhost:<port>`. `cloud`: the worker resolves a public URL via the rclaude shim (`rpreview <port>`). |
| `PO_DEMO_VIDEO` | `0` \| `1` | `1` asks the worker to record a short demo video for visual changes. Default `0`. |

Mechanism: the worker writes its URL to `<run_dir>/preview_url.txt`. Before the
critic runs, local preview mode resolves the listening process, proves its cwd
belongs to the worker's registered worktree, and proves that checkout's `HEAD`
equals the mechanically verified worker SHA. The proven served SHA and app root
are written to `verified-delivery.json`; stale or wrong-app previews fail closed.
On a critic pass the URL is also stamped as `po.preview_url` on the seed bead
(parallel to how core stamps `po.run_dir`). A backend-only change leaves no file
and stamps nothing. Public/cloud previews currently cannot provide this local
process proof and therefore must not claim a preview URL; use local strict proof
until a remote revision-attestation protocol is configured. Example `.po-env`:

```
PO_PREVIEW=local
PO_DEMO_VIDEO=1
```
