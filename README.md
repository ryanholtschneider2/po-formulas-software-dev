# po-formulas-software-dev

Software-development formula pack(s) for [`prefect-orchestration`](https://github.com/ryanholtschneider2/prefect-agent-orchestration).

This monorepo ships **two installable packages side-by-side**:

| Subdir | Distribution | What it provides |
|---|---|---|
| [`parent/`](./parent) | `po-formulas-software-dev` | The canonical pipeline: `software-dev-full` / `software-dev-fast` / `software-dev-edit`, `epic`, `epic-finalize`, `minimal-task`, `pre-pr-review`, `pr-writer`, `code-health-review`, `skill-evals`, `prompt`, `graph`, plus 18 role prompts. |
| [`wts/`](./wts) | `po-formulas-software-dev-wts` | Worktree-aware fork. Same flow bodies as `parent/` with `*-wts` EP keys; on flow entry each bead runs in its own `git worktree` at `<rig>.wt-<sanitized-id>/` so concurrent siblings can't conflict on shared source files. Adds `epic-finalize-wts` with an LLM spec-auditor + 27 pytest unit tests for the worktree machinery. |

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

`wts` was forked from `parent` to add git-worktree isolation (each bead gets its own working tree, with `.beads/` and `.planning/` symlinked back to the main rig so bd ops and run-dir artifacts stay authoritative in one place). The fork keeps the EP namespace clean — operators opt in by running `po run software-dev-full-wts` instead of `po run software-dev-full`. Once the worktree pattern soaks in across formulas the two will likely converge again, but for now keeping them as two distributions lets us iterate on `wts` without disturbing the canonical pack.

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
    └── tests/                 # test_worktree.py: 27 pytests passing
```

Both subdirs ship their own `pyproject.toml`, `tests/`, and overlay. Cross-subdir imports are forbidden — each package stands on its own. Shared deps (`prefect-orchestration`) are sourced via each `pyproject.toml`'s `[tool.uv.sources]` block, with paths relative to that file.
