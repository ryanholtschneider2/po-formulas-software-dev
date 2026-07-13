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
2. **Decomposition** — a planner decomposes the goal into child issues and
   **owns the ordering**: it declares ``depends_on`` edges so that children which
   edit the same surface are sequenced (the later one resumes from the earlier
   one's merged code) and independent children are left parallel. ``touches`` is
   recorded as informational context for the critic — nothing is inferred from it.
3. **Plan-critic loop** — a critic audits the *decomposition* (coverage, sizing,
   and whether the planner's ``depends_on`` correctly sequences same-surface
   children so the parallel lanes are conflict-safe). Actor-critic until pass or
   ``plan_iter_cap``.
4. **Dispatch** — the flow creates the child beads, records the planner's
   ``depends_on`` as ``blocks`` edges (pure transport — no dep is inferred), and
   fans them out via ``graph_run`` with ``shared_branch=True``: one ``epic/<id>``
   integration branch, children parallel where independent and stacked where the
   planner sequenced them, **each child merges its own branch back into the epic
   branch after it passes its critic**, and the epic PR is opened at finalize.

The flow never merges to ``main``: the single epic PR is the deliverable. The
flow itself runs no ``git merge`` — integration is the child agent's job (see
``agentic.py``); the only judgment about ordering lives in the planning agent.

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

**Ordering is the planner's judgment, not the flow's.** Two children that edit
the same surface must be sequenced (a ``depends_on``) so the second resumes from
the first's merged code instead of colliding. That decision belongs to the
planning agent — like a beads-epic / plan-epic skill — and the plan-critic checks
it. The flow only *records* the planner's ``depends_on`` as ``blocks`` edges;
it infers nothing from ``touches``. A merge conflict at integration therefore
means the planner mis-ordered (it should have sequenced two same-surface
children) — the fix is a dep, not deterministic conflict-resolution code.
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
from po_formulas import delivery_truth, verified_delivery
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
_ACCEPTANCE_MANIFEST_FILE = "acceptance-manifest.json"
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
    """Tidy a planner-declared ``touches`` path (strip whitespace + leading
    ``./``, collapse backslashes) so the recorded surface list is clean for the
    plan-critic to read. Informational only — nothing is inferred from it."""
    s = p.strip().replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    return s.rstrip("/")


