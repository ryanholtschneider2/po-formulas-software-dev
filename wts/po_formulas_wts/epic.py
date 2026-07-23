"""Prefect flow: `epic_run`.

Thin wrapper over `graph_run` (prefect-orchestration-uc0): delegates to
edge-driven sub-graph traversal with `traverse=("parent-child", "blocks")`.

Discovery is controlled by two parameters
(prefect-orchestration-h5s):

- ``discover``: which mode drives child discovery —
  ``parent-child`` (default; walk ONLY parent-child edges to collect
  the child set), ``ids`` (dot-suffix probe; legacy naming),
  ``deps`` (`bd dep` graph walk over parent-child + blocks edges), or
  ``both`` (deps order first, then dot-suffix-only ids appended).
- ``child_ids``: comma-separated explicit override that bypasses
  discovery entirely. Each listed id must exist and be open; the DAG
  is built from `bd dep` edges *between* the listed ids (out-of-set
  blockers are dropped).

Why ``parent-child`` is the default (po-formulas-software-dev-e9s):
``deps`` / ``both`` walk `blocks` edges *up* to widen the discovered
set, so dispatching an epic that another epic ``blocks``-depends on
pulls that sibling epic (and its children) into the run — inverting
the intended dependency. ``parent-child`` collects children via the
parent-child relationship only; `blocks` edges are still honoured for
topo-ordering *within* the discovered set (``list_subgraph`` always
builds the blocks sub-DAG regardless of `traverse`), they just no
longer WIDEN it. Operators who genuinely want the blocks-aware union
opt in with ``discover=both``.

The dot-suffix fallback in older builds was implicit ("if deps returned
nothing, probe ids"). It is now explicit: pick `discover=both` to keep
the old union behaviour, `discover=deps` to opt out of the legacy
probe, or `discover=ids` to opt out of the bd-dep walk.

Concurrency is a deploy-time concern:

    prefect work-pool create po --type process --concurrency-limit 4
    prefect concurrency-limit create critic 2
    prefect concurrency-limit create builder 3
"""

from __future__ import annotations

from typing import Any

from prefect import flow, get_run_logger

from prefect_orchestration.beads_meta import (
    VALID_DISCOVER_MODES,
    collect_explicit_children,
    list_epic_children,
    list_subgraph,
)

from po_formulas_wts.graph import (
    _check_formula_signature,
    _dispatch_nodes,
    _resolve_formula,
    _tag_root_run,
)

# Discovery mode that collects children via parent-child edges ONLY.
# `blocks` edges are still used to topo-order the discovered set (see
# `_discover_children`), they just don't WIDEN it into sibling epics.
# This is the safe default — see module docstring + po-formulas-software-dev-e9s.
PARENT_CHILD_DISCOVER = "parent-child"

# `discover` values this flow accepts: the parent-child-only default plus
# the core `list_epic_children` modes (ids / deps / both).
VALID_EPIC_DISCOVER_MODES: tuple[str, ...] = (
    PARENT_CHILD_DISCOVER,
    *VALID_DISCOVER_MODES,
)


def _discover_children(
    epic_id: str, discover: str, rig_path: str
) -> list[dict[str, Any]]:
    """Resolve an epic's child nodes for the given ``discover`` mode.

    ``parent-child`` walks only parent-child edges via `list_subgraph`
    (which still populates `block_deps` for topo-ordering within the
    collected set); every other mode delegates to `list_epic_children`.
    """
    if discover == PARENT_CHILD_DISCOVER:
        return list_subgraph(
            epic_id,
            traverse=(PARENT_CHILD_DISCOVER,),
            include_closed=False,
            include_root=False,
            rig_path=rig_path,
        )
    return list_epic_children(epic_id, mode=discover, rig_path=rig_path)


def _legacy_dot_suffix_children(epic_id: str) -> list[dict[str, Any]]:
    """Adapter kept for `tests/test_epic_legacy_dot_suffix.py`.

    Pre-h5s the helper did its own legacy→graph shape adaptation; today
    `list_epic_children(mode="ids")` returns the graph shape directly,
    so this is a one-line shim that exists only to keep the test
    boundary stable.
    """
    return list_epic_children(epic_id, mode="ids")


