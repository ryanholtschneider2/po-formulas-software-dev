"""Prefect flow: `graph_run`.

Generalization of `epic_run` (prefect-orchestration-uc0). Takes any bead
ID as a root and fans out every reachable descendant — collected via
`bd dep` edges (parent-child / blocks / tracks), no naming convention
required — as a Prefect DAG with `wait_for=` wired from the blocks
sub-graph.

Replaces the dot-suffix convention (`<epic>.1`, `<epic>.2`, …) with
edge-driven traversal so feature beads with sub-tasks, convoys, and
ad-hoc grouping beads can all be batch-dispatched. `epic_run` becomes
a thin wrapper that calls `graph_run` with `traverse=("parent-child",
"blocks")` and falls back to dot-suffix probing for legacy epics whose
`bd dep` edges are not populated.

CLI surface (via core's generic `po run` kwargs passthrough):

    po run graph <root-id> \\
      --rig <name> \\
      --rig-path <path> \\
      [--traverse=parent-child,blocks,tracks]   # default: parent-child,blocks
      [--formula=software-dev-full]              # default: software-dev-full
      [--max-issues=N]
      [--include-closed]
      [--root-as-node]

Formula contract (AC 4): the formula named by `--formula` must accept
`(issue_id, rig, rig_path)` plus optional `parent_bead` / `dry_run`.
A pre-flight `inspect.signature` check rejects mismatches before any
submissions land.
"""

from __future__ import annotations

import inspect
from importlib.metadata import entry_points
from typing import Any, Callable, Iterable

from prefect import flow, get_run_logger, task
from prefect.futures import PrefectFuture
from prefect.runtime import flow_run

from prefect_orchestration.beads_meta import (
    DEFAULT_TRAVERSE,
    list_subgraph,
    topo_sort_blocks,
)


def _live_flow_run_count(issue_id: str) -> int:
    """Count Running Prefect flow runs tagged with issue_id:<id>. Best-effort; returns 0 on error."""
    try:
        import anyio
        from prefect.client.orchestration import get_client
        from prefect_orchestration import status as _status

        async def _count() -> int:
            async with get_client() as client:
                runs = await _status.find_runs_by_issue_id(
                    client, issue_id=issue_id, state="Running", limit=10
                )
                return len(runs)

        return anyio.run(_count)
    except Exception:  # noqa: BLE001
        return 0


def _child_is_in_flight(node: dict[str, Any], logger: Any) -> bool:
    """True if this child bead is in-progress with a live Prefect flow run.

    Two-stage check: first the cheap bead-status probe (no network), then a
    Prefect server query only when the bead is in-progress. If Prefect is
    unreachable the guard degrades silently (returns False so dispatch proceeds).
    """
    if node.get("status") != "in_progress":
        return False
    node_id = node["id"]
    count = _live_flow_run_count(node_id)
    if count > 0:
        logger.warning(
            "graph_run: child %s is in_progress with %d live flow run(s) — skipping re-dispatch",
            node_id,
            count,
        )
        return True
    return False


def _tag_root_run(root_id: str, logger: Any, *, extra_tag: str | None = None) -> None:
    """Stamp `issue_id:<id>` (+ optional extra) tags on the active flow run.

    Best-effort: a client failure should not abort the flow. Mirrors
    `epic.py::_tag_epic_run` but parametrised so the `epic_run` wrapper
    can keep stamping `epic_id:<id>` for backward compat while
    `graph_run` stamps `root_id:<id>`.
    """
    fr_id = flow_run.get_id()
    if not fr_id:
        return
    try:
        from prefect.client.orchestration import get_client

        with get_client(sync_client=True) as c:
            existing = list(flow_run.tags or [])
            want = [f"issue_id:{root_id}"]
            if extra_tag:
                want.append(extra_tag)
            missing = [t for t in want if t not in existing]
            if missing:
                c.update_flow_run(fr_id, tags=[*existing, *missing])
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"flow-run tag failed: {exc}")


def _resolve_formula(name: str) -> Callable[..., Any]:
    """Look up `name` in the `po.formulas` EP group; load + return the callable.

    Raises `ValueError` with a clear message on miss so the flow fails
    before any submissions.
    """
    try:
        eps = entry_points(group="po.formulas")
    except TypeError:  # older importlib.metadata
        eps = entry_points().get("po.formulas", [])  # type: ignore[assignment]
    by_name = {ep.name: ep for ep in eps}
    if name not in by_name:
        known = sorted(by_name)
        raise ValueError(f"unknown formula {name!r}; known: {known}. Run `po list`.")
    return by_name[name].load()


_REQUIRED_FORMULA_PARAMS = ("issue_id", "rig", "rig_path")