def _blocks_edges(plan: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """The ``blocks`` edges to wire, as ``(child_key, prereq_key)`` — exactly the
    planner's declared ``depends_on``, nothing inferred.

    Coupling/ordering is the **planning agent's** judgment: it decides which
    children must be sequenced (e.g. two children editing the same file get a
    ``depends_on`` so the second resumes from the first's merged code) and which
    are independent. The flow only *records* those decisions as bd ``blocks``
    edges — pure transport. No dependency is derived from ``touches`` here; the
    plan-critic, not deterministic code, is what checks the planner got it right.
    """
    edges: set[tuple[str, str]] = set()
    for c in plan:
        for prereq in c.get("depends_on") or []:
            edges.add((c["key"], prereq))
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
    child — formula-per-bead-size). ``blocks`` edges are exactly the planner's
    declared ``depends_on`` (:func:`_blocks_edges`) — the planning agent owns
    which children are sequenced vs parallel; the flow only records it.
    """
    child_ids: dict[str, str] = {}  # plan-key → bead id
    for c in plan:
        requested_id = f"{epic_id}.{c['key']}"
        # `create_child_bead` honors `requested_id` on dolt, but br mints its
        # own id (no `--id` flag) and returns it. Use the RETURNED id for every
        # downstream op — stamping `po.formula` or building the deps map against
        # the requested dotted id targets a phantom bead on br (the real bead
        # exists under the br-assigned id), silently breaking graph_run dispatch.
        actual_id = create_child_bead(
            parent_id=epic_id,
            child_id=requested_id,
            title=c["title"],
            description=c["description"],
            issue_type="task",
            rig_path=rig_path,
            priority=2,
        )
        # Stamp the per-child formula so graph_run routes each through the right
        # loop. `_bd_set_metadata` also adds a `formula:<name>` label, which is
        # the only per-bead stamp beads-rust honors (no arbitrary metadata).
        _bd_set_metadata(actual_id, "po.formula", c["formula"], rig_path)
        child_ids[c["key"]] = actual_id
        logger.info(
            "agentic-epic: created %s (%s) [%s]",
            actual_id,
            c["title"][:60],
            c["formula"],
        )

    # Second pass: wire the planner's declared deps as blocks edges now that
    # every child exists (transport — the planner decided the ordering).
    for child_key, prereq_key in _blocks_edges(plan):
        _bd_dep_add(child_ids[child_key], child_ids[prereq_key], rig_path)
    return list(child_ids.values())


def _plan_lanes(plan: list[dict[str, Any]]) -> list[list[str]]:
    """Group child keys into dependency *levels* over the planner's dep DAG.

    The DAG is :func:`_blocks_edges` (the planner's declared ``depends_on``). Each
    returned inner list is a set of children with no unsatisfied prerequisites at
    that level — i.e. they run **in parallel**. Successive lists are **stacked** (a
    level waits for all earlier levels). Used by the dry-run to show the lanes
    without dispatching anything.
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

    Surfaces the phases (PRD artifact, decomposition + the planner's declared dep
    edges, plan-critic verdict already gated by the caller, and the shared-branch
    dispatch shape) so the operator sees the whole plan up front. In shared-branch
    mode it also surfaces the integration-branch name and the parallel/serial
    lanes. Best-effort — a stub planner may not have written a parseable
    ``plan.json``. Satisfies the dry-run acceptance criterion (AC1).
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
    out["blocks_edges"] = [list(e) for e in _blocks_edges(plan)]
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
        base = "(no per-child results reported)"
    else:
        base = ""
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
                lines.append(
                    f"- `{cid}`: DROPPED — not integrated ({str(detail)[:140]})"
                )
        else:
            lines.append(f"- `{cid}`: FAILED — {str(res)[:140]}")
    acceptance_fix = (dispatch or {}).get("acceptance_fix") or {}
    if isinstance(acceptance_fix, dict):
        fix_id = acceptance_fix.get("id")
        fix_dispatch = acceptance_fix.get("dispatch") or {}
        fix_results = (
            fix_dispatch.get("results") if isinstance(fix_dispatch, dict) else {}
        )
        if fix_id and isinstance(fix_results, dict):
            fix_res = fix_results.get(fix_id)
            if isinstance(fix_res, dict) and (fix_res.get("integration") or {}).get(
                "merged"
            ):
                lines.append(f"- `{fix_id}`: LANDED (acceptance-fix child)")
            elif fix_res is not None:
                lines.append(f"- `{fix_id}`: DROPPED — acceptance-fix did not land")
            else:
                lines.append(f"- `{fix_id}`: DROPPED — acceptance-fix result missing")
    if base and not lines:
        return base
    return "\n".join(lines)


def _build_acceptance_manifest(
    *,
    child_ids: list[str],
    dispatch: dict[str, Any],
    rig_path: Path,
    pack_path: Path,
    run_dir: Path,
    base_branch: str,
    epic_branch: str,
) -> dict[str, Any]:
    """Persist mechanical delivery facts for assembled-epic judgment.

    This deliberately does not decide whether evidence is sufficient for a PRD
    criterion. It proves identities, ancestry, artifact presence, and terminal
    state; the acceptance model judges product coverage and broken seams.
    """
    assembled_sha = delivery_truth.revision(pack_path, epic_branch)
    base_sha = delivery_truth.revision(pack_path, base_branch)
    delivery_truth.require_ancestor(
        pack_path,
        base_sha,
        assembled_sha,
        label="assembled epic base mismatch",
    )
    results = (dispatch or {}).get("results") or {}
    blocking_facts: list[str] = []
    children: list[dict[str, Any]] = []
    for child_id in child_ids:
        result = results.get(child_id)
        integration = result.get("integration") if isinstance(result, dict) else {}
        merged = bool(isinstance(integration, dict) and integration.get("merged"))
        artifact_path = (
            rig_path
            / ".planning"
            / "software-dev-agentic"
            / child_id
            / verified_delivery.ARTIFACT_NAME
        )
        artifact_error = ""
        artifact: dict[str, Any] | None = None
        raw_artifact = (
            result.get("verified_delivery") if isinstance(result, dict) else None
        )
        try:
            if isinstance(raw_artifact, dict):
                artifact = verified_delivery.normalize(raw_artifact)
            elif artifact_path.is_file():
                artifact = verified_delivery.read(artifact_path.parent)
            else:
                artifact_error = "verified-delivery artifact missing"
        except ValueError as exc:
            artifact_error = str(exc)

        ancestry_proven = False
        head_sha = (
            str((artifact or {}).get("revisions", {}).get("head") or "")
            if artifact
            else ""
        )
        if merged and head_sha:
            try:
                delivery_truth.require_ancestor(
                    pack_path,
                    head_sha,
                    assembled_sha,
                    label=f"child {child_id} is not integrated",
                )
                ancestry_proven = True
            except delivery_truth.DeliveryTruthError as exc:
                artifact_error = str(exc)

        terminal_state = (
            str((artifact or {}).get("terminal", {}).get("state") or "")
            if artifact
            else ""
        )
        facts: list[str] = []
        if result is None:
            facts.append("dispatch result missing")
        if not merged:
            facts.append("child not integrated")
        if artifact_error:
            facts.append(artifact_error)
        if artifact and terminal_state != "completed":
            facts.append(f"delivery terminal state is {terminal_state or 'missing'}")
        if artifact and not head_sha:
            facts.append("delivery head revision missing")
        if merged and head_sha and not ancestry_proven and not artifact_error:
            facts.append("child ancestry not proven")
        blocking_facts.extend(f"{child_id}: {fact}" for fact in facts)
        children.append(
            {
                "id": child_id,
                "dispatch_present": result is not None,
                "integrated": merged,
                "ancestry_proven": ancestry_proven,
                "artifact_path": str(artifact_path),
                "artifact": artifact,
                "blocking_facts": facts,
            }
        )

    manifest = {
        "schema": "po.epic-acceptance-manifest",
        "version": 1,
        "base_branch": base_branch,
        "base_sha": base_sha,
        "epic_branch": epic_branch,
        "assembled_sha": assembled_sha,
        "children": children,
        "blocking_facts": blocking_facts,
    }
    (run_dir / _ACCEPTANCE_MANIFEST_FILE).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )
    return manifest