def _parse_child_ids(raw: str) -> list[str]:
    """Split ``"a, b ,c"`` → ``["a", "b", "c"]``; drop empties.

    Validation (existence, dup detection, closed filtering) lives in
    `collect_explicit_children` so the same checks apply to any caller.
    """
    return [tok.strip() for tok in raw.split(",") if tok.strip()]


@flow(name="epic_run_wts", flow_run_name="{epic_id}", log_prints=True)
def epic_run(
    epic_id: str,
    rig: str,
    rig_path: str,
    iter_cap: int = 3,
    plan_iter_cap: int = 2,
    verify_iter_cap: int = 3,
    ralph_iter_cap: int = 3,
    dry_run: bool = False,
    max_issues: int | None = None,
    discover: str = PARENT_CHILD_DISCOVER,
    child_ids: str | None = None,
    parent_epic_worktree: str | None = None,
    parent_epic_branch: str | None = None,
    parent_epic_id: str | None = None,
    merge_target_branch: str = "main",
) -> dict[str, Any]:
    """Fan out an epic's open children as concurrent software_dev_full runs.

    Args:
        epic_id: beads epic ID (e.g. "sr-8yu" or
            "prefect-orchestration-3cu").
        max_issues: if set, only submit the first N topo-sorted children.
            Useful for testing "one issue at a time" before unleashing.
        discover: which discovery mode to use — ``parent-child``
            (parent-child edges only; default), ``ids`` (dot-suffix
            probe), ``deps`` (`bd dep` graph walk over parent-child +
            blocks), or ``both`` (union). Ignored when ``child_ids`` is
            supplied. Default is ``parent-child`` so `blocks` edges
            don't widen discovery into sibling epics; pass
            ``discover=both`` for the blocks-aware union.
        child_ids: comma-separated explicit override. Skips discovery
            and submits exactly these ids in topo order built from
            their `bd dep --type=blocks` edges. Each id must exist and
            be open; closed ids raise (reopen first).
    """
    logger = get_run_logger()
    _tag_root_run(epic_id, logger, extra_tag=f"epic_id:{epic_id}")

    if discover not in VALID_EPIC_DISCOVER_MODES:
        raise ValueError(
            f"unknown discover mode {discover!r}; "
            f"valid: {list(VALID_EPIC_DISCOVER_MODES)}"
        )

    if child_ids:
        ids = _parse_child_ids(child_ids)
        if not ids:
            raise ValueError("--child-ids was set but parsed to an empty list")
        # Thread rig_path so bd shellouts target the rig's `.beads/`,
        # not the Python (Prefect runner) cwd. See prefect-orchestration-3mw.
        nodes = collect_explicit_children(ids, rig_path=rig_path)
        logger.info(
            f"--child-ids supplied; bypassing discovery, "
            f"dispatching {len(nodes)} explicit node(s): {ids}"
        )
    else:
        nodes = _discover_children(epic_id, discover, rig_path)
        logger.info(
            f"discovered {len(nodes)} node(s) under {epic_id} via mode={discover}"
        )

    if not nodes:
        logger.warning(
            f"no open/in-progress children under {epic_id} "
            f"(discover={discover!r}, child_ids={child_ids!r})"
        )
        return {"status": "empty", "epic_id": epic_id}

    # NOTE (po-formulas-software-dev-1y0): the *-wts* variant accepts
    # `parent_epic_worktree=...` and runs `agent_step` inside the shared
    # epic worktree. The bare `software-dev-full` would silently ignore
    # those kwargs and run in the main rig, sending child commits to
    # `main` instead of `wts-<epic-id>`.
    formula_callable = _resolve_formula("software-dev-full-wts")
    _check_formula_signature("software-dev-full-wts", formula_callable)

    out = _dispatch_nodes(
        nodes=nodes,
        rig=rig,
        rig_path=rig_path,
        formula_callable=formula_callable,
        parent_bead=epic_id,
        iter_caps={
            "iter_cap": iter_cap,
            "plan_iter_cap": plan_iter_cap,
            "verify_iter_cap": verify_iter_cap,
            "ralph_iter_cap": ralph_iter_cap,
        },
        dry_run=dry_run,
        max_issues=max_issues,
        logger=logger,
        extra_formula_kwargs={
            "parent_epic_worktree": parent_epic_worktree,
            "parent_epic_branch": parent_epic_branch,
            "parent_epic_id": parent_epic_id or epic_id,
            "parent_epic_merge_target": merge_target_branch,
        },
    )
    return {"epic_id": epic_id, **out}
