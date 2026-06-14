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


# ── _plan_lanes (parallel-where-independent / serial-where-coupled) ───────────


def test_plan_lanes_groups_parallel_then_serial():
    plan = [
        {"key": "1", "depends_on": []},
        {"key": "2", "depends_on": []},
        {"key": "3", "depends_on": ["1", "2"]},
        {"key": "4", "depends_on": ["3"]},
    ]
    # 1 & 2 are independent → one parallel lane; 3 stacks on both; 4 stacks on 3.
    assert ae._plan_lanes(plan) == [["1", "2"], ["3"], ["4"]]


# ── shared-branch mode ───────────────────────────────────────────────────────


def _patch_common(monkeypatch, run_dir, plan):
    """Shared monkeypatching for the shared-branch flow tests."""

    def fake_agent_step(*, agent_dir, step, **kwargs):
        if step == "epic-plan":
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / ae._PLAN_FILE).write_text(json.dumps(plan))

        class _R:
            verdict = "pass"
            closed_by = "agent"

        return _R()

    monkeypatch.setattr(ae, "get_run_logger", lambda: _NULL_LOGGER)
    monkeypatch.setattr(ae, "agent_step", fake_agent_step)
    monkeypatch.setattr(ae, "claim_issue", lambda *a, **k: None)
    monkeypatch.setattr(ae, "_bd_show_description", lambda *a, **k: "goal text")
    monkeypatch.setattr(
        ae, "create_child_bead", lambda *, parent_id, child_id, **k: child_id
    )
    monkeypatch.setattr(ae, "_bd_set_metadata", lambda *a, **k: None)
    monkeypatch.setattr(ae, "_bd_dep_add", lambda *a, **k: None)
    monkeypatch.setattr(ae, "close_issue", lambda *a, **k: None)


def test_agentic_epic_shared_branch_creates_one_branch_and_pr(tmp_path, monkeypatch):
    epic_id = "rig-epic1"
    run_dir = tmp_path / ".planning" / "agentic-epic" / epic_id
    plan = {
        "children": [
            {"key": "1", "title": "a", "description": "d1", "depends_on": []},
            {"key": "2", "title": "b", "description": "d2", "depends_on": ["1"]},
        ]
    }
    _patch_common(monkeypatch, run_dir, plan)

    calls: dict = {}

    def fake_create(rp, eid, **k):
        calls["create"] = (str(eid), k)
        return {"branch": f"epic/{eid}", "created": True, "pushed": True, "remote": True}

    def fake_pr(rp, **k):
        calls["pr"] = k
        return {"opened": True, "url": "https://x/pull/9", "reason": ""}

    def fake_ready(rp, **k):
        calls["ready"] = k
        return {"ready": True, "reason": ""}

    monkeypatch.setattr(ae.sb, "create_integration_branch", fake_create)
    monkeypatch.setattr(ae.sb, "open_draft_pr", fake_pr)
    monkeypatch.setattr(ae.sb, "mark_pr_ready", fake_ready)
    monkeypatch.setattr(
        ae.sb, "cleanup_integration_worktree",
        lambda rp, eid: calls.__setitem__("cleanup", str(eid)),
    )
    dispatched: dict = {}
    monkeypatch.setattr(ae, "graph_run", lambda **k: dispatched.update(k) or {"status": "ok"})

    result = ae.agentic_epic.fn(
        epic_id=epic_id, rig="rig", rig_path=str(tmp_path), shared_branch=True
    )

    # One integration branch off main + one draft PR.
    assert calls["create"][0] == epic_id
    assert calls["create"][1]["base_branch"] == "main"
    assert calls["pr"]["branch"] == f"epic/{epic_id}"
    assert calls["pr"]["base_branch"] == "main"
    # Children fanned out with the epic_branch / parent_epic_id threaded through.
    assert dispatched["extra_formula_kwargs"] == {
        "epic_branch": f"epic/{epic_id}",
        "parent_epic_id": epic_id,
    }
    assert dispatched["formula"] == "software-dev-agentic"
    # Finalize flips the single PR to ready + cleans the integration worktree.
    assert calls["ready"]["branch"] == f"epic/{epic_id}"
    assert calls["cleanup"] == epic_id
    assert result["shared_branch"] is True
    assert result["epic_branch"] == f"epic/{epic_id}"
    assert result["pr"]["url"] == "https://x/pull/9"


def test_agentic_epic_default_off_does_not_touch_shared_branch(tmp_path, monkeypatch):
    """Default OFF must not create a branch / PR and must dispatch with no
    extra_formula_kwargs (the existing per-child-PR path, unchanged)."""
    epic_id = "rig-epic2"
    run_dir = tmp_path / ".planning" / "agentic-epic" / epic_id
    plan = {"children": [{"key": "1", "title": "a", "description": "d", "depends_on": []}]}
    _patch_common(monkeypatch, run_dir, plan)

    def boom(*a, **k):
        pytest.fail("default-off must not call shared_branch transport")

    monkeypatch.setattr(ae.sb, "create_integration_branch", boom)
    monkeypatch.setattr(ae.sb, "open_draft_pr", boom)
    monkeypatch.setattr(ae.sb, "mark_pr_ready", boom)

    dispatched: dict = {}
    monkeypatch.setattr(ae, "graph_run", lambda **k: dispatched.update(k) or {"status": "ok"})

    result = ae.agentic_epic.fn(epic_id=epic_id, rig="rig", rig_path=str(tmp_path))

    assert dispatched["extra_formula_kwargs"] is None
    assert result["shared_branch"] is False
    assert result["epic_branch"] is None


def test_agentic_epic_shared_branch_dry_run(tmp_path, monkeypatch):
    """Dry-run shows the epic branch + draft-PR intent + parallel/serial lanes
    without creating beads, branches, or dispatching (AC1)."""
    epic_id = "rig-epic3"
    run_dir = tmp_path / ".planning" / "agentic-epic" / epic_id
    plan = {
        "children": [
            {"key": "1", "title": "a", "description": "d", "depends_on": []},
            {"key": "2", "title": "b", "description": "d", "depends_on": []},
            {"key": "3", "title": "c", "description": "d", "depends_on": ["1", "2"]},
        ]
    }
    _patch_common(monkeypatch, run_dir, plan)
    monkeypatch.setattr(
        ae.sb, "create_integration_branch",
        lambda *a, **k: pytest.fail("dry-run must not create a branch"),
    )
    monkeypatch.setattr(ae, "graph_run", lambda **k: pytest.fail("dry-run must not dispatch"))

    result = ae.agentic_epic.fn(
        epic_id=epic_id, rig="rig", rig_path=str(tmp_path), shared_branch=True, dry_run=True
    )
    assert result["status"] == "dry-run"
    assert result["shared_branch"] is True
    assert result["epic_branch"] == f"epic/{epic_id}"
    assert result["lanes"] == [["1", "2"], ["3"]]