def _check_formula_signature(name: str, fn: Callable[..., Any]) -> None:
    """Reject formulas missing the required `(issue_id, rig, rig_path)` params.

    Looks at the underlying function (Prefect `@flow` wraps in a `Flow`
    object whose `.fn` is the original callable).
    """
    inner = getattr(fn, "fn", fn)
    try:
        sig = inspect.signature(inner)
    except (TypeError, ValueError):
        return  # builtins / C funcs — caller takes their chances
    params = sig.parameters
    missing = [p for p in _REQUIRED_FORMULA_PARAMS if p not in params]
    if missing:
        raise ValueError(
            f"formula {name!r} missing required parameter(s) {missing}; "
            "graph_run formulas must accept (issue_id, rig, rig_path)."
        )


@task(name="run_node")
def _run_node_task(
    formula_callable: Callable[..., Any], **kwargs: Any
) -> dict[str, Any]:
    """Task shim: invoke the resolved formula with the given kwargs.

    Prefect 3 doesn't allow `flow.submit()`; a task that calls the flow
    gives us per-node `wait_for=` ordering plus a child-run row in the
    UI. Mirrors `epic.py::_run_issue_task` but parametrised on the
    formula callable.
    """
    return formula_callable(**kwargs)


def _dispatch_nodes(
    *,
    nodes: list[dict[str, Any]],
    rig: str,
    rig_path: str,
    formula_callable: Callable[..., Any],
    parent_bead: str | None,
    iter_caps: dict[str, Any],
    dry_run: bool,
    max_issues: int | None,
    logger: Any,
    extra_formula_kwargs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Topo-sort `nodes` by their blocks-subgraph and submit one task per node.

    `iter_caps` is the bag of optional knobs (`iter_cap`, `plan_iter_cap`,
    …) the formula understands. We pass them through as kwargs only if
    the formula's signature accepts them, so a formula that doesn't care
    about iter caps doesn't get mystery kwargs.

    `extra_formula_kwargs` is the same idea for non-iter knobs a specific
    dispatcher wants threaded to every node (e.g. shared-branch mode passes
    `epic_branch` / `parent_epic_id`). Like `iter_caps`, each key is forwarded
    only to formulas whose signature accepts it.
    """
    ordered = topo_sort_blocks(nodes)
    if max_issues:
        ordered = ordered[:max_issues]
    if not ordered:
        return {"status": "empty", "submitted": 0, "results": {}}

    logger.info(f"submitting {len(ordered)} node(s): {[c['id'] for c in ordered]}")

    # Resolve the per-node formula. Default = caller's `formula_callable`
    # (already resolved); override = `po.formula` metadata on the bead.
    # `po.formula=none` (or empty string) means "human/sync-point bead" —
    # skip dispatch, just poll for closure (the human / external process
    # closes the bead).
    futures: dict[str, PrefectFuture] = {}
    skipped: dict[str, str] = {}  # node_id → reason (for diagnostics)

    # Two-pass: first resolve which nodes would dispatch (have a
    # formula, not human-sync), THEN skip nodes whose parent-child
    # ancestor in the dispatch set is itself dispatching. This is
    # implicit-formula-ownership: when a parent runs as a formula,
    # the formula owns its descendant subtree, so graph_run shouldn't
    # also dispatch the descendants as standalone runs (which would
    # cause double-execution + recursive blow-up across re-runs).
    # See prefect-orchestration-zlf.
    pc_ownership: dict[str, str | None] = {}  # node_id → owning ancestor or None
    per_node_callable: dict[str, Callable[..., Any] | None] = {}
    for node in ordered:
        per_node_callable[node["id"]] = _resolve_per_bead_formula(
            node,
            default_callable=formula_callable,
            rig_path=rig_path,
            logger=logger,
        )
    dispatchable_ids = {nid for nid, c in per_node_callable.items() if c is not None}
    if len(dispatchable_ids) > 1:
        ancestor_index = _build_pc_ancestor_index(
            list(dispatchable_ids), rig_path=rig_path
        )
        for nid in dispatchable_ids:
            owning = next(
                (a for a in ancestor_index.get(nid, set()) if a in dispatchable_ids),
                None,
            )
            pc_ownership[nid] = owning
    else:
        pc_ownership = dict.fromkeys(dispatchable_ids, None)

    for node in ordered:
        deps = [futures[d] for d in node.get("block_deps", []) if d in futures]
        node_callable = per_node_callable[node["id"]]
        if node_callable is None:
            skipped[node["id"]] = "no formula (human/sync-point bead)"
            logger.info(
                "skip dispatch %s: po.formula=none — leaving open for human/external close",
                node["id"],
            )
            continue
        owning_ancestor = pc_ownership.get(node["id"])
        if owning_ancestor is not None:
            skipped[node["id"]] = f"internal to {owning_ancestor}'s formula run"
            logger.info(
                "skip dispatch %s: parent-child descendant of dispatched %s",
                node["id"],
                owning_ancestor,
            )
            continue
        if _child_is_in_flight(node, logger):
            skipped[node["id"]] = (
                "already in-flight (in_progress + live Prefect flow run)"
            )
            continue
        inner = getattr(node_callable, "fn", node_callable)
        try:
            sig = inspect.signature(inner)
            accepted = set(sig.parameters)
        except (TypeError, ValueError):
            accepted = set(_REQUIRED_FORMULA_PARAMS) | {"parent_bead", "dry_run"}
        base_caps = {k: v for k, v in iter_caps.items() if k in accepted}
        extra_caps = {
            k: v for k, v in (extra_formula_kwargs or {}).items() if k in accepted
        }
        kwargs: dict[str, Any] = {
            "issue_id": node["id"],
            "rig": rig,
            "rig_path": rig_path,
            **base_caps,
            **extra_caps,
        }
        if "parent_bead" in accepted and parent_bead is not None:
            kwargs["parent_bead"] = parent_bead
        if "dry_run" in accepted:
            kwargs["dry_run"] = dry_run
        futures[node["id"]] = _run_node_task.submit(
            node_callable, **kwargs, wait_for=deps
        )

    results: dict[str, Any] = {}
    for cid, fut in futures.items():
        try:
            results[cid] = fut.result(raise_on_failure=False)
        except Exception as exc:  # noqa: BLE001
            results[cid] = {"status": "failed", "error": str(exc)}
    for cid, reason in skipped.items():
        results[cid] = {"status": "skipped", "reason": reason}

    return {
        "submitted": len(futures),
        "skipped": len(skipped),
        "results": results,
    }


def _build_pc_ancestor_index(
    node_ids: list[str],
    *,
    rig_path: str,
) -> dict[str, set[str]]:
    """For each node, return the set of parent-child ancestors that are
    ALSO in `node_ids`.

    Walks `bd dep list <id> --direction=down --type=parent-child` (the
    direction returns *parents*, per `beads_meta.resolve_seed_bead`'s
    rig-verified convention). Bounded depth (20 hops) to guard against
    pathological loops; cycles short-circuit.

    Used by `_dispatch_nodes` to enforce implicit-formula-ownership:
    a node whose parent-child ancestor in the dispatch set is itself
    being dispatched as a formula run is "internal" to that run, and
    graph_run should NOT also dispatch it as a standalone subflow.
    See prefect-orchestration-zlf.
    """
    from prefect_orchestration.beads_meta import _bd_dep_list

    in_set = set(node_ids)
    result: dict[str, set[str]] = {nid: set() for nid in node_ids}
    for nid in node_ids:
        cur = nid
        seen: set[str] = {nid}
        for _ in range(20):  # safety bound
            try:
                parents = _bd_dep_list(
                    cur,
                    direction="down",
                    edge_type="parent-child",
                    rig_path=rig_path,
                )
            except Exception:  # noqa: BLE001
                break
            parent_ids = [
                p.get("id")
                for p in (parents or [])
                if isinstance(p, dict) and p.get("id")
            ]
            if not parent_ids:
                break
            cur = sorted(parent_ids)[0]
            if cur in seen:
                break
            seen.add(cur)
            if cur in in_set:
                result[nid].add(cur)
    return result


def _formula_name_from_labels(labels: Any) -> str | None:
    """A bead can carry its formula as a LABEL, e.g. ``formula:software-dev-agentic``
    or ``po.formula=software-dev-agentic``. This is the only per-bead stamp that
    works on beads-rust (which has no arbitrary metadata, only labels). Returns
    the formula name, or None if no formula label is present.
    """
    if not isinstance(labels, (list, tuple)):
        return None
    for raw in labels:
        s = str(raw).strip()
        low = s.lower()
        if low.startswith("formula:"):
            return s.split(":", 1)[1].strip() or None
        if low.startswith("po.formula="):
            return s.split("=", 1)[1].strip() or None
    return None


def _resolve_per_bead_formula(
    node: dict[str, Any],
    *,
    default_callable: Callable[..., Any],
    rig_path: str,
    logger: Any,
) -> Callable[..., Any] | None:
    """Pick the per-bead formula callable from a `po.formula` metadata key OR a
    `formula:<name>` label, falling back to the default.

    Resolution order: ``po.formula`` metadata (dolt-bd) > a ``formula:<name>`` /
    ``po.formula=<name>`` label (beads-rust, which has no metadata) > the default
    callable. Returns:
      - the **default callable** when the bead names no formula (common case).
      - a freshly-EP-resolved callable when it names a different one.
      - ``None`` when the bead is a human/sync-point (formula in
        {none, no, human, skip} or unresolvable) — caller skips dispatch.

    ``list_subgraph`` returns lean rows, so we fetch the bead's metadata+labels
    lazily via ``_bd_show`` (one shell per node, only when the dispatcher cares).
    """
    from prefect_orchestration.beads_meta import _bd_show as _shell_bd_show

    meta = node.get("metadata") or {}
    if not isinstance(meta, dict):
        meta = {}
    labels = node.get("labels")
    # Lazily fetch the full row if the lean node lacks metadata or labels.
    if not meta or labels is None:
        try:
            row = _shell_bd_show(node["id"], rig_path=rig_path) or {}
        except Exception:  # noqa: BLE001
            row = {}
        if isinstance(row, dict):
            if not meta:
                meta = row.get("metadata") or {}
                if not isinstance(meta, dict):
                    meta = {}
            if labels is None:
                labels = row.get("labels")

    requested = meta.get("po.formula")
    if requested is None or requested == "":
        # beads-rust path: read the formula off a label instead of metadata.
        requested = _formula_name_from_labels(labels)
    if requested is None or requested == "":
        return default_callable
    if str(requested).lower() in {"none", "no", "human", "skip"}:
        return None
    try:
        return _resolve_formula(str(requested))
    except ValueError as exc:
        logger.warning(
            "skip dispatch %s: formula=%r unresolvable (%s)",
            node["id"],
            requested,
            exc,
        )
        return None


@flow(name="graph_run", flow_run_name="{root_id}", log_prints=True)
def graph_run(
    root_id: str,
    rig: str,
    rig_path: str,
    traverse: str | Iterable[str] = ",".join(DEFAULT_TRAVERSE),
    formula: str = "software-dev-full",
    max_issues: int | None = None,
    include_closed: bool = False,
    root_as_node: bool = False,
    iter_cap: int = 3,
    plan_iter_cap: int = 2,
    verify_iter_cap: int = 3,
    ralph_iter_cap: int = 3,
    dry_run: bool = False,
    extra_formula_kwargs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fan out a bd sub-graph rooted at `root_id` as a Prefect DAG.

    Args:
        root_id: bd issue id used as the BFS starting point.
        rig: rig name (passed through to each per-node formula).
        rig_path: absolute path to the rig (passed through to each
            per-node formula).
        traverse: comma-separated edge types to follow during BFS
            (`parent-child`, `blocks`, `tracks`). Default:
            `parent-child,blocks`.
        formula: `po.formulas` entry-point name to invoke per node.
            Default: `software-dev-full`. The formula must declare
            `issue_id`, `rig`, `rig_path` as **named parameters**
            (positional-or-keyword); `**kwargs`-only signatures fail
            the pre-flight check even though they could in principle
            accept the args, so the contract violation we usually
            care about (a formula that genuinely doesn't take those)
            still surfaces clearly. `parent_bead` and `dry_run` are
            passed through if the formula declares them.
        max_issues: if set, submit only the first N topo-prefix nodes.
        include_closed: include closed beads in the collected set
            (for re-running / verification).
        root_as_node: include the root bead itself in the submitted set.
            Default: false (root is treated as a container).

    Returns a dict::

        {"root_id": str, "submitted": int, "results": {id: result, ...}}
    """
    logger = get_run_logger()
    _tag_root_run(root_id, logger, extra_tag=f"root_id:{root_id}")

    formula_callable = _resolve_formula(formula)
    _check_formula_signature(formula, formula_callable)

    # Thread rig_path so bd shellouts target the rig's `.beads/`, not
    # the Python (Prefect runner) cwd. See prefect-orchestration-3mw.
    nodes = list_subgraph(
        root_id,
        traverse=traverse,
        include_closed=include_closed,
        include_root=root_as_node,
        rig_path=rig_path,
    )
    if not nodes:
        logger.warning(
            f"no reachable nodes under {root_id} (traverse={traverse!r}, "
            f"include_closed={include_closed}, root_as_node={root_as_node})"
        )
        return {"status": "empty", "root_id": root_id, "submitted": 0, "results": {}}

    out = _dispatch_nodes(
        nodes=nodes,
        rig=rig,
        rig_path=rig_path,
        formula_callable=formula_callable,
        parent_bead=root_id,
        iter_caps={
            "iter_cap": iter_cap,
            "plan_iter_cap": plan_iter_cap,
            "verify_iter_cap": verify_iter_cap,
            "ralph_iter_cap": ralph_iter_cap,
        },
        dry_run=dry_run,
        max_issues=max_issues,
        logger=logger,
        extra_formula_kwargs=extra_formula_kwargs,
    )
    return {"root_id": root_id, **out}
