"""Pack-shipped `po.commands` callables — non-orchestrated utility ops.

Each callable here is registered in this pack's `pyproject.toml` under
`[project.entry-points."po.commands"]` and dispatched via
`po <command> [--key=value ...]` (NOT `po run`). They skip Prefect
overhead per principle §4.
"""

from __future__ import annotations

import json
from pathlib import Path

from prefect_orchestration.run_lookup import resolve_run_dir, RunDirNotFound


def summarize_verdicts(issue_id: str) -> None:
    """Print a one-line summary per `verdicts/*.json` for an issue's run dir.

    Resolves the run dir via bd metadata (po.rig_path / po.run_dir) and
    walks `<run_dir>/verdicts/*.json` in name order, printing
    `<step> <verdict> <reason-first-line>` for each. Non-fatal if the
    `verdicts/` directory is absent or empty — prints a clear hint.
    """
    try:
        loc = resolve_run_dir(issue_id)
    except RunDirNotFound as exc:
        print(f"error: {exc}")
        raise SystemExit(2) from exc

    vdir = loc.run_dir / "verdicts"
    if not vdir.is_dir():
        print(f"no verdicts/ under {loc.run_dir}")
        return

    files = sorted(vdir.glob("*.json"))
    if not files:
        print(f"no verdict files under {vdir}")
        return

    for path in files:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            print(f"  {path.stem:24s}  (unreadable: {exc})")
            continue
        verdict = str(data.get("verdict", "?"))
        reason_raw = data.get("reason") or data.get("summary") or ""
        first = reason_raw.splitlines()[0] if reason_raw else ""
        print(f"  {path.stem:24s}  {verdict:12s}  {first}")


def planning_init(kind: str, slug: str, title: str | None = None) -> None:
    """Scaffold durable planning artifacts under `.planning/`.

    This is a low-level primitive, not the primary planning workflow.
    Prefer product-level planning (`beads-product`) or epic-level
    planning (`beads-epic-brainstorm`) when the work still needs
    collaborative decomposition.
    """
    plan_root = Path(".planning")
    display_title = title or slug.replace("-", " ").title()

    if kind == "product":
        base_dir = plan_root / "products" / slug
        files = {
            base_dir / f"{slug}-vision.md": _product_vision_template(display_title),
            base_dir / f"{slug}-epics.md": _product_epics_template(display_title),
        }
    elif kind == "epic":
        base_dir = plan_root / "epics" / slug
        files = {
            base_dir / f"{slug}-brainstorm.md": _epic_brainstorm_template(
                display_title
            ),
            base_dir / f"{slug}-design.md": _epic_design_template(display_title),
            base_dir / f"{slug}-epic-plan.md": _epic_plan_template(display_title),
            base_dir / f"{slug}-issues.md": _epic_issues_template(display_title),
        }
    else:
        print("error: kind must be 'product' or 'epic'")
        raise SystemExit(2)

    existing = [path for path in files if path.exists()]
    if existing:
        print("error: planning artifacts already exist:")
        for path in existing:
            print(f"  {path}")
        raise SystemExit(2)

    base_dir.mkdir(parents=True, exist_ok=True)
    for path, contents in files.items():
        path.write_text(contents)
        print(f"created {path}")


def _product_vision_template(title: str) -> str:
    return f"""# {title} Vision

> Primary workflow: collaborate at the product / roadmap layer first, then
> refine this file with `beads-product` before filing epics.

## Goal

## Users and Operators

## Problems to Solve

## Success Signals

## Constraints and Non-Goals
"""


def _product_epics_template(title: str) -> str:
    return f"""# {title} Epic Outline

> Use this as durable scaffolding, then run the higher-level product planning
> workflow to decompose into epics and dependencies.

## Candidate Epics

## Sequencing Notes

## Planning Workflow
- Preferred: `beads-product` for product/initiative decomposition into multiple epics
- Use `beads-epic-brainstorm` once a single epic needs deeper shaping
- Use `po planning-init` only to create the initial durable files when they do not exist yet

## Dispatch Guidance
- Keep work inline for trivial, single-file changes.
- Use local subagents for bounded research or narrow edits.
- Use `po run epic` or `po run software-dev-full` when the work needs durable artifacts, verifier gates, or parallel fan-out.
"""


def _epic_brainstorm_template(title: str) -> str:
    return f"""# {title} Brainstorm

> Preferred workflow: shape the epic collaboratively with `beads-epic-brainstorm`,
> then use this file as the durable artifact it writes into.

## Goal

## User Outcomes

## Open Questions

## Risks
"""


def _epic_design_template(title: str) -> str:
    return f"""# {title} Design

## Scope

## Existing Systems to Reuse

## Proposed Approach

## Boundaries and Non-Goals
"""


def _epic_plan_template(title: str) -> str:
    return f"""# {title} Epic Plan

## Features

## Dependencies

## Verification Strategy

## Dispatch Plan
"""


def _epic_issues_template(title: str) -> str:
    return f"""# {title} Issue Breakdown

## Parent Epic

## Child Beads

## Inline vs PO Routing
- Keep trivial edits inline.
- Use local subagents for bounded sidecar work.
- Stamp `metadata.formula` and dispatch the remaining implementation via `po run epic` or `po run software-dev-full`.
"""
