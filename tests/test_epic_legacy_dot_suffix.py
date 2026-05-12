"""Regression test for the `epic_run` dot-suffix fallback (AC 8 of
prefect-orchestration-uc0; updated by prefect-orchestration-h5s).

Real legacy data in this rig (e.g. `prefect-orchestration-3cu`) has zero
`bd dep` edges to its `<epic>.N` children — switching `epic_run` to pure
graph traversal would silently drop them. The fallback path in
`epic._legacy_dot_suffix_children` is what keeps them working.

After h5s the shape adaptation moved into
`prefect_orchestration.beads_meta.list_epic_children(mode="ids")`, so
`_legacy_dot_suffix_children` is now a one-line shim that delegates
there. The contract this test pins down: when the dot-suffix probe
returns nodes, the helper passes them through unchanged in
`{id, status, title, block_deps}` shape.
"""

from __future__ import annotations

from unittest.mock import patch

from po_formulas.epic import _legacy_dot_suffix_children


def test_legacy_helper_returns_dot_suffix_nodes() -> None:
    fake_children = [
        {"id": "ep.1", "status": "open", "title": "first", "block_deps": []},
        {"id": "ep.2", "status": "open", "title": "second", "block_deps": ["ep.1"]},
        {
            "id": "ep.3",
            "status": "open",
            "title": "third",
            "block_deps": ["ep.2"],
        },
    ]
    with patch(
        "po_formulas.epic.list_epic_children", return_value=fake_children
    ) as mock_lec:
        nodes = _legacy_dot_suffix_children("ep")

    mock_lec.assert_called_once_with("ep", mode="ids")
    assert [n["id"] for n in nodes] == ["ep.1", "ep.2", "ep.3"]
    by_id = {n["id"]: n for n in nodes}
    assert by_id["ep.1"]["block_deps"] == []
    assert by_id["ep.2"]["block_deps"] == ["ep.1"]
    assert by_id["ep.3"]["block_deps"] == ["ep.2"]


def test_legacy_helper_returns_empty_when_no_children() -> None:
    with patch("po_formulas.epic.list_epic_children", return_value=[]):
        assert _legacy_dot_suffix_children("ep") == []