def _acceptance_critique(run_dir: Path) -> str:
    """Read the latest epic acceptance critique for a fixer bead body."""
    critique = _read_text(run_dir / "critique-epic-acceptance.md").strip()
    return critique or "(acceptance critic did not write a critique file)"


def _create_acceptance_fix_bead(
    *,
    epic_id: str,
    rig_path: Path,
    run_dir: Path,
    epic_branch: str,
    base_branch: str,
    dispatch: dict[str, Any],
    fix_n: int,
    logger: Any,
) -> str:
    """Create the follow-up child that repairs a failed epic acceptance gate."""
    requested_id = f"{epic_id}.acceptance-fix-{fix_n}"
    description = (
        f"Fix the remaining acceptance gaps for epic `{epic_id}`.\n\n"
        "The shared-branch epic acceptance critic failed after the planned "
        "children ran. Work directly against the existing integration branch "
        f"`{epic_branch}` and make the smallest complete fix needed for the "
        "whole epic to satisfy its PRD.\n\n"
        "## Inputs\n\n"
        f"- PRD: `{run_dir / _PRD_FILE}`\n"
        f"- Acceptance critique: `{run_dir / 'critique-epic-acceptance.md'}`\n"
        f"- Pinned acceptance manifest: `{run_dir / _ACCEPTANCE_MANIFEST_FILE}`\n"
        f"- Base branch: `{base_branch}`\n"
        f"- Integration branch: `{epic_branch}`\n\n"
        "## Current child integration state\n\n"
        f"{_integration_summary(dispatch)}\n\n"
        "## Critic gaps to fix\n\n"
        f"{_acceptance_critique(run_dir)}\n\n"
        "## Acceptance criteria\n\n"
        "- Every UNMET item in the epic acceptance critique is addressed by "
        "integrated code on the epic branch.\n"
        "- Any child work that was dropped or failed but is required by the PRD "
        "is implemented or recovered.\n"
        "- Relevant tests, lint, or smoke checks are run and recorded in the "
        "worker artifacts.\n"
        "- Do not open a child PR; the agentic-epic flow owns the single epic PR."
    )
    actual_id = create_child_bead(
        parent_id=epic_id,
        child_id=requested_id,
        title=f"Fix acceptance gaps for {epic_id}",
        description=description,
        issue_type="task",
        rig_path=rig_path,
        priority=1,
    )
    _bd_set_metadata(actual_id, "po.formula", _CHILD_FORMULA, rig_path)
    _bd_set_metadata(actual_id, "agentic_epic.acceptance_fix_for", epic_id, rig_path)
    logger.warning(
        "agentic-epic: acceptance failed; filed fixer child %s for %s",
        actual_id,
        epic_id,
    )
    return actual_id


