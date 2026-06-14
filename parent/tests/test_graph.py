"""Unit tests for `graph_run` (prefect-orchestration-uc0).

These tests exercise the dispatch helpers (`_resolve_formula`,
`_check_formula_signature`, `_dispatch_nodes`) directly. The Prefect
`@flow`/`@task` decorators are not driven through the live engine here
because that would require a Prefect server — instead we hit the inner
`.fn` callables and patch the task's `.submit` to capture invocations.

The end-to-end shape of `graph_run` itself (BFS → topo → submit) is
covered by the core `test_beads_graph.py` (BFS + topo) plus these
dispatch tests; the e2e CLI test is the integration check that ties
them together.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from po_formulas import graph as graph_mod


# ─────────────────────── _resolve_formula ────────────────────────────


def test_resolve_formula_known_returns_callable() -> None:
    fn = graph_mod._resolve_formula("software-dev-full")
    inner = getattr(fn, "fn", fn)
    assert callable(inner)
    # Sanity: the resolved formula has the expected name.
    assert inner.__name__ == "software_dev_full"


def test_resolve_formula_unknown_raises_with_known_list() -> None:
    with pytest.raises(ValueError) as exc_info:
        graph_mod._resolve_formula("nonsense")
    msg = str(exc_info.value)
    assert "unknown formula" in msg
    assert "nonsense" in msg
    # Includes the list of known names — at least `software-dev-full`
    # should be there for any sane install.
    assert "software-dev-full" in msg


# ─────────────────────── _check_formula_signature ────────────────────


def test_check_formula_signature_accepts_compliant_callable() -> None:
    def good(issue_id: str, rig: str, rig_path: str, **_: Any) -> None: ...
    graph_mod._check_formula_signature("good", good)  # no raise


def test_check_formula_signature_rejects_missing_params() -> None:
    def bad(some_other: str) -> None: ...
    with pytest.raises(ValueError) as exc_info:
        graph_mod._check_formula_signature("bad", bad)
    msg = str(exc_info.value)
    assert "issue_id" in msg
    assert "rig" in msg


# ─────────────────────── _dispatch_nodes ─────────────────────────────


class _StubFuture:
    def __init__(self, value: Any) -> None:
        self._value = value

    def result(self, raise_on_failure: bool = True) -> Any:
        return self._value


def _make_capturing_submit(captured: list[dict[str, Any]]):
    def _submit(formula_callable: Any, **kwargs: Any) -> _StubFuture:
        captured.append(
            {
                "formula": formula_callable,
                "wait_for": kwargs.pop("wait_for", []),
                "kwargs": kwargs,
            }
        )
        return _StubFuture({"id": kwargs.get("issue_id"), "ok": True})

    return _submit


def test_dispatch_nodes_passes_block_deps_as_wait_for() -> None:
    """AC 1 + AC 2 (downstream): nodes run in topo order and `wait_for=`
    contains only futures of in-set blockers."""
    nodes = [
        {"id": "C", "status": "open", "block_deps": ["A", "B"]},
        {"id": "B", "status": "open", "block_deps": ["A"]},
        {"id": "A", "status": "open", "block_deps": []},
    ]
    captured: list[dict[str, Any]] = []

    def fake_formula(*, issue_id: str, rig: str, rig_path: str, **_: Any) -> dict:
        return {"issue_id": issue_id}

    with patch.object(graph_mod._run_node_task, "submit",
                      side_effect=_make_capturing_submit(captured)):
        out = graph_mod._dispatch_nodes(
            nodes=nodes,
            rig="r",
            rig_path="/tmp/rig",
            formula_callable=fake_formula,
            parent_bead="root",
            iter_caps={},
            dry_run=False,
            max_issues=None,
            logger=_NullLogger(),
        )

    assert out["submitted"] == 3
    submitted_ids = [c["kwargs"]["issue_id"] for c in captured]
    # A first (no deps), then B and C in topo order after A.
    assert submitted_ids[0] == "A"
    assert submitted_ids.index("B") < submitted_ids.index("C")
    # A had no waits; B waits on A; C waits on A and B.
    a_idx = submitted_ids.index("A")
    b_idx = submitted_ids.index("B")
    c_idx = submitted_ids.index("C")
    assert captured[a_idx]["wait_for"] == []
    assert len(captured[b_idx]["wait_for"]) == 1
    assert len(captured[c_idx]["wait_for"]) == 2


def test_dispatch_nodes_max_issues_caps_after_topo() -> None:
    """AC 5: `max_issues` slices the topo-prefix."""
    nodes = [
        {"id": str(i), "status": "open", "block_deps": []}
        for i in range(5)
    ]
    captured: list[dict[str, Any]] = []

    def fake(*, issue_id: str, **_: Any) -> dict:
        return {"id": issue_id}

    with patch.object(graph_mod._run_node_task, "submit",
                      side_effect=_make_capturing_submit(captured)):
        out = graph_mod._dispatch_nodes(
            nodes=nodes,
            rig="r",
            rig_path="/tmp/rig",
            formula_callable=fake,
            parent_bead=None,
            iter_caps={},
            dry_run=False,
            max_issues=2,
            logger=_NullLogger(),
        )

    assert out["submitted"] == 2
    assert len(captured) == 2


def test_dispatch_nodes_uses_resolved_formula_callable() -> None:
    """AC 4: `--formula` dispatch — the configured callable, not a default."""
    sentinel = object()

    def fake(*, issue_id: str, rig: str, rig_path: str, **_: Any) -> Any:
        return sentinel

    captured: list[dict[str, Any]] = []
    nodes = [{"id": "A", "status": "open", "block_deps": []}]
    with patch.object(graph_mod._run_node_task, "submit",
                      side_effect=_make_capturing_submit(captured)):
        graph_mod._dispatch_nodes(
            nodes=nodes,
            rig="r",
            rig_path="/tmp/rig",
            formula_callable=fake,
            parent_bead=None,
            iter_caps={},
            dry_run=False,
            max_issues=None,
            logger=_NullLogger(),
        )
    assert captured[0]["formula"] is fake


def test_dispatch_nodes_skips_human_sync_point_beads() -> None:
    """Bead with `po.formula=none` skips dispatch + records as 'skipped'."""
    captured: list[dict[str, Any]] = []
    nodes = [
        {"id": "agent-bead", "status": "open", "block_deps": []},
        {"id": "human-bead", "status": "open", "block_deps": []},
    ]

    def fake_show(bid: str, rig_path: Any = None) -> dict | None:
        # human-bead has po.formula=none; agent-bead has no metadata.
        if bid == "human-bead":
            return {"id": bid, "metadata": {"po.formula": "none"}}
        return {"id": bid, "metadata": {}}

    def fake(*, issue_id: str, rig: str, rig_path: str, **_: Any) -> Any:
        return "ran"

    with (
        patch("prefect_orchestration.beads_meta._bd_show", fake_show),
        patch.object(graph_mod._run_node_task, "submit",
                     side_effect=_make_capturing_submit(captured)),
    ):
        out = graph_mod._dispatch_nodes(
            nodes=nodes, rig="r", rig_path="/tmp/rig",
            formula_callable=fake, parent_bead=None,
            iter_caps={}, dry_run=False, max_issues=None,
            logger=_NullLogger(),
        )

    assert out["submitted"] == 1
    assert out["skipped"] == 1
    # Only the agent bead got dispatched
    submitted_ids = [c["kwargs"]["issue_id"] for c in captured]
    assert submitted_ids == ["agent-bead"]
    # Skipped bead is recorded in results with reason
    assert out["results"]["human-bead"]["status"] == "skipped"


def test_dispatch_nodes_skips_descendants_of_dispatched_ancestor() -> None:
    """Implicit formula ownership: when a parent-child ancestor in the
    dispatch set has a formula, its descendants are skipped (the parent's
    formula run owns them). prefect-orchestration-zlf.
    """
    nodes = [
        {"id": "child1", "status": "open", "block_deps": []},
        {"id": "child1.iter1", "status": "open", "block_deps": []},
        {"id": "child2", "status": "open", "block_deps": []},
    ]
    captured: list[dict[str, Any]] = []

    def fake_show(bid: str, rig_path: Any = None) -> dict | None:
        return {"id": bid, "metadata": {}}

    def fake_dep_list(
        issue_id: str, direction: str, edge_type: str | None = None,
        rig_path: Any = None,
    ) -> list[dict[str, Any]]:
        # Parent-child ancestry: child1.iter1's parent is child1.
        if issue_id == "child1.iter1" and edge_type == "parent-child":
            return [{"id": "child1"}]
        return []

    def fake(*, issue_id: str, rig: str, rig_path: str, **_: Any) -> Any:
        return "ran"

    with (
        patch("prefect_orchestration.beads_meta._bd_show", fake_show),
        patch("prefect_orchestration.beads_meta._bd_dep_list", fake_dep_list),
        patch.object(graph_mod._run_node_task, "submit",
                     side_effect=_make_capturing_submit(captured)),
    ):
        out = graph_mod._dispatch_nodes(
            nodes=nodes, rig="r", rig_path="/tmp/rig",
            formula_callable=fake, parent_bead=None,
            iter_caps={}, dry_run=False, max_issues=None,
            logger=_NullLogger(),
        )

    # child1 + child2 dispatch; child1.iter1 is owned by child1's run
    assert out["submitted"] == 2
    assert out["skipped"] == 1
    submitted_ids = sorted(c["kwargs"]["issue_id"] for c in captured)
    assert submitted_ids == ["child1", "child2"]
    assert out["results"]["child1.iter1"]["status"] == "skipped"
    assert "internal to child1" in out["results"]["child1.iter1"]["reason"]


def test_dispatch_nodes_per_bead_formula_override() -> None:
    """Bead with `po.formula=other-formula` re-resolves the callable per node."""
    other_callable = object()  # sentinel
    nodes = [
        {"id": "default-bead", "status": "open", "block_deps": []},
        {"id": "override-bead", "status": "open", "block_deps": []},
    ]
    captured: list[dict[str, Any]] = []

    def fake_show(bid: str, rig_path: Any = None) -> dict | None:
        if bid == "override-bead":
            return {"id": bid, "metadata": {"po.formula": "other-formula"}}
        return {"id": bid, "metadata": {}}

    def fake_resolve(name: str) -> Any:
        if name == "other-formula":
            return other_callable
        raise ValueError(f"unknown formula {name!r}")

    def default_fn(*, issue_id: str, rig: str, rig_path: str, **_: Any) -> Any:
        return "default"

    with (
        patch("prefect_orchestration.beads_meta._bd_show", fake_show),
        patch.object(graph_mod, "_resolve_formula", fake_resolve),
        patch.object(graph_mod._run_node_task, "submit",
                     side_effect=_make_capturing_submit(captured)),
    ):
        graph_mod._dispatch_nodes(
            nodes=nodes, rig="r", rig_path="/tmp/rig",
            formula_callable=default_fn, parent_bead=None,
            iter_caps={}, dry_run=False, max_issues=None,
            logger=_NullLogger(),
        )

    # Two dispatches; first uses default callable, second uses override.
    by_bead = {c["kwargs"]["issue_id"]: c for c in captured}
    assert by_bead["default-bead"]["formula"] is default_fn
    assert by_bead["override-bead"]["formula"] is other_callable


def test_formula_name_from_labels() -> None:
    assert (
        graph_mod._formula_name_from_labels(["feature", "formula:software-dev-agentic"])
        == "software-dev-agentic"
    )
    assert graph_mod._formula_name_from_labels(["po.formula=agent-step", "x"]) == "agent-step"
    assert graph_mod._formula_name_from_labels(["feature", "bug"]) is None
    assert graph_mod._formula_name_from_labels(None) is None
    assert graph_mod._formula_name_from_labels(["formula:"]) is None  # empty -> None


def test_resolve_per_bead_formula_from_label() -> None:
    """beads-rust path: no metadata, formula carried on a `formula:<name>` label."""
    sentinel = object()

    def fake_show(bid: str, rig_path: Any = None) -> dict | None:
        return {"id": bid, "metadata": {}, "labels": ["feature", "formula:agent-step"]}

    def fake_resolve(name: str) -> Any:
        if name == "agent-step":
            return sentinel
        raise ValueError(name)

    def default_fn(**_: Any) -> Any:
        return "default"

    with (
        patch("prefect_orchestration.beads_meta._bd_show", fake_show),
        patch.object(graph_mod, "_resolve_formula", fake_resolve),
    ):
        got = graph_mod._resolve_per_bead_formula(
            {"id": "b1", "status": "open", "block_deps": []},
            default_callable=default_fn,
            rig_path="/tmp/rig",
            logger=_NullLogger(),
        )
    assert got is sentinel


def test_resolve_per_bead_formula_metadata_beats_label() -> None:
    """`po.formula` metadata takes precedence over a `formula:` label."""
    meta_cb, label_cb = object(), object()

    def fake_show(bid: str, rig_path: Any = None) -> dict | None:
        return {
            "id": bid,
            "metadata": {"po.formula": "meta-formula"},
            "labels": ["formula:label-formula"],
        }

    def fake_resolve(name: str) -> Any:
        return {"meta-formula": meta_cb, "label-formula": label_cb}[name]

    with (
        patch("prefect_orchestration.beads_meta._bd_show", fake_show),
        patch.object(graph_mod, "_resolve_formula", fake_resolve),
    ):
        got = graph_mod._resolve_per_bead_formula(
            {"id": "b1"},
            default_callable=object(),
            rig_path="/tmp/rig",
            logger=_NullLogger(),
        )
    assert got is meta_cb


def test_dispatch_nodes_filters_iter_caps_by_signature() -> None:
    """`iter_cap`/etc. only flow into formulas that declare them — a minimal
    formula isn't surprised by mystery kwargs."""

    def minimal(*, issue_id: str, rig: str, rig_path: str) -> None: ...

    captured: list[dict[str, Any]] = []
    nodes = [{"id": "A", "status": "open", "block_deps": []}]
    with patch.object(graph_mod._run_node_task, "submit",
                      side_effect=_make_capturing_submit(captured)):
        graph_mod._dispatch_nodes(
            nodes=nodes,
            rig="r",
            rig_path="/tmp/rig",
            formula_callable=minimal,
            parent_bead="root",
            iter_caps={"iter_cap": 9, "plan_iter_cap": 9},
            dry_run=True,
            max_issues=None,
            logger=_NullLogger(),
        )
    kwargs = captured[0]["kwargs"]
    assert "iter_cap" not in kwargs
    assert "plan_iter_cap" not in kwargs
    assert "parent_bead" not in kwargs
    assert "dry_run" not in kwargs


def test_dispatch_nodes_cycle_in_blocks_raises_dependency_cycle() -> None:
    """AC 2: cycle in blocks-subgraph → `ValueError` with `dependency cycle`."""
    nodes = [
        {"id": "X", "status": "open", "block_deps": ["Y"]},
        {"id": "Y", "status": "open", "block_deps": ["X"]},
    ]

    def fake(**_: Any) -> None: ...

    with patch.object(graph_mod._run_node_task, "submit", side_effect=AssertionError):
        with pytest.raises(ValueError) as exc_info:
            graph_mod._dispatch_nodes(
                nodes=nodes,
                rig="r",
                rig_path="/tmp/rig",
                formula_callable=fake,
                parent_bead=None,
                iter_caps={},
                dry_run=False,
                max_issues=None,
                logger=_NullLogger(),
            )
    msg = str(exc_info.value)
    assert msg.startswith("dependency cycle: [")
    assert "X" in msg and "Y" in msg


# ─────────────────────── helpers ─────────────────────────────────────


class _NullLogger:
    def info(self, *_a: Any, **_k: Any) -> None: ...
    def warning(self, *_a: Any, **_k: Any) -> None: ...
    def error(self, *_a: Any, **_k: Any) -> None: ...
