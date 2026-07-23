"""Unit tests for `epic_run` discovery flags (prefect-orchestration-h5s).

Covers the `discover={parent-child,ids,deps,both}` parameter and the
`child_ids="a,b,c"` explicit override. The `@flow` engine is not driven
through Prefect server here — we hit the inner ``epic_run.fn`` callable
directly so each test is a pure-Python invocation.

`_dispatch_nodes` is monkey-patched to capture the nodes that *would*
be submitted; `list_subgraph`, `list_epic_children`, and
`collect_explicit_children` are patched per-test to feed canned
discovery results.

The default mode is ``parent-child`` (po-formulas-software-dev-e9s):
discovery walks ONLY parent-child edges so `blocks` edges can't widen
the set into sibling epics. `blocks` is still used to topo-order within
the discovered set, and `discover=both` opts back into the union.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from po_formulas import epic as epic_mod


_RUN = epic_mod.epic_run.fn  # bypass Prefect engine


class _NullLogger:
    def info(self, *_a: Any, **_k: Any) -> None: ...
    def warning(self, *_a: Any, **_k: Any) -> None: ...
    def error(self, *_a: Any, **_k: Any) -> None: ...


def _stub_logger() -> Any:
    """`get_run_logger()` raises outside a Prefect context — return a no-op."""
    return patch("po_formulas.epic.get_run_logger", return_value=_NullLogger())


def _capture_dispatch() -> tuple[list[dict[str, Any]], Any]:
    """Build a stand-in for `_dispatch_nodes` that records its `nodes` arg."""
    captured: list[dict[str, Any]] = []

    def fake_dispatch(
        *,
        nodes: list[dict[str, Any]],
        rig: str,
        rig_path: str,
        formula_callable: Any,
        parent_bead: str | None,
        iter_caps: dict[str, Any],
        dry_run: bool,
        max_issues: int | None,
        logger: Any,
    ) -> dict[str, Any]:
        captured.extend(nodes)
        return {"submitted": len(nodes), "results": {n["id"]: {} for n in nodes}}

    return captured, fake_dispatch


def _stub_resolve_and_check() -> tuple[Any, Any]:
    """Patches that bypass entry-point lookup + signature checks."""
    return (
        patch("po_formulas.epic._resolve_formula", return_value=lambda **_: None),
        patch("po_formulas.epic._check_formula_signature", return_value=None),
    )


def _stub_tag() -> Any:
    return patch("po_formulas.epic._tag_root_run", return_value=None)


# ─────────────────────── AC 1: dot-suffix back-compat ─────────────────


def test_discover_ids_only_uses_dot_suffix() -> None:
    """`discover="ids"` must call `list_epic_children(mode="ids")` and
    must NOT touch `collect_explicit_children`."""
    captured, fake_dispatch = _capture_dispatch()
    ids_nodes = [
        {"id": "ep.1", "status": "open", "title": "first", "block_deps": []},
        {"id": "ep.2", "status": "open", "title": "second", "block_deps": ["ep.1"]},
    ]
    resolve_p, check_p = _stub_resolve_and_check()
    with (
        _stub_logger(),
        _stub_tag(),
        resolve_p,
        check_p,
        patch("po_formulas.epic._dispatch_nodes", side_effect=fake_dispatch),
        patch(
            "po_formulas.epic.list_epic_children", return_value=ids_nodes
        ) as mock_lec,
        patch(
            "po_formulas.epic.collect_explicit_children",
            side_effect=AssertionError("must not be called"),
        ),
    ):
        out = _RUN(epic_id="ep", rig="r", rig_path="/tmp/r", discover="ids")

    mock_lec.assert_called_once_with("ep", mode="ids", rig_path="/tmp/r")
    assert out["submitted"] == 2
    assert [n["id"] for n in captured] == ["ep.1", "ep.2"]


# ─────────────────────── AC 2: deps walk ──────────────────────────────


def test_discover_deps_uses_subgraph() -> None:
    captured, fake_dispatch = _capture_dispatch()
    deps_nodes = [
        {"id": "child-A", "status": "open", "title": "A", "block_deps": []},
        {"id": "child-B", "status": "open", "title": "B", "block_deps": ["child-A"]},
    ]
    resolve_p, check_p = _stub_resolve_and_check()
    with (
        _stub_logger(),
        _stub_tag(),
        resolve_p,
        check_p,
        patch("po_formulas.epic._dispatch_nodes", side_effect=fake_dispatch),
        patch(
            "po_formulas.epic.list_epic_children", return_value=deps_nodes
        ) as mock_lec,
    ):
        out = _RUN(epic_id="ep", rig="r", rig_path="/tmp/r", discover="deps")

    mock_lec.assert_called_once_with("ep", mode="deps", rig_path="/tmp/r")
    assert out["submitted"] == 2
    assert sorted(n["id"] for n in captured) == ["child-A", "child-B"]


# ─────────────────────── AC 3: --child-ids override ───────────────────


def test_child_ids_skips_discovery() -> None:
    """`child_ids="a,b,c"` must dispatch exactly those, with discovery
    helpers neither called for `list_epic_children` nor errored."""
    captured, fake_dispatch = _capture_dispatch()
    explicit_nodes = [
        {"id": "a", "status": "open", "title": "a", "block_deps": []},
        {"id": "b", "status": "open", "title": "b", "block_deps": ["a"]},
        {"id": "c", "status": "open", "title": "c", "block_deps": ["b"]},
    ]
    resolve_p, check_p = _stub_resolve_and_check()
    with (
        _stub_logger(),
        _stub_tag(),
        resolve_p,
        check_p,
        patch("po_formulas.epic._dispatch_nodes", side_effect=fake_dispatch),
        patch(
            "po_formulas.epic.list_epic_children",
            side_effect=AssertionError("must not be called when child_ids set"),
        ),
        patch(
            "po_formulas.epic.collect_explicit_children", return_value=explicit_nodes
        ) as mock_cec,
    ):
        out = _RUN(epic_id="ep", rig="r", rig_path="/tmp/r", child_ids="a, b ,c")

    mock_cec.assert_called_once_with(["a", "b", "c"], rig_path="/tmp/r")
    assert out["submitted"] == 3
    # Topo via blocks: a → b → c
    submitted_ids = [n["id"] for n in captured]
    assert (
        submitted_ids.index("a") < submitted_ids.index("b") < submitted_ids.index("c")
    )


def test_child_ids_empty_after_parse_raises() -> None:
    """Whitespace-only `child_ids` should raise rather than silently
    fall back to discovery."""
    resolve_p, check_p = _stub_resolve_and_check()
    with (
        _stub_logger(),
        _stub_tag(),
        resolve_p,
        check_p,
        patch(
            "po_formulas.epic._dispatch_nodes",
            side_effect=AssertionError("must not be called"),
        ),
    ):
        with pytest.raises(ValueError, match="empty list"):
            _RUN(epic_id="ep", rig="r", rig_path="/tmp/r", child_ids=" , ,")


# ─────────────────────── AC 4: --discover validation + both ───────────


def test_invalid_discover_raises() -> None:
    resolve_p, check_p = _stub_resolve_and_check()
    with (
        _stub_logger(),
        _stub_tag(),
        resolve_p,
        check_p,
        patch(
            "po_formulas.epic._dispatch_nodes",
            side_effect=AssertionError("must not be called"),
        ),
    ):
        with pytest.raises(ValueError, match="unknown discover mode"):
            _RUN(epic_id="ep", rig="r", rig_path="/tmp/r", discover="bogus")


def test_discover_default_is_parent_child() -> None:
    """Default behaviour (po-formulas-software-dev-e9s): walk parent-child
    edges ONLY via `list_subgraph` — `blocks` must not widen the set, and
    `list_epic_children` must not be touched."""
    captured, fake_dispatch = _capture_dispatch()
    pc_nodes = [
        {"id": "child-A", "status": "open", "title": "A", "block_deps": []},
        {"id": "child-B", "status": "open", "title": "B", "block_deps": ["child-A"]},
    ]
    resolve_p, check_p = _stub_resolve_and_check()
    with (
        _stub_logger(),
        _stub_tag(),
        resolve_p,
        check_p,
        patch("po_formulas.epic._dispatch_nodes", side_effect=fake_dispatch),
        patch("po_formulas.epic.list_subgraph", return_value=pc_nodes) as mock_ls,
        patch(
            "po_formulas.epic.list_epic_children",
            side_effect=AssertionError("must not be called for parent-child mode"),
        ),
    ):
        out = _RUN(epic_id="ep", rig="r", rig_path="/tmp/r")

    # parent-child traversal collects the set; blocks is NOT in `traverse`.
    mock_ls.assert_called_once_with(
        "ep",
        traverse=("parent-child",),
        include_closed=False,
        include_root=False,
        rig_path="/tmp/r",
    )
    assert out["submitted"] == 2
    assert sorted(n["id"] for n in captured) == ["child-A", "child-B"]


def test_discover_explicit_parent_child_matches_default() -> None:
    """Passing `discover="parent-child"` explicitly behaves like the default."""
    _captured, fake_dispatch = _capture_dispatch()
    resolve_p, check_p = _stub_resolve_and_check()
    with (
        _stub_logger(),
        _stub_tag(),
        resolve_p,
        check_p,
        patch("po_formulas.epic._dispatch_nodes", side_effect=fake_dispatch),
        patch(
            "po_formulas.epic.list_subgraph",
            return_value=[
                {"id": "x", "status": "open", "title": "x", "block_deps": []}
            ],
        ) as mock_ls,
        patch(
            "po_formulas.epic.list_epic_children",
            side_effect=AssertionError("must not be called for parent-child mode"),
        ),
    ):
        _RUN(epic_id="ep", rig="r", rig_path="/tmp/r", discover="parent-child")

    mock_ls.assert_called_once_with(
        "ep",
        traverse=("parent-child",),
        include_closed=False,
        include_root=False,
        rig_path="/tmp/r",
    )


def test_discover_both_opts_into_union() -> None:
    """`discover="both"` keeps the blocks-aware union via
    `list_epic_children(..., mode="both")` and does NOT call
    `list_subgraph` directly."""
    _captured, fake_dispatch = _capture_dispatch()
    resolve_p, check_p = _stub_resolve_and_check()
    with (
        _stub_logger(),
        _stub_tag(),
        resolve_p,
        check_p,
        patch("po_formulas.epic._dispatch_nodes", side_effect=fake_dispatch),
        patch(
            "po_formulas.epic.list_epic_children",
            return_value=[
                {"id": "x", "status": "open", "title": "x", "block_deps": []}
            ],
        ) as mock_lec,
        patch(
            "po_formulas.epic.list_subgraph",
            side_effect=AssertionError("must not be called for mode=both"),
        ),
    ):
        _RUN(epic_id="ep", rig="r", rig_path="/tmp/r", discover="both")

    mock_lec.assert_called_once_with("ep", mode="both", rig_path="/tmp/r")


# ─────────────────────── empty-discovery short-circuit ────────────────


def test_empty_discovery_returns_empty_status_without_dispatch() -> None:
    resolve_p, check_p = _stub_resolve_and_check()
    with (
        _stub_logger(),
        _stub_tag(),
        resolve_p,
        check_p,
        patch(
            "po_formulas.epic._dispatch_nodes",
            side_effect=AssertionError("must not be called"),
        ),
        patch("po_formulas.epic.list_epic_children", return_value=[]),
    ):
        out = _RUN(epic_id="ep", rig="r", rig_path="/tmp/r", discover="deps")

    assert out == {"status": "empty", "epic_id": "ep"}