def _run_epic_acceptance_critic(
    *,
    epic_id: str,
    rig_path: Path,
    run_dir: Path,
    pack_path: Path,
    epic_branch: str,
    base_branch: str,
    child_ids: list[str],
    dispatch: dict[str, Any],
    iter_n: int,
    dry_run: bool,
) -> str:
    """Return pass/fail for the assembled epic branch."""
    if dry_run:
        return "pass"
    manifest = _build_acceptance_manifest(
        child_ids=child_ids,
        dispatch=dispatch,
        rig_path=rig_path,
        pack_path=pack_path,
        run_dir=run_dir,
        base_branch=base_branch,
        epic_branch=epic_branch,
    )
    try:
        integration_path = delivery_truth.worktree_for_branch(pack_path, epic_branch)
    except delivery_truth.DeliveryTruthError:
        integration_path = pack_path
    accept = agent_step(
        agent_dir=_AGENTS_DIR / "agentic-epic-acceptance-critic",
        task=_AGENTS_DIR / "agentic-epic-acceptance-critic" / "task.md",
        seed_id=epic_id,
        rig_path=str(rig_path),
        run_dir=run_dir,
        step="epic-acceptance-critic",
        iter_n=iter_n,
        ctx={
            "pack_path": str(pack_path),
            "prd_file": _PRD_FILE,
            "epic_branch": epic_branch,
            "base_branch": base_branch,
            "integration_summary": _integration_summary(dispatch),
            "acceptance_manifest": str(run_dir / _ACCEPTANCE_MANIFEST_FILE),
            "assembled_sha": manifest["assembled_sha"],
            "integration_path": str(integration_path),
        },
        verdict_keywords=("pass", "fail"),
    )
    if manifest["blocking_facts"]:
        return "fail"
    return accept.verdict


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
    iter_cap: int = 4,
    max_children: int = 12,
    dry_run: bool = False,
    shared_branch: bool = True,
    base_branch: str = "main",
    force_replan: bool = False,
    acceptance_fix_cap: int = 2,
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
        # Always thread base_branch to children: in non-shared mode it keeps each
        # child's worktree + PR off ``main`` (against the epic's base branch); in
        # shared mode the per-child branch_directive takes over, so it is merely
        # cosmetic there but harmless.
        extra_kwargs: dict[str, Any] = {"base_branch": base_branch}
        if shared_branch:
            epic_branch = sb.epic_branch_name(epic_id)
            branch_info = sb.create_integration_branch(
                pack_path_p, epic_id, base_branch=base_branch
            )
            extra_kwargs |= {"epic_branch": epic_branch, "parent_epic_id": epic_id}
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
        acceptance_fix_id: str | None = None
        acceptance_fix_dispatch: dict[str, Any] | None = None
        acceptance_fix_ids: list[str] = []
        if shared_branch:
            ahead = sb.commits_ahead(pack_path_p, base_branch, epic_branch)
            accept_verdict = "n/a"
            if ahead > 0:
                # Epic acceptance-critic: the ONLY check that reads the PRD against
                # the assembled diff. Per-child critics judged slices in isolation;
                # this catches dropped children, unmet acceptance criteria, and hard
                # PRD constraints (a required skill, an autonomy rule) the build
                # ignored. FAIL → file and run one software-dev-agentic fixer
                # child against the shared branch, then re-run acceptance. A
                # second FAIL opens a DRAFT and leaves the epic open.
                accept_verdict = _run_epic_acceptance_critic(
                    epic_id=epic_id,
                    rig_path=rig_path_p,
                    run_dir=run_dir,
                    pack_path=pack_path_p,
                    epic_branch=epic_branch,
                    base_branch=base_branch,
                    child_ids=child_ids,
                    dispatch=dispatch,
                    iter_n=1,
                    dry_run=dry_run,
                )
                fix_n = 0
                while (
                    accept_verdict != "pass"
                    and not dry_run
                    and fix_n < acceptance_fix_cap
                ):
                    fix_n += 1
                    acceptance_fix_id = _create_acceptance_fix_bead(
                        epic_id=epic_id,
                        rig_path=rig_path_p,
                        run_dir=run_dir,
                        epic_branch=epic_branch,
                        base_branch=base_branch,
                        dispatch=dispatch,
                        fix_n=fix_n,
                        logger=logger,
                    )
                    acceptance_fix_ids.append(acceptance_fix_id)
                    acceptance_fix_dispatch = graph_run(
                        root_id=acceptance_fix_id,
                        rig=rig,
                        rig_path=str(rig_path_p),
                        traverse="parent-child,blocks",
                        formula=_CHILD_FORMULA,
                        iter_cap=iter_cap,
                        dry_run=False,
                        root_as_node=True,
                        extra_formula_kwargs=extra_kwargs,
                    )
                    dispatch = {
                        **dispatch,
                        "results": {
                            **(dispatch.get("results") or {}),
                            **(acceptance_fix_dispatch.get("results") or {}),
                        },
                        "acceptance_fix": {
                            "id": acceptance_fix_id,
                            "dispatch": acceptance_fix_dispatch,
                        },
                    }
                    ahead = sb.commits_ahead(pack_path_p, base_branch, epic_branch)
                    if ahead > 0:
                        accept_verdict = _run_epic_acceptance_critic(
                            epic_id=epic_id,
                            rig_path=rig_path_p,
                            run_dir=run_dir,
                            pack_path=pack_path_p,
                            epic_branch=epic_branch,
                            base_branch=base_branch,
                            child_ids=[*child_ids, *acceptance_fix_ids],
                            dispatch=dispatch,
                            iter_n=fix_n + 1,
                            dry_run=False,
                        )
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
                        pack_path_p,
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
                if acceptance_fix_id:
                    pr_info["acceptance_fix_id"] = acceptance_fix_id
            else:
                pr_info = {
                    "opened": False,
                    "url": "",
                    "reason": "no children integrated commits — no PR",
                    "acceptance_verdict": "n/a",
                }
            sb.cleanup_integration_worktree(pack_path_p, epic_id)
            logger.info(
                "agentic-epic: shared-branch finalize — PR=%s (acceptance=%s, %d commit(s) ahead)",
                pr_info.get("url") or f"(none: {pr_info.get('reason')})",
                accept_verdict,
                ahead,
            )

        # Legacy fan-out closes on graph completion. Shared-branch epics close
        # only when final assembled acceptance passes; otherwise the epic stays
        # open with the fixer/draft PR artifacts attached for follow-up.
        close_epic = not shared_branch or (
            bool(pr_info and pr_info.get("opened"))
            and pr_info.get("acceptance_verdict") == "pass"
        )
        if close_epic:
            close_issue(
                epic_id,
                notes=f"po agentic-epic complete: {len(child_ids)} child(ren) dispatched",
                rig_path=rig_path_p,
            )
        else:
            logger.warning(
                "agentic-epic: leaving %s open (shared_branch=%s, pr=%s)",
                epic_id,
                shared_branch,
                pr_info,
            )
        return {
            "status": "completed" if close_epic else "incomplete",
            "epic_id": epic_id,
            "children": child_ids,
            "dispatch": dispatch,
            "shared_branch": shared_branch,
            "epic_branch": epic_branch or None,
            "pr": pr_info,
            "acceptance_fix_id": acceptance_fix_id,
            "acceptance_fix_ids": acceptance_fix_ids,
            "acceptance_fix_dispatch": acceptance_fix_dispatch,
        }
    except Exception as exc:
        _record_flow_outcome(run_dir, exc, epic_id, str(rig_path_p))
        raise


__all__ = ["agentic_epic"]
