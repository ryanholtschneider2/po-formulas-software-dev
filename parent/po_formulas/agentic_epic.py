"""Prefect flow: ``agentic-epic``.

A single entry point that **plans, creates, and dispatches** a whole epic of
``software-dev-agentic`` runs from one high-level goal.

The goal lives in the epic bead's own description (``po run agentic-epic
--epic-id <id>``). One planner agent decomposes that goal into child issues; a
plan-critic reviews/iterates the decomposition (actor-critic on the *plan*);
then the flow **creates the child beads** (wired with ``bd dep`` order edges and
each stamped ``po.formula=software-dev-agentic``) and **fans them out** via the
existing ``graph_run`` engine — so every child runs through the normal
``software-dev-agentic`` worker→critic loop and ends at its own PR.

Pipeline::

    claim epic; read goal from `bd show <epic>`
      → plan loop in 1..plan_iter_cap:
            agent_step(agentic-epic-planner)      → writes <run_dir>/plan.json
            agent_step(agentic-epic-plan-critic)  → pass | fail (+ fix list)
            if critic == pass: break ; else feed fix list back to the planner
      → create one child bead per planned item (parent-child + blocks edges,
        po.formula=software-dev-agentic stamped)
      → graph_run(root_id=<epic>, formula=software-dev-agentic) — fan out
      → return summary (children created + dispatch result)

The flow never merges: each child leaves its own PR for human review, exactly
like a plain ``software-dev-agentic`` run.

``plan.json`` contract (the planner writes this; the flow reads it)::

    {
      "children": [
        {
          "key": "1",                       // child id becomes <epic>.1
          "title": "short imperative title",
          "description": "full bd body: what + why + acceptance criteria",
          "depends_on": ["2"]               // keys that must finish first (blocks)
        },
        ...
      ]
    }
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from prefect import flow, get_run_logger
from prefect_orchestration.agent_step import agent_step
from prefect_orchestration.beads_meta import claim_issue, close_issue, create_child_bead

from po_formulas.agentic import _bd_set_metadata, _read_text
from po_formulas.graph import graph_run
from po_formulas.software_dev import (
    _load_rig_env,
    _record_flow_outcome,
    _tag_flow_run_with_issue_id,
)

_AGENTS_DIR = Path(__file__).parent / "agents"
_PLAN_FILE = "plan.json"
_CHILD_FORMULA = "software-dev-agentic"


def _bd_show_description(epic_id: str, rig_path: Path) -> str:
    """Return the epic bead's description (the high-level goal). Best-effort."""
    proc = subprocess.run(
        ["bd", "show", epic_id, "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=str(rig_path),
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return ""
    try:
        row = json.loads(proc.stdout)
        # `bd show --json` may return a single object or a 1-element list.
        if isinstance(row, list):
            row = row[0] if row else {}
        return str(row.get("description") or row.get("body") or "")
    except (json.JSONDecodeError, IndexError, AttributeError):
        return ""


def _plan_revision_note(fix_list: str) -> str:
    """Retry guidance fed back to the planner after a plan-critic FAIL."""
    if not fix_list.strip():
        return ""
    return (
        "## Prior plan-critic verdict: FAIL\n\n"
        "Your previous decomposition was rejected. Revise plan.json to address "
        "every item below, then re-write the file and exit:\n\n" + fix_list.strip()
    )


def _parse_plan(run_dir: Path, max_children: int) -> list[dict[str, Any]]:
    """Read + validate ``<run_dir>/plan.json`` → ordered list of child specs.

    Raises ``ValueError`` with a clear message when the planner produced an
    unusable plan (missing file, malformed JSON, no children, duplicate or
    missing keys) so the flow fails loudly rather than dispatching garbage.
    """
    raw = _read_text(run_dir / _PLAN_FILE)
    if not raw.strip():
        raise ValueError(f"planner wrote no {_PLAN_FILE} in {run_dir}")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{_PLAN_FILE} is not valid JSON: {exc}") from exc

    children = data.get("children") if isinstance(data, dict) else None
    if not isinstance(children, list) or not children:
        raise ValueError(f"{_PLAN_FILE} has no non-empty 'children' array")
    if len(children) > max_children:
        raise ValueError(
            f"plan has {len(children)} children > max_children={max_children}; "
            "raise the cap or ask the planner for a coarser breakdown"
        )

    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for i, c in enumerate(children, 1):
        if not isinstance(c, dict):
            raise ValueError(f"children[{i}] is not an object")
        key = str(c.get("key") or i).strip()
        if not key or key in seen:
            raise ValueError(f"children[{i}] has a missing/duplicate key {key!r}")
        seen.add(key)
        title = str(c.get("title") or "").strip()
        description = str(c.get("description") or "").strip()
        if not title or not description:
            raise ValueError(f"child {key!r} is missing a title or description")
        depends_on = [str(d).strip() for d in (c.get("depends_on") or []) if str(d).strip()]
        out.append({"key": key, "title": title, "description": description, "depends_on": depends_on})

    # Validate dep references point at real sibling keys.
    keys = {c["key"] for c in out}
    for c in out:
        for d in c["depends_on"]:
            if d not in keys:
                raise ValueError(f"child {c['key']!r} depends_on unknown key {d!r}")
    return out


def _bd_dep_add(child_id: str, prereq_id: str, rig_path: Path) -> None:
    """``bd dep add <child> <prereq> --type=blocks`` — child depends on prereq.

    Direction per beads: ``bd dep add A B`` = "A depends on B" (A dependent,
    B prereq). graph_run's topo-sort reads these ``blocks`` edges to order the
    fan-out (a child waits for its prereqs via Prefect ``wait_for``).
    """
    subprocess.run(
        ["bd", "dep", "add", child_id, prereq_id, "--type=blocks"],
        check=False,
        capture_output=True,
        text=True,
        cwd=str(rig_path),
    )


def _create_children(
    epic_id: str, plan: list[dict[str, Any]], rig_path: Path, logger: Any
) -> list[str]:
    """Create + stamp + wire one bead per planned child. Returns child ids."""
    child_ids: dict[str, str] = {}  # plan-key → bead id
    for c in plan:
        child_id = f"{epic_id}.{c['key']}"
        create_child_bead(
            parent_id=epic_id,
            child_id=child_id,
            title=c["title"],
            description=c["description"],
            issue_type="task",
            rig_path=rig_path,
            priority=2,
        )
        # Stamp the per-child formula so graph_run routes each through the
        # agentic worker→critic loop (not the dispatcher's default).
        _bd_set_metadata(child_id, "po.formula", _CHILD_FORMULA, rig_path)
        child_ids[c["key"]] = child_id
        logger.info("agentic-epic: created %s (%s)", child_id, c["title"][:60])

    # Second pass: wire blocks edges now that every child exists.
    for c in plan:
        for dep_key in c["depends_on"]:
            _bd_dep_add(child_ids[c["key"]], child_ids[dep_key], rig_path)
    return list(child_ids.values())


@flow(name="agentic_epic", flow_run_name="{epic_id}", log_prints=True)
def agentic_epic(
    epic_id: str,
    rig: str,
    rig_path: str,
    pack_path: str | None = None,
    plan_iter_cap: int = 2,
    iter_cap: int = 2,
    max_children: int = 12,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Plan an epic from its goal, create stamped child beads, and fan them out.

    The epic bead's *description* is the goal. A planner decomposes it into
    children; a plan-critic gates the decomposition; the flow then creates the
    children (each stamped ``po.formula=software-dev-agentic``, wired with
    ``bd dep`` order edges) and dispatches them via ``graph_run`` so each runs
    the normal agentic worker→critic loop and ends at its own PR. Never merges.

    The signature carries the dispatcher-required ``(issue_id-equivalent)``
    triple as ``(epic_id, rig, rig_path)`` — ``epic_id`` is the root.
    """
    logger = get_run_logger()
    rig_path_p = Path(rig_path).expanduser().resolve()
    pack_path_p = Path(pack_path).expanduser().resolve() if pack_path else rig_path_p
    run_dir = rig_path_p / ".planning" / "agentic-epic" / epic_id
    run_dir.mkdir(parents=True, exist_ok=True)

    if not dry_run:
        claim_issue(epic_id, assignee=f"po-{os.getpid()}", rig_path=rig_path_p)

    _load_rig_env(rig_path_p)
    _tag_flow_run_with_issue_id(epic_id, logger)

    goal = _bd_show_description(epic_id, rig_path_p)
    (run_dir / "goal.md").write_text(goal or f"(no description on {epic_id})")

    try:
        # ── Phase 1: plan (actor-critic on the decomposition) ──────────────
        plan_verdict = ""
        fix_list = ""
        for iter_n in range(1, plan_iter_cap + 1):
            agent_step(
                agent_dir=_AGENTS_DIR / "agentic-epic-planner",
                task=_AGENTS_DIR / "agentic-epic-planner" / "task.md",
                seed_id=epic_id,
                rig_path=str(rig_path_p),
                run_dir=run_dir,
                step="epic-plan",
                iter_n=iter_n,
                ctx={
                    "iter": iter_n,
                    "pack_path": str(pack_path_p),
                    "plan_file": _PLAN_FILE,
                    "max_children": max_children,
                    "child_formula": _CHILD_FORMULA,
                    "revision_note": _plan_revision_note(fix_list),
                },
                verdict_keywords=("complete", "failed"),
                dry_run=dry_run,
            )
            review = agent_step(
                agent_dir=_AGENTS_DIR / "agentic-epic-plan-critic",
                task=_AGENTS_DIR / "agentic-epic-plan-critic" / "task.md",
                seed_id=epic_id,
                rig_path=str(rig_path_p),
                run_dir=run_dir,
                step="epic-plan-critic",
                iter_n=iter_n,
                ctx={"iter": iter_n, "plan_file": _PLAN_FILE},
                verdict_keywords=("pass", "fail"),
                dry_run=dry_run,
            )
            plan_verdict = "pass" if dry_run else review.verdict
            logger.info("agentic-epic: plan iter %s critic=%s", iter_n, plan_verdict)
            if plan_verdict == "pass":
                break
            fix_list = _read_text(run_dir / f"critique-epic-plan-iter-{iter_n}.md")

        if plan_verdict != "pass":
            raise RuntimeError(
                f"agentic-epic: plan did not pass critic after {plan_iter_cap} iter(s)"
            )

        # ── Phase 2: create the stamped child beads ────────────────────────
        if dry_run:
            logger.info("agentic-epic: dry-run — skipping bead creation + dispatch")
            return {"status": "dry-run", "epic_id": epic_id, "children": []}

        plan = _parse_plan(run_dir, max_children)
        child_ids = _create_children(epic_id, plan, rig_path_p, logger)
        logger.info("agentic-epic: created %d child bead(s): %s", len(child_ids), child_ids)

        # ── Phase 3: fan out (each child runs software-dev-agentic) ─────────
        dispatch = graph_run(
            root_id=epic_id,
            rig=rig,
            rig_path=str(rig_path_p),
            traverse="parent-child,blocks",
            formula=_CHILD_FORMULA,
            iter_cap=iter_cap,
            dry_run=False,
        )

        # The epic closes only when every discovered child has closed — that is
        # graph_run's contract. Mirror it: close the epic on a clean fan-out.
        close_issue(
            epic_id,
            notes=f"po agentic-epic complete: {len(child_ids)} child(ren) dispatched",
            rig_path=rig_path_p,
        )
        return {
            "status": "completed",
            "epic_id": epic_id,
            "children": child_ids,
            "dispatch": dispatch,
        }
    except Exception as exc:
        _record_flow_outcome(run_dir, exc, epic_id, str(rig_path_p))
        raise


__all__ = ["agentic_epic"]
