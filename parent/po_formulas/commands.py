"""Pack-shipped `po.commands` callables — non-orchestrated utility ops.

Each callable here is registered in this pack's `pyproject.toml` under
`[project.entry-points."po.commands"]` and dispatched via
`po <command> [--key=value ...]` (NOT `po run`). They skip Prefect
overhead per principle §4.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from prefect_orchestration.run_lookup import resolve_run_dir, RunDirNotFound


def summarize_verdicts(issue_id: str) -> None:
    """Print a one-line summary per `po.*` metadata key across an issue's iter beads.

    Walks every iter bead under the seed (`<seed>.<step>.iter<N>`) and
    prints one line per `po.<role>` metadata key found, sorted by step
    + iter index. Resolves the seed's rig_path via bd metadata so the
    `bd` shellout runs in the right rig.
    """
    try:
        loc = resolve_run_dir(issue_id)
    except RunDirNotFound as exc:
        print(f"error: {exc}")
        raise SystemExit(2) from exc

    rig_path = loc.rig_path
    proc = subprocess.run(
        ["bd", "list", "--parent", issue_id, "--all", "--json"],
        cwd=str(rig_path),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        print(f"no iter beads found for {issue_id} under {rig_path}")
        return

    try:
        rows = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        print(f"bd list returned unparseable JSON: {exc}")
        return

    iter_pat = re.compile(rf"^{re.escape(issue_id)}\.(.+?)\.iter(\d+)$")
    items = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        m = iter_pat.match(str(row.get("id", "")))
        if not m:
            continue
        metadata = row.get("metadata") or {}
        for key, value in metadata.items():
            if not str(key).startswith("po."):
                continue
            if key in {"po.run_dir", "po.rig_path"}:
                continue
            items.append((m.group(1), int(m.group(2)), key, value))

    if not items:
        print(f"no po.* verdict metadata found on iter beads of {issue_id}")
        return

    items.sort(key=lambda t: (t[0], t[1], t[2]))
    for step, iter_n, key, value in items:
        if isinstance(value, dict):
            verdict = str(
                value.get("verdict")
                or value.get("passed")
                or value.get("ralph_found_improvement")
                or "?"
            )
            reason = value.get("reason") or value.get("summary") or ""
            first = str(reason).splitlines()[0] if reason else ""
        else:
            verdict = str(value)
            first = ""
        label = f"{step}-iter-{iter_n} {key.removeprefix('po.')}"
        print(f"  {label:38s}  {verdict:12s}  {first}")


def write_verdict(
    bead_id: str, name: str, payload: str, rig_path: str | None = None
) -> None:
    """Write a role's structured verdict onto its iter bead, backend-agnostically.

    Routes through ``prefect_orchestration.beads_backend.write_verdict``, which
    picks the write form from the rig's beads backend (``resolve_backend``):

      - **dolt** — ``bd update <id> --set-metadata po.<name>=<json>``
      - **br** — ``br comments add <id> 'po-verdict:<name>:<json>'``

    Role prompts call ``po write-verdict ...`` instead of hardcoding
    ``bd update --metadata`` so a verdict lands on either backend without the
    role knowing which one the rig runs. The orchestrator reads it back via the
    same seam (``parsing.read_bead_verdict``).

    Args:
        bead_id: the iter bead to stamp (e.g. ``<seed>.triage.iter1``).
        name: verdict key *without* the ``po.`` prefix (``triage``, ``ralph``,
            ``full_test_gate``, ``code_health``).
        payload: a JSON object string — the verdict body (the value that used
            to sit under the ``po.<name>`` key in the old ``--metadata`` form).
        rig_path: rig root for backend resolution + the shellout cwd; defaults
            to the current directory.

    Prints a one-line confirmation naming the resolved backend on success;
    exits non-zero (with a diagnostic) on bad JSON or a failed write.
    """
    # Lazy import: a rig running an older core (pre-`beads_backend`) can still
    # load this module for the other commands; only `write-verdict` needs it.
    try:
        from prefect_orchestration.beads_backend import (
            resolve_backend,
            write_verdict as _backend_write_verdict,
        )
    except ImportError as exc:
        print(
            "error: this core lacks prefect_orchestration.beads_backend "
            f"(need the backend-agnostic verdict seam): {exc}"
        )
        raise SystemExit(2) from exc

    rp = rig_path or "."
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        print(f"error: --payload is not valid JSON: {exc}")
        raise SystemExit(2) from exc

    backend = resolve_backend(rp)
    try:
        _backend_write_verdict(bead_id, name, parsed, backend=backend, rig_path=rp)
    except (RuntimeError, NotImplementedError) as exc:
        print(f"error: write_verdict for {bead_id}.{name} failed ({backend}): {exc}")
        raise SystemExit(1) from exc
    print(f"wrote po.{name} verdict on {bead_id} via {backend}")


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
