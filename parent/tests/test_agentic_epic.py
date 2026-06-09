"""Unit tests for `agentic_epic` (plan → create stamped children → fan out).

Two layers:
  * `_parse_plan` — the flow's correctness gate on the planner's plan.json
    (rejects malformed / empty / dup-key / dangling-dep plans; accepts a
    valid one, ordered, deps normalized).
  * the flow happy path — with `agent_step` / `create_child_bead` / `graph_run`
    / claim / close monkeypatched (mirrors test_agentic_flow.py): asserts one
    bead per planned child, each stamped `po.formula=software-dev-agentic`,
    blocks edges wired, then `graph_run` dispatched and the epic closed.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from po_formulas import agentic_epic as ae

_NULL_LOGGER = logging.getLogger("po_formulas.agentic_epic.test")


# ── _parse_plan ─────────────────────────────────────────────────────────────


def _write_plan(tmp_path: Path, obj) -> Path:
    (tmp_path / ae._PLAN_FILE).write_text(json.dumps(obj))
    return tmp_path


def test_parse_plan_valid(tmp_path):
    _write_plan(
        tmp_path,
        {
            "children": [
                {"key": "1", "title": "add model", "description": "d1", "depends_on": []},
                {"key": "2", "title": "wire route", "description": "d2", "depends_on": ["1"]},
            ]
        },
    )
    plan = ae._parse_plan(tmp_path, max_children=12)
    assert [c["key"] for c in plan] == ["1", "2"]
    assert plan[1]["depends_on"] == ["1"]


@pytest.mark.parametrize(
    "obj, match",
    [
        ({"children": []}, "no non-empty 'children'"),
        ({"nope": 1}, "no non-empty 'children'"),
        ({"children": [{"key": "1", "title": "", "description": "d"}]}, "missing a title"),
        (
            {"children": [{"key": "1", "title": "t", "description": "d"}, {"key": "1", "title": "u", "description": "d"}]},
            "missing/duplicate key",
        ),
        (
            {"children": [{"key": "1", "title": "t", "description": "d", "depends_on": ["9"]}]},
            "unknown key",
        ),
    ],
)
def test_parse_plan_rejects(tmp_path, obj, match):
    _write_plan(tmp_path, obj)
    with pytest.raises(ValueError, match=match):
        ae._parse_plan(tmp_path, max_children=12)


def test_parse_plan_missing_file(tmp_path):
    with pytest.raises(ValueError, match="wrote no plan.json"):
        ae._parse_plan(tmp_path, max_children=12)


def test_parse_plan_over_cap(tmp_path):
    _write_plan(
        tmp_path,
        {"children": [{"key": str(i), "title": "t", "description": "d"} for i in range(5)]},
    )
    with pytest.raises(ValueError, match="max_children=3"):
        ae._parse_plan(tmp_path, max_children=3)


# ── flow happy path ─────────────────────────────────────────────────────────


def test_agentic_epic_creates_stamped_children_and_dispatches(tmp_path, monkeypatch):
    epic_id = "rig-epic1"
    rig_path = tmp_path
    run_dir = rig_path / ".planning" / "agentic-epic" / epic_id

    plan = {
        "children": [
            {"key": "1", "title": "add model", "description": "d1", "depends_on": []},
            {"key": "2", "title": "wire route", "description": "d2", "depends_on": ["1"]},
        ]
    }

    # The planner step writes plan.json; the critic step passes.
    def fake_agent_step(*, agent_dir, step, **kwargs):
        if step == "epic-plan":
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / ae._PLAN_FILE).write_text(json.dumps(plan))

        class _R:
            verdict = "pass"
            closed_by = "agent"

        return _R()

    created: list[tuple[str, str]] = []
    stamped: list[tuple[str, str, str]] = []
    deps: list[tuple[str, str]] = []
    dispatched: dict = {}
    closed: list[str] = []

    monkeypatch.setattr(ae, "get_run_logger", lambda: _NULL_LOGGER)
    monkeypatch.setattr(ae, "agent_step", fake_agent_step)
    monkeypatch.setattr(ae, "claim_issue", lambda *a, **k: None)
    monkeypatch.setattr(ae, "_bd_show_description", lambda *a, **k: "build the thing")
    monkeypatch.setattr(
        ae,
        "create_child_bead",
        lambda *, parent_id, child_id, **k: created.append((parent_id, child_id)) or child_id,
    )
    monkeypatch.setattr(ae, "_bd_set_metadata", lambda i, k, v, rp: stamped.append((i, k, v)))
    monkeypatch.setattr(ae, "_bd_dep_add", lambda c, d, rp: deps.append((c, d)))
    monkeypatch.setattr(ae, "graph_run", lambda **k: dispatched.update(k) or {"status": "ok"})
    monkeypatch.setattr(ae, "close_issue", lambda i, **k: closed.append(i))

    result = ae.agentic_epic.fn(
        epic_id=epic_id, rig="rig", rig_path=str(rig_path), iter_cap=2, plan_iter_cap=2
    )

    # One bead per child, parented under the epic.
    assert created == [(epic_id, f"{epic_id}.1"), (epic_id, f"{epic_id}.2")]
    # Each child stamped with the agentic formula.
    assert (f"{epic_id}.1", "po.formula", "software-dev-agentic") in stamped
    assert (f"{epic_id}.2", "po.formula", "software-dev-agentic") in stamped
    # The blocks edge: child .2 depends on .1.
    assert deps == [(f"{epic_id}.2", f"{epic_id}.1")]
    # Fanned out via graph_run, rooted at the epic, with the agentic formula.
    assert dispatched["root_id"] == epic_id
    assert dispatched["formula"] == "software-dev-agentic"
    # Epic closed on a clean fan-out.
    assert closed == [epic_id]
    assert result["status"] == "completed"
    assert result["children"] == [f"{epic_id}.1", f"{epic_id}.2"]


def test_agentic_epic_dry_run_skips_creation(tmp_path, monkeypatch):
    monkeypatch.setattr(ae, "get_run_logger", lambda: _NULL_LOGGER)
    monkeypatch.setattr(ae, "agent_step", lambda **k: type("R", (), {"verdict": "pass", "closed_by": "x"})())
    monkeypatch.setattr(ae, "_bd_show_description", lambda *a, **k: "goal")
    monkeypatch.setattr(
        ae, "create_child_bead", lambda **k: pytest.fail("dry-run must not create beads")
    )
    monkeypatch.setattr(ae, "graph_run", lambda **k: pytest.fail("dry-run must not dispatch"))

    result = ae.agentic_epic.fn(epic_id="e1", rig="r", rig_path=str(tmp_path), dry_run=True)
    assert result["status"] == "dry-run"
