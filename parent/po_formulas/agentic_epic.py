"""Prefect flow: ``agentic-epic``.

A single entry point that **scopes, plans, and dispatches** a whole epic of
``software-dev-agentic`` runs from one high-level goal, landing it as **one
shared integration branch + one draft PR** (the po-formulas-software-dev-18m
executor).

The goal lives in the epic bead's own description (``po run agentic-epic
--epic-id <id>``). Four phases run in order:

1. **PRD** — one agent turns the goal into a short PRD (problem statement,
   acceptance criteria, the concrete surfaces/files the work touches), written to
   ``<run_dir>/prd.md``.
2. **Decomposition** — a planner decomposes the goal into child issues, each
   declaring the files it ``touches`` (the *coupling map*) plus any real
   ``depends_on`` output edge.
3. **Plan-critic loop** — a critic audits the *decomposition* (coverage, sizing,
   dependencies, and — critically — whether coupling is captured so the parallel
   lanes are conflict-safe). Actor-critic until pass or ``plan_iter_cap``.
4. **Dispatch** — the flow creates the child beads, wires ``blocks`` edges
   **only between coupled children** (shared files → serialized; disjoint files →
   left parallel), and fans them out via ``graph_run`` with ``shared_branch=True``:
   one ``epic/<id>`` integration branch, one draft PR, parallel where independent,
   stacked where coupled, integrate-on-pass, mark-ready at finalize.

The flow never merges to ``main``: the single epic PR is the deliverable.

``plan.json`` contract (the planner writes this; the flow reads it)::

    {
      "children": [
        {
          "key": "1",                       // child id becomes <epic>.1
          "title": "short imperative title",
          "description": "full bd body: what + why + acceptance criteria",
          "touches": ["parent/po_formulas/foo.py"],  // files this child edits
          "depends_on": ["2"],              // keys that must finish first (blocks)
          "formula": "software-dev-agentic" // optional per-child override
        },
        ...
      ]
    }

**Coupling → blocks edges.** Two children that share a file are *coupled*: they
must not run in parallel off the same epic tip or they collide on integration.
The flow derives the coupling map from each child's ``touches`` and auto-wires a
``blocks`` edge for any coupled pair the planner left unordered — so coupled
children always stack. Children with disjoint ``touches`` and no declared
``depends_on`` get **no** edge and fan out in parallel. This is the
"wire blocks only between coupled children" rule, enforced as transport.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from prefect import flow, get_run_logger
from prefect_orchestration.agent_step import agent_step
from prefect_orchestration.beads_meta import (
    claim_issue,
    close_issue,
    create_child_bead,
    list_epic_children,
)

from po_formulas import shared_branch as sb
from po_formulas.agentic import _bd_set_metadata, _read_text
from po_formulas.graph import graph_run
from po_formulas.software_dev import (
    _load_rig_env,
    _record_flow_outcome,
    _tag_flow_run_with_issue_id,
)

_AGENTS_DIR = Path(__file__).parent / "agents"
_PLAN_FILE = "plan.json"
_PRD_FILE = "prd.md"
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
        depends_on = [
            str(d).strip() for d in (c.get("depends_on") or []) if str(d).strip()
        ]
        raw_touches = c.get("touches") or []
        if not isinstance(raw_touches, list):
            raise ValueError(f"child {key!r} has a non-list 'touches'")
        touches = [_norm_path(str(t)) for t in raw_touches if str(t).strip()]
        formula = str(c.get("formula") or _CHILD_FORMULA).strip() or _CHILD_FORMULA
        out.append(
            {
                "key": key,
                "title": title,
                "description": description,
                "depends_on": depends_on,
                "touches": touches,
                "formula": formula,
            }
        )

    # Validate dep references point at real sibling keys.
    keys = {c["key"] for c in out}
    for c in out:
        for d in c["depends_on"]:
            if d not in keys:
                raise ValueError(f"child {c['key']!r} depends_on unknown key {d!r}")
    return out


def _norm_path(p: str) -> str:
    """Normalize a ``touches`` path for coupling comparison: strip whitespace and
    a leading ``./``, collapse backslashes. Cheap + deterministic so two children
    naming the same file the same way compare equal."""
    s = p.strip().replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    return s.rstrip("/")


def _coupling_map(plan: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Return the *coupled* child-key pairs — children that ``touch`` ≥1 common
    file and therefore must not run in parallel off the same epic tip.

    Pairs are ordered ``(lo, hi)`` by key for deterministic output and de-duped.
    A child with no ``touches`` couples with nothing (the planner declared no
    shared surface). This is the read side of ZFC: deterministic transport over
    the planner's judgment-authored ``touches``.
    """
    keyed = [(c["key"], set(c.get("touches") or [])) for c in plan]
    coupled: set[tuple[str, str]] = set()
    for i in range(len(keyed)):
        for j in range(i + 1, len(keyed)):
            (ka, ta), (kb, tb) = keyed[i], keyed[j]
            if ta and tb and (ta & tb):
                coupled.add(tuple(sorted((ka, kb))))  # type: ignore[arg-type]
    return sorted(coupled)


def _reachable(start: str, deps: dict[str, set[str]]) -> set[str]:
    """All keys reachable from ``start`` following ``depends_on`` edges (its
    transitive prerequisites). Used to test whether a coupled pair is already
    ordered before auto-adding a serialization edge."""
    seen: set[str] = set()
    stack = list(deps.get(start, set()))
    while stack:
        k = stack.pop()
        if k in seen:
            continue
        seen.add(k)
        stack.extend(deps.get(k, set()))
    return seen


def _blocks_edges(plan: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Resolve the full set of ``blocks`` edges to wire as ``(child_key, prereq_key)``.

    Two sources, unioned:

    * the planner's declared ``depends_on`` (real output dependencies), and
    * **coupling-derived** edges: for every coupled pair (sharing a file) that is
      not already ordered in either direction, add ``(hi, lo)`` so the
      higher-keyed child stacks after the lower — guaranteeing coupled children
      never run concurrently on the shared branch.

    Children that neither share a file nor declare a dependency get **no** edge —
    that is the "blocks only between coupled children" rule. Returns a sorted,
    de-duped edge list (child depends on prereq).
    """
    deps = {c["key"]: set(c["depends_on"]) for c in plan}
    edges: set[tuple[str, str]] = set()
    for child, prereqs in deps.items():
        for prereq in prereqs:
            edges.add((child, prereq))
    # Auto-serialize coupled-but-unordered pairs.
    for lo, hi in _coupling_map(plan):
        if lo in _reachable(hi, deps) or hi in _reachable(lo, deps):
            continue  # already ordered (directly or transitively) — leave as-is
        edges.add((hi, lo))
    return sorted(edges)


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
    """Create + stamp + wire one bead per planned child. Returns child ids.

    Each child is stamped with its own resolved formula (default
    ``software-dev-agentic``; the planner may set ``minimal-task`` on a trivial
    child — formula-per-bead-size). ``blocks`` edges come from
    :func:`_blocks_edges` — the planner's ``depends_on`` plus coupling-derived
    serialization for any coupled pair left unordered — so coupled children
    stack and independent children stay parallel.
    """
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
        # Stamp the per-child formula so graph_run routes each through the right
        # loop. `_bd_set_metadata` also adds a `formula:<name>` label, which is
        # the only per-bead stamp beads-rust honors (no arbitrary metadata).
        _bd_set_metadata(child_id, "po.formula", c["formula"], rig_path)
        child_ids[c["key"]] = child_id
        logger.info(
            "agentic-epic: created %s (%s) [%s]",
            child_id,
            c["title"][:60],
            c["formula"],
        )

    # Second pass: wire blocks edges now that every child exists — declared deps
    # plus coupling-derived serialization (blocks only between coupled children).
    for child_key, prereq_key in _blocks_edges(plan):
        _bd_dep_add(child_ids[child_key], child_ids[prereq_key], rig_path)
    return list(child_ids.values())


def _plan_lanes(plan: list[dict[str, Any]]) -> list[list[str]]:
    """Group child keys into dependency *levels* over the **resolved** blocks DAG.

    The DAG is :func:`_blocks_edges` (declared ``depends_on`` plus
    coupling-derived serialization), not raw ``depends_on``, so the lanes reflect
    the real conflict-safe shape — coupled children that the planner left
    unordered still show up stacked. Each returned inner list is a set of
    children with no unsatisfied prerequisites at that level — i.e. they run **in
    parallel**. Successive lists are **stacked** (a level waits for all earlier
    levels). Used by the dry-run to show the lanes without dispatching anything.
    """
    deps: dict[str, set[str]] = {c["key"]: set() for c in plan}
    for child_key, prereq_key in _blocks_edges(plan):
        deps[child_key].add(prereq_key)
    done: set[str] = set()
    lanes: list[list[str]] = []
    remaining = set(deps)
    while remaining:
        ready = sorted(k for k in remaining if deps[k] <= done)
        if not ready:  # cycle / dangling — bail rather than loop forever
            lanes.append(sorted(remaining))
            break
        lanes.append(ready)
        done |= set(ready)
        remaining -= set(ready)
    return lanes


def _dry_run_summary(
    epic_id: str,
    run_dir: Path,
    max_children: int,
    shared_branch: bool,
    logger: Any,
) -> dict[str, Any]:
    """Describe what a real run would do, without creating beads or dispatching.

    Surfaces the four phases (PRD artifact, decomposition + coupling map,
    plan-critic verdict already gated by the caller, and the shared-branch
    dispatch shape) so the operator sees the whole plan up front. In
    shared-branch mode it also surfaces the integration-branch name, the single
    draft PR that would be opened, and the parallel/serial lanes. Best-effort — a
    stub planner may not have written a parseable ``plan.json``. Satisfies the
    dry-run acceptance criterion (AC1).
    """
    logger.info("agentic-epic: dry-run — skipping bead creation + dispatch")
    out: dict[str, Any] = {"status": "dry-run", "epic_id": epic_id, "children": []}
    out["prd"] = _PRD_FILE if (run_dir / _PRD_FILE).exists() else None
    if shared_branch:
        out["shared_branch"] = True
        out["epic_branch"] = sb.epic_branch_name(epic_id)
        out["would_open_draft_pr"] = True
    try:
        plan = _parse_plan(run_dir, max_children)
    except ValueError:
        return out  # no parseable plan (common under StubBackend) — basic summary
    out["children"] = [f"{epic_id}.{c['key']}" for c in plan]
    coupled = _coupling_map(plan)
    out["coupling"] = [list(pair) for pair in coupled]
    out["blocks_edges"] = [list(e) for e in _blocks_edges(plan)]
    if coupled:
        logger.info("agentic-epic: dry-run — coupled (shared-file) pairs: %s", coupled)
    if shared_branch:
        lanes = _plan_lanes(plan)
        out["lanes"] = lanes
        for i, lane in enumerate(lanes, 1):
            kind = "parallel" if len(lane) > 1 else "single"
            logger.info("agentic-epic: dry-run lane %d (%s): %s", i, kind, lane)
        logger.info(
            "agentic-epic: dry-run — would create %s + open 1 PR at finalize, %d lane(s)",
            out["epic_branch"],
            len(lanes),
        )
    return out


def _integration_summary(dispatch: dict[str, Any]) -> str:
    """One markdown line per child — which landed on the branch vs. were dropped.

    Feeds the acceptance-critic so it knows which children's work is actually in
    the integrated diff. A dropped child (merge conflict or critic-fail) is the
    usual source of an unmet PRD acceptance criterion, so naming them explicitly
    keeps the critic from assuming the whole plan shipped.
    """
    results = (dispatch or {}).get("results") or {}
    if not results:
        return "(no per-child results reported)"
    lines: list[str] = []
    for cid, res in sorted(results.items()):
        if isinstance(res, dict):
            integ = res.get("integration") or {}
            if integ.get("merged"):
                lines.append(f"- `{cid}`: LANDED (integrated onto the branch)")
            elif integ.get("conflict"):
                lines.append(
                    f"- `{cid}`: DROPPED — merge conflict, work NOT on the branch "
                    f"({(integ.get('reason') or '')[:140]})"
                )
            else:
                detail = integ.get("reason") or res.get("status") or "unknown"
                lines.append(f"- `{cid}`: DROPPED — not integrated ({str(detail)[:140]})")
        else:
            lines.append(f"- `{cid}`: FAILED — {str(res)[:140]}")
    return "\n".join(lines)


# Epic-process beads (the PRD / plan / plan-critic agent_step iter beads) are
# parent-child dependents of the epic too, but they are not planned work — skip
# them when probing for an existing decomposition.
_EPIC_PROCESS_TITLE = re.compile(r"^epic-(prd|plan|plan-critic)\b", re.IGNORECASE)


def _existing_planned_children(epic_id: str, rig_path: Path) -> list[str]:
    """Open/in-progress *planned* children already linked under the epic.

    Idempotency probe. A prior dispatch that decomposed this epic leaves its
    planned children as non-closed parent-child dependents; we must NOT
    re-decompose on a repeat dispatch. This matters most on the **br** backend,
    where the deterministic ``{epic}.{key}`` child ids are rejected and children
    get fresh auto-ids — so a re-run mints a *duplicate* child set with nothing
    to collide against (the 2026-06-14 runaway: 43 concurrent flow runs from one
    epic re-decomposed across pulses). Excludes the transient epic-process beads.
    """
    try:
        nodes = list_epic_children(epic_id, mode="deps", rig_path=rig_path)
    except Exception:  # discovery is best-effort; never block dispatch on it
        return []
    return [
        str(n["id"])
        for n in nodes
        if not _EPIC_PROCESS_TITLE.match(str(n.get("title", "")).strip())
    ]


def _decompose_epic(
    epic_id: str,
    rig_path_p: Path,
    pack_path_p: Path,
    run_dir: Path,
    plan_iter_cap: int,
    max_children: int,
    shared_branch: bool,
    dry_run: bool,
    logger: Any,
) -> list[str] | dict[str, Any]:
    """Phases 1-3: PRD -> plan (actor-critic) -> create stamped children.

    Returns the created child ids, or (for ``dry_run``) the dry-run summary dict.
    Raises if the plan never passes the critic. Factored out of ``agentic_epic``
    so the flow can SKIP it on an idempotent re-run (children already exist).
    """
    # ── Phase 1: PRD (scope the goal before decomposing it) ─────────────
    agent_step(
        agent_dir=_AGENTS_DIR / "agentic-epic-prd",
        task=_AGENTS_DIR / "agentic-epic-prd" / "task.md",
        seed_id=epic_id,
        rig_path=str(rig_path_p),
        run_dir=run_dir,
        step="epic-prd",
        iter_n=1,
        ctx={
            "pack_path": str(pack_path_p),
            "prd_file": _PRD_FILE,
            "revision_note": "",
        },
        verdict_keywords=("complete", "failed"),
        dry_run=dry_run,
    )
    logger.info("agentic-epic: PRD written to %s", run_dir / _PRD_FILE)

    # ── Phase 2: plan (actor-critic on the decomposition) ──────────────
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
                "prd_file": _PRD_FILE,
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
            ctx={"iter": iter_n, "plan_file": _PLAN_FILE, "prd_file": _PRD_FILE},
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

    # ── Phase 3: create the stamped child beads ────────────────────────
    if dry_run:
        return _dry_run_summary(epic_id, run_dir, max_children, shared_branch, logger)

    plan = _parse_plan(run_dir, max_children)
    child_ids = _create_children(epic_id, plan, rig_path_p, logger)
    logger.info("agentic-epic: created %d child bead(s): %s", len(child_ids), child_ids)
    return child_ids


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
    shared_branch: bool = True,
    base_branch: str = "main",
    force_replan: bool = False,
) -> dict[str, Any]:
    """Scope, plan, and dispatch an epic from its goal as one shared-branch PR.

    The epic bead's *description* is the goal. Four phases run in order: a **PRD**
    author scopes the goal (problem / acceptance criteria / surfaces); a
    **planner** decomposes it into children (each declaring the files it
    ``touches`` + any real ``depends_on``); a **plan-critic** gates the
    decomposition (coverage, sizing, deps, and coupling); then the flow creates
    the children (each stamped with its resolved ``po.formula``, wired with
    ``blocks`` edges **only between coupled children**) and **dispatches**.

    The signature carries the dispatcher-required ``(issue_id-equivalent)``
    triple as ``(epic_id, rig, rig_path)`` — ``epic_id`` is the root.

    **Shared-branch mode** (``shared_branch=True``, the **default**): the whole
    epic lands as one integration branch ``epic/<epic-id>`` + **one PR opened at
    the end**. The flow cuts the epic branch off ``base_branch`` (no PR yet),
    then fans the children out via ``graph_run`` threading ``epic_branch`` /
    ``parent_epic_id`` to each child run — so each child branches off the
    **current epic tip** (parallel where independent, stacked along ``blocks``
    chains), is merged into the epic branch on critic-pass, and **never opens its
    own PR**. At finalize, once every child has integrated, an **epic
    acceptance-critic** reads the PRD against the *assembled* diff (the only check
    that does — per-child critics judged slices in isolation): on PASS the single
    PR is opened ready-for-review; on FAIL it is opened as a **draft** with the
    gap list, so the PR-sheriff can never auto-merge an incomplete epic. Pass
    ``shared_branch=False`` to fall back to the legacy per-child-PR path (N
    independent PRs, one per child).
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
        # ── Phases 1-3: decompose, UNLESS this epic was already decomposed ──
        # Idempotency guard (2026-06-14 runaway fix): if planned children already
        # exist under the epic, do NOT re-decompose — reuse them. Phase 4 below
        # re-dispatches whatever child set we end up with, so a repeat dispatch
        # produces exactly one child set instead of minting duplicates.
        reuse = (
            []
            if (dry_run or force_replan)
            else _existing_planned_children(epic_id, rig_path_p)
        )
        if reuse:
            logger.warning(
                "agentic-epic: %s already has %d planned child(ren) %s — skipping "
                "decomposition; reusing the existing set (idempotent re-run). Pass "
                "force_replan=True to re-decompose.",
                epic_id,
                len(reuse),
                reuse,
            )
            child_ids = reuse
        else:
            decomposed = _decompose_epic(
                epic_id,
                rig_path_p,
                pack_path_p,
                run_dir,
                plan_iter_cap,
                max_children,
                shared_branch,
                dry_run,
                logger,
            )
            if isinstance(decomposed, dict):
                return decomposed  # dry-run summary
            child_ids = decomposed

        # ── Phase 4a: shared-branch setup (one integration branch, NO PR yet) ──
        # The PR is opened at FINALIZE, only after every child has integrated, so
        # the PR-sheriff never sees (and can never auto-merge) an incomplete epic.
        epic_branch = ""
        pr_info: dict[str, Any] | None = None
        extra_kwargs: dict[str, Any] | None = None
        if shared_branch:
            epic_branch = sb.epic_branch_name(epic_id)
            branch_info = sb.create_integration_branch(
                rig_path_p, epic_id, base_branch=base_branch
            )
            extra_kwargs = {"epic_branch": epic_branch, "parent_epic_id": epic_id}
            logger.info(
                "agentic-epic: shared-branch %s (created=%s) — PR deferred to finalize",
                epic_branch,
                branch_info.get("created"),
            )

        # ── Phase 4b: fan out (each child runs software-dev-agentic) ─────────
        dispatch = graph_run(
            root_id=epic_id,
            rig=rig,
            rig_path=str(rig_path_p),
            traverse="parent-child,blocks",
            formula=_CHILD_FORMULA,
            iter_cap=iter_cap,
            dry_run=False,
            extra_formula_kwargs=extra_kwargs,
        )

        # ── Phase 4c: shared-branch finalize — open ONE ready PR now ─────────
        # Only when children actually integrated commits (else there is nothing
        # to review). Opened ready-for-review (not draft) so the sheriff acts on
        # a complete epic.
        if shared_branch:
            ahead = sb.commits_ahead(rig_path_p, base_branch, epic_branch)
            accept_verdict = "n/a"
            if ahead > 0:
                # Epic acceptance-critic: the ONLY check that reads the PRD against
                # the assembled diff. Per-child critics judged slices in isolation;
                # this catches dropped children, unmet acceptance criteria, and hard
                # PRD constraints (a required skill, an autonomy rule) the build
                # ignored. FAIL → open the PR as a DRAFT so the sheriff can't
                # auto-merge a gapped epic; the gap list lives in
                # critique-epic-acceptance.md and the PR body.
                if dry_run:
                    accept_verdict = "pass"
                else:
                    accept = agent_step(
                        agent_dir=_AGENTS_DIR / "agentic-epic-acceptance-critic",
                        task=_AGENTS_DIR / "agentic-epic-acceptance-critic" / "task.md",
                        seed_id=epic_id,
                        rig_path=str(rig_path_p),
                        run_dir=run_dir,
                        step="epic-acceptance-critic",
                        iter_n=1,
                        ctx={
                            "pack_path": str(pack_path_p),
                            "prd_file": _PRD_FILE,
                            "epic_branch": epic_branch,
                            "base_branch": base_branch,
                            "integration_summary": _integration_summary(dispatch),
                        },
                        verdict_keywords=("pass", "fail"),
                    )
                    accept_verdict = accept.verdict
                draft = accept_verdict != "pass"
                gap_note = (
                    ""
                    if accept_verdict == "pass"
                    else (
                        "\n\nThis epic did NOT fully satisfy its PRD — opened as a "
                        "DRAFT. Read `critique-epic-acceptance.md` in the run dir for "
                        "the per-criterion verdict + gap list before merging."
                    )
                )
                pr_info = dict(
                    sb.open_draft_pr(
                        rig_path_p,
                        branch=epic_branch,
                        base_branch=base_branch,
                        title=f"[epic] {epic_id}",
                        body=(goal or f"agentic-epic {epic_id}")
                        + f"\n\nShared-integration-branch epic. Children: {child_ids}"
                        + f"\n\n**Epic acceptance critic: {accept_verdict.upper()}**"
                        + gap_note,
                        draft=draft,
                    )
                )
                pr_info["acceptance_verdict"] = accept_verdict
            else:
                pr_info = {
                    "opened": False,
                    "url": "",
                    "reason": "no children integrated commits — no PR",
                    "acceptance_verdict": "n/a",
                }
            sb.cleanup_integration_worktree(rig_path_p, epic_id)
            logger.info(
                "agentic-epic: shared-branch finalize — PR=%s (acceptance=%s, %d commit(s) ahead)",
                pr_info.get("url") or f"(none: {pr_info.get('reason')})",
                accept_verdict,
                ahead,
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
            "shared_branch": shared_branch,
            "epic_branch": epic_branch or None,
            "pr": pr_info,
        }
    except Exception as exc:
        _record_flow_outcome(run_dir, exc, epic_id, str(rig_path_p))
        raise


__all__ = ["agentic_epic"]
