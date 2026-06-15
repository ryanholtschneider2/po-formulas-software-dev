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
                {
                    "key": "1",
                    "title": "add model",
                    "description": "d1",
                    "depends_on": [],
                },
                {
                    "key": "2",
                    "title": "wire route",
                    "description": "d2",
                    "depends_on": ["1"],
                },
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
        (
            {"children": [{"key": "1", "title": "", "description": "d"}]},
            "missing a title",
        ),
        (
            {
                "children": [
                    {"key": "1", "title": "t", "description": "d"},
                    {"key": "1", "title": "u", "description": "d"},
                ]
            },
            "missing/duplicate key",
        ),
        (
            {
                "children": [
                    {"key": "1", "title": "t", "description": "d", "depends_on": ["9"]}
                ]
            },
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
        {
            "children": [
                {"key": str(i), "title": "t", "description": "d"} for i in range(5)
            ]
        },
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
            {
                "key": "2",
                "title": "wire route",
                "description": "d2",
                "depends_on": ["1"],
            },
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
        lambda *, parent_id, child_id, **k: (
            created.append((parent_id, child_id)) or child_id
        ),
    )
    monkeypatch.setattr(
        ae, "_bd_set_metadata", lambda i, k, v, rp: stamped.append((i, k, v))
    )
    monkeypatch.setattr(ae, "_bd_dep_add", lambda c, d, rp: deps.append((c, d)))
    monkeypatch.setattr(
        ae, "graph_run", lambda **k: dispatched.update(k) or {"status": "ok"}
    )
    monkeypatch.setattr(ae, "close_issue", lambda i, **k: closed.append(i))

    result = ae.agentic_epic.fn(
        epic_id=epic_id,
        rig="rig",
        rig_path=str(rig_path),
        iter_cap=2,
        plan_iter_cap=2,
        shared_branch=False,  # exercise the legacy per-child-PR path explicitly
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


def test_agentic_epic_idempotent_reuses_existing_children(tmp_path, monkeypatch):
    """A repeat dispatch with planned children already under the epic must NOT
    re-decompose: no `agent_step`, no new beads. It reuses the existing set and
    re-dispatches. Guards the 2026-06-14 runaway (the br backend mints fresh
    child ids per run, so re-decomposition duplicated the whole child set)."""
    epic_id = "rig-epic9"
    existing = ["rig-c1", "rig-c2", "rig-c3"]

    def boom_agent_step(*a, **k):
        pytest.fail("idempotent re-run must not re-decompose (agent_step called)")

    def boom_create(*a, **k):
        pytest.fail("idempotent re-run must not create new child beads")

    dispatched: dict = {}
    closed: list[str] = []

    monkeypatch.setattr(ae, "get_run_logger", lambda: _NULL_LOGGER)
    monkeypatch.setattr(ae, "claim_issue", lambda *a, **k: None)
    monkeypatch.setattr(ae, "_bd_show_description", lambda *a, **k: "goal")
    monkeypatch.setattr(
        ae, "_existing_planned_children", lambda eid, rp: list(existing)
    )
    monkeypatch.setattr(ae, "agent_step", boom_agent_step)
    monkeypatch.setattr(ae, "create_child_bead", boom_create)
    monkeypatch.setattr(
        ae, "graph_run", lambda **k: dispatched.update(k) or {"status": "ok"}
    )
    monkeypatch.setattr(ae, "close_issue", lambda i, **k: closed.append(i))

    result = ae.agentic_epic.fn(
        epic_id=epic_id,
        rig="rig",
        rig_path=str(tmp_path),
        shared_branch=False,
    )

    # Reused the existing children, dispatched them, closed the epic — no re-plan.
    assert dispatched["root_id"] == epic_id
    assert result["children"] == existing
    assert closed == [epic_id]


def test_agentic_epic_force_replan_redecomposes(tmp_path, monkeypatch):
    """`force_replan=True` ignores existing children and decomposes fresh."""
    epic_id = "rig-epic8"
    plan = {
        "children": [{"key": "1", "title": "t", "description": "d", "depends_on": []}]
    }

    def fake_agent_step(*, step, **kwargs):
        if step == "epic-plan":
            run_dir = tmp_path / ".planning" / "agentic-epic" / epic_id
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / ae._PLAN_FILE).write_text(json.dumps(plan))

        class _R:
            verdict = "pass"
            closed_by = "agent"

        return _R()

    created: list[str] = []

    monkeypatch.setattr(ae, "get_run_logger", lambda: _NULL_LOGGER)
    monkeypatch.setattr(ae, "claim_issue", lambda *a, **k: None)
    monkeypatch.setattr(ae, "_bd_show_description", lambda *a, **k: "goal")
    # Existing children present, but force_replan must ignore them entirely.
    monkeypatch.setattr(ae, "_existing_planned_children", lambda eid, rp: ["old-1"])
    monkeypatch.setattr(ae, "agent_step", fake_agent_step)
    monkeypatch.setattr(
        ae,
        "create_child_bead",
        lambda *, parent_id, child_id, **k: created.append(child_id) or child_id,
    )
    monkeypatch.setattr(ae, "_bd_set_metadata", lambda *a, **k: None)
    monkeypatch.setattr(ae, "_bd_dep_add", lambda *a, **k: None)
    monkeypatch.setattr(ae, "graph_run", lambda **k: {"status": "ok"})
    monkeypatch.setattr(ae, "close_issue", lambda *a, **k: None)

    result = ae.agentic_epic.fn(
        epic_id=epic_id,
        rig="rig",
        rig_path=str(tmp_path),
        shared_branch=False,
        force_replan=True,
    )

    # Re-decomposed: created the fresh child, did not reuse "old-1".
    assert created == [f"{epic_id}.1"]
    assert result["children"] == [f"{epic_id}.1"]


def test_agentic_epic_dry_run_skips_creation(tmp_path, monkeypatch):
    monkeypatch.setattr(ae, "get_run_logger", lambda: _NULL_LOGGER)
    monkeypatch.setattr(
        ae,
        "agent_step",
        lambda **k: type("R", (), {"verdict": "pass", "closed_by": "x"})(),
    )
    monkeypatch.setattr(ae, "_bd_show_description", lambda *a, **k: "goal")
    monkeypatch.setattr(
        ae,
        "create_child_bead",
        lambda **k: pytest.fail("dry-run must not create beads"),
    )
    monkeypatch.setattr(
        ae, "graph_run", lambda **k: pytest.fail("dry-run must not dispatch")
    )

    result = ae.agentic_epic.fn(
        epic_id="e1", rig="r", rig_path=str(tmp_path), dry_run=True
    )
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
        return {
            "branch": f"epic/{eid}",
            "created": True,
            "pushed": True,
            "remote": True,
        }

    def fake_pr(rp, **k):
        calls["pr"] = k
        return {"opened": True, "url": "https://x/pull/9", "reason": ""}

    def boom_ready(rp, **k):
        pytest.fail("mark_pr_ready must not be called — PR is opened ready at finalize")

    monkeypatch.setattr(ae.sb, "create_integration_branch", fake_create)
    monkeypatch.setattr(ae.sb, "open_draft_pr", fake_pr)
    monkeypatch.setattr(ae.sb, "mark_pr_ready", boom_ready)
    monkeypatch.setattr(
        ae.sb,
        "commits_ahead",
        lambda rp, base, branch: calls.__setitem__("ahead", (base, branch)) or 2,
    )
    monkeypatch.setattr(
        ae.sb,
        "cleanup_integration_worktree",
        lambda rp, eid: calls.__setitem__("cleanup", str(eid)),
    )
    dispatched: dict = {}
    monkeypatch.setattr(
        ae, "graph_run", lambda **k: dispatched.update(k) or {"status": "ok"}
    )

    result = ae.agentic_epic.fn(
        epic_id=epic_id, rig="rig", rig_path=str(tmp_path), shared_branch=True
    )

    # One integration branch off main; NO PR opened upfront (calls["pr"] only set
    # at finalize). Children fanned out with epic_branch / parent_epic_id threaded.
    assert calls["create"][0] == epic_id
    assert calls["create"][1]["base_branch"] == "main"
    assert dispatched["extra_formula_kwargs"] == {
        "epic_branch": f"epic/{epic_id}",
        "parent_epic_id": epic_id,
    }
    assert dispatched["formula"] == "software-dev-agentic"
    # Finalize: checks commits ahead, then opens ONE ready PR (draft=False) and
    # cleans the integration worktree.
    assert calls["ahead"] == ("main", f"epic/{epic_id}")
    assert calls["pr"]["branch"] == f"epic/{epic_id}"
    assert calls["pr"]["draft"] is False
    assert calls["cleanup"] == epic_id
    assert result["shared_branch"] is True
    assert result["epic_branch"] == f"epic/{epic_id}"
    assert result["pr"]["url"] == "https://x/pull/9"


def test_agentic_epic_shared_branch_no_pr_when_nothing_integrated(
    tmp_path, monkeypatch
):
    """If no child integrated a commit (commits_ahead==0), finalize opens NO PR."""
    epic_id = "rig-epic-empty"
    run_dir = tmp_path / ".planning" / "agentic-epic" / epic_id
    plan = {
        "children": [{"key": "1", "title": "a", "description": "d", "depends_on": []}]
    }
    _patch_common(monkeypatch, run_dir, plan)

    monkeypatch.setattr(
        ae.sb,
        "create_integration_branch",
        lambda rp, eid, **k: {
            "branch": f"epic/{eid}",
            "created": True,
            "pushed": True,
            "remote": True,
        },
    )
    monkeypatch.setattr(ae.sb, "commits_ahead", lambda rp, base, branch: 0)

    def boom_pr(*a, **k):
        pytest.fail("no commits ahead → must not open a PR")

    monkeypatch.setattr(ae.sb, "open_draft_pr", boom_pr)
    monkeypatch.setattr(ae.sb, "cleanup_integration_worktree", lambda rp, eid: None)
    monkeypatch.setattr(ae, "graph_run", lambda **k: {"status": "ok"})

    result = ae.agentic_epic.fn(
        epic_id=epic_id, rig="rig", rig_path=str(tmp_path), shared_branch=True
    )
    assert result["pr"]["opened"] is False
    assert "no children integrated" in result["pr"]["reason"]


def test_agentic_epic_acceptance_fail_opens_draft(tmp_path, monkeypatch):
    """Epic acceptance-critic FAIL → the single PR is opened as a DRAFT (so the
    sheriff can't auto-merge a gapped epic), with the verdict surfaced."""
    epic_id = "rig-epic-accept-fail"
    run_dir = tmp_path / ".planning" / "agentic-epic" / epic_id
    plan = {"children": [{"key": "1", "title": "a", "description": "d", "depends_on": []}]}
    _patch_common(monkeypatch, run_dir, plan)

    # Planning steps pass; the acceptance critic FAILS.
    def fake_agent_step(*, agent_dir, step, **kwargs):
        if step == "epic-plan":
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / ae._PLAN_FILE).write_text(json.dumps(plan))
        v = "fail" if step == "epic-acceptance-critic" else "pass"
        return type("R", (), {"verdict": v, "closed_by": "agent"})()

    monkeypatch.setattr(ae, "agent_step", fake_agent_step)
    monkeypatch.setattr(
        ae.sb, "create_integration_branch",
        lambda rp, eid, **k: {"branch": f"epic/{eid}", "created": True, "pushed": True, "remote": True},
    )
    monkeypatch.setattr(ae.sb, "commits_ahead", lambda rp, base, branch: 3)
    pr_calls: dict = {}
    monkeypatch.setattr(
        ae.sb, "open_draft_pr",
        lambda rp, **k: pr_calls.update(k) or {"opened": True, "url": "https://x/pull/5", "reason": ""},
    )
    monkeypatch.setattr(ae.sb, "cleanup_integration_worktree", lambda rp, eid: None)
    monkeypatch.setattr(ae, "graph_run", lambda **k: {"status": "ok", "results": {}})

    result = ae.agentic_epic.fn(
        epic_id=epic_id, rig="rig", rig_path=str(tmp_path), shared_branch=True
    )
    assert pr_calls["draft"] is True            # FAIL → draft
    assert "FAIL" in pr_calls["body"]           # verdict surfaced in the PR body
    assert result["pr"]["acceptance_verdict"] == "fail"


def test_integration_summary_marks_landed_and_dropped():
    dispatch = {
        "results": {
            "c1": {"integration": {"merged": True}},
            "c2": {"integration": {"merged": False, "conflict": True, "reason": "merge conflict: IdeaCard.tsx"}},
            "c3": RuntimeError("did not converge after 2 iter(s) — critic=fail"),
            "c4": {"integration": {"merged": False, "conflict": False, "reason": "skipped"}},
        }
    }
    s = ae._integration_summary(dispatch)
    assert "`c1`: LANDED" in s
    assert "`c2`: DROPPED — merge conflict" in s
    assert "`c3`: FAILED" in s and "critic=fail" in s
    assert "`c4`: DROPPED — not integrated" in s
    assert "no per-child results" in ae._integration_summary({})


def test_agentic_epic_default_is_shared_branch(tmp_path, monkeypatch):
    """The default (no shared_branch kwarg) now lands the epic on ONE integration
    branch + draft PR — shared-branch is the dispatch path, not opt-in."""
    epic_id = "rig-epic-default"
    run_dir = tmp_path / ".planning" / "agentic-epic" / epic_id
    plan = {
        "children": [{"key": "1", "title": "a", "description": "d", "depends_on": []}]
    }
    _patch_common(monkeypatch, run_dir, plan)

    seen: dict = {}
    monkeypatch.setattr(
        ae.sb,
        "create_integration_branch",
        lambda rp, eid, **k: (
            seen.__setitem__("create", str(eid))
            or {
                "branch": f"epic/{eid}",
                "created": True,
                "pushed": True,
                "remote": True,
            }
        ),
    )
    monkeypatch.setattr(
        ae.sb,
        "open_draft_pr",
        lambda rp, **k: {"opened": True, "url": "https://x/pull/1", "reason": ""},
    )
    monkeypatch.setattr(
        ae.sb, "mark_pr_ready", lambda rp, **k: {"ready": True, "reason": ""}
    )
    monkeypatch.setattr(ae.sb, "cleanup_integration_worktree", lambda rp, eid: None)
    dispatched: dict = {}
    monkeypatch.setattr(
        ae, "graph_run", lambda **k: dispatched.update(k) or {"status": "ok"}
    )

    # No shared_branch kwarg → default path.
    result = ae.agentic_epic.fn(epic_id=epic_id, rig="rig", rig_path=str(tmp_path))

    assert seen["create"] == epic_id
    assert result["shared_branch"] is True
    assert result["epic_branch"] == f"epic/{epic_id}"
    assert dispatched["extra_formula_kwargs"] == {
        "epic_branch": f"epic/{epic_id}",
        "parent_epic_id": epic_id,
    }


def test_agentic_epic_shared_branch_false_opts_out(tmp_path, monkeypatch):
    """shared_branch=False must NOT create a branch / PR and must dispatch with no
    extra_formula_kwargs (the legacy per-child-PR path, unchanged)."""
    epic_id = "rig-epic2"
    run_dir = tmp_path / ".planning" / "agentic-epic" / epic_id
    plan = {
        "children": [{"key": "1", "title": "a", "description": "d", "depends_on": []}]
    }
    _patch_common(monkeypatch, run_dir, plan)

    def boom(*a, **k):
        pytest.fail("opt-out must not call shared_branch transport")

    monkeypatch.setattr(ae.sb, "create_integration_branch", boom)
    monkeypatch.setattr(ae.sb, "open_draft_pr", boom)
    monkeypatch.setattr(ae.sb, "mark_pr_ready", boom)

    dispatched: dict = {}
    monkeypatch.setattr(
        ae, "graph_run", lambda **k: dispatched.update(k) or {"status": "ok"}
    )

    result = ae.agentic_epic.fn(
        epic_id=epic_id, rig="rig", rig_path=str(tmp_path), shared_branch=False
    )

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
        ae.sb,
        "create_integration_branch",
        lambda *a, **k: pytest.fail("dry-run must not create a branch"),
    )
    monkeypatch.setattr(
        ae, "graph_run", lambda **k: pytest.fail("dry-run must not dispatch")
    )

    result = ae.agentic_epic.fn(
        epic_id=epic_id,
        rig="rig",
        rig_path=str(tmp_path),
        shared_branch=True,
        dry_run=True,
    )
    assert result["status"] == "dry-run"
    assert result["shared_branch"] is True
    assert result["epic_branch"] == f"epic/{epic_id}"
    assert result["lanes"] == [["1", "2"], ["3"]]


# ── _parse_plan: touches + per-child formula ─────────────────────────────────


def test_parse_plan_accepts_touches_and_formula(tmp_path):
    _write_plan(
        tmp_path,
        {
            "children": [
                {
                    "key": "1",
                    "title": "t",
                    "description": "d",
                    "touches": ["./parent/foo.py", "parent/bar.py/"],
                    "formula": "minimal-task",
                },
                {"key": "2", "title": "u", "description": "d"},  # defaults
            ]
        },
    )
    plan = ae._parse_plan(tmp_path, max_children=12)
    # Leading ./ and trailing / normalized for stable coupling comparison.
    assert plan[0]["touches"] == ["parent/foo.py", "parent/bar.py"]
    assert plan[0]["formula"] == "minimal-task"
    # Omitted touches/formula default sanely.
    assert plan[1]["touches"] == []
    assert plan[1]["formula"] == "software-dev-agentic"


def test_parse_plan_rejects_non_list_touches(tmp_path):
    _write_plan(
        tmp_path,
        {
            "children": [
                {"key": "1", "title": "t", "description": "d", "touches": "foo.py"}
            ]
        },
    )
    with pytest.raises(ValueError, match="non-list 'touches'"):
        ae._parse_plan(tmp_path, max_children=12)


# ── _blocks_edges (records the planner's declared deps) ──────────────────────


def test_blocks_edges_no_edges_between_independent_children():
    plan = [
        {"key": "1", "touches": ["a.py"], "depends_on": []},
        {"key": "2", "touches": ["b.py"], "depends_on": []},
    ]
    assert ae._blocks_edges(plan) == []


def test_blocks_edges_preserves_declared_dep_and_skips_already_ordered():
    # 1 and 2 share a file AND the planner already declared 2 depends_on 1 → keep
    # the one declared edge, do NOT add a duplicate/reverse coupling edge.
    plan = [
        {"key": "1", "touches": ["shared.py"], "depends_on": []},
        {"key": "2", "touches": ["shared.py"], "depends_on": ["1"]},
    ]
    assert ae._blocks_edges(plan) == [("2", "1")]


def test_blocks_edges_respects_transitive_ordering():
    # 1↔3 share a file but are already ordered transitively (3→2→1); no new edge.
    plan = [
        {"key": "1", "touches": ["x.py"], "depends_on": []},
        {"key": "2", "touches": ["m.py"], "depends_on": ["1"]},
        {"key": "3", "touches": ["x.py"], "depends_on": ["2"]},
    ]
    assert ae._blocks_edges(plan) == [("2", "1"), ("3", "2")]


# ── PRD phase + plan-critic loop ─────────────────────────────────────────────


def test_prd_runs_before_decomposition(tmp_path, monkeypatch):
    """The PRD step must execute before the planner step (it scopes the goal the
    planner decomposes)."""
    epic_id = "rig-prd1"
    run_dir = tmp_path / ".planning" / "agentic-epic" / epic_id
    plan = {
        "children": [{"key": "1", "title": "a", "description": "d", "depends_on": []}]
    }
    order: list[str] = []

    def fake_agent_step(*, agent_dir, step, **kwargs):
        order.append(step)
        if step == "epic-plan":
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / ae._PLAN_FILE).write_text(json.dumps(plan))
        return type("R", (), {"verdict": "pass", "closed_by": "agent"})()

    monkeypatch.setattr(ae, "get_run_logger", lambda: _NULL_LOGGER)
    monkeypatch.setattr(ae, "agent_step", fake_agent_step)
    monkeypatch.setattr(ae, "_bd_show_description", lambda *a, **k: "goal")
    monkeypatch.setattr(
        ae, "graph_run", lambda **k: pytest.fail("dry-run must not dispatch")
    )

    ae.agentic_epic.fn(epic_id=epic_id, rig="r", rig_path=str(tmp_path), dry_run=True)

    assert order[0] == "epic-prd"
    assert "epic-plan" in order
    assert order.index("epic-prd") < order.index("epic-plan")


def test_plan_critic_loop_iterates_on_bad_plan(tmp_path, monkeypatch):
    """A deliberately-bad first plan is rejected (critic fail), the planner is
    re-invoked with the revision note, and the second pass passes."""
    epic_id = "rig-iter1"
    run_dir = tmp_path / ".planning" / "agentic-epic" / epic_id
    run_dir.mkdir(parents=True, exist_ok=True)
    good_plan = {
        "children": [{"key": "1", "title": "a", "description": "d", "depends_on": []}]
    }

    planner_iters: list[int] = []
    revision_notes: list[str] = []
    critic_calls = {"n": 0}

    def fake_agent_step(*, agent_dir, step, ctx=None, iter_n=None, **kwargs):
        if step == "epic-plan":
            planner_iters.append(iter_n)
            revision_notes.append((ctx or {}).get("revision_note", ""))
            (run_dir / ae._PLAN_FILE).write_text(json.dumps(good_plan))
            return type("R", (), {"verdict": "complete", "closed_by": "agent"})()
        if step == "epic-plan-critic":
            critic_calls["n"] += 1
            # FAIL the first iteration, PASS the second.
            verdict = "fail" if critic_calls["n"] == 1 else "pass"
            (run_dir / f"critique-epic-plan-iter-{iter_n}.md").write_text(
                "FAIL: child 1 is too vague" if verdict == "fail" else "PASS"
            )
            return type("R", (), {"verdict": verdict, "closed_by": "agent"})()
        return type("R", (), {"verdict": "complete", "closed_by": "agent"})()

    _patch_common(monkeypatch, run_dir, good_plan)
    monkeypatch.setattr(ae, "agent_step", fake_agent_step)
    monkeypatch.setattr(
        ae.sb,
        "create_integration_branch",
        lambda rp, eid, **k: {
            "branch": f"epic/{eid}",
            "created": True,
            "pushed": True,
            "remote": True,
        },
    )
    monkeypatch.setattr(
        ae.sb,
        "open_draft_pr",
        lambda rp, **k: {"opened": True, "url": "https://x/pull/2", "reason": ""},
    )
    monkeypatch.setattr(
        ae.sb, "mark_pr_ready", lambda rp, **k: {"ready": True, "reason": ""}
    )
    monkeypatch.setattr(ae.sb, "cleanup_integration_worktree", lambda rp, eid: None)
    monkeypatch.setattr(ae, "graph_run", lambda **k: {"status": "ok"})

    result = ae.agentic_epic.fn(
        epic_id=epic_id, rig="r", rig_path=str(tmp_path), plan_iter_cap=2
    )

    # Planner ran twice; the second invocation carried the critic's fix list.
    assert planner_iters == [1, 2]
    assert "child 1 is too vague" in revision_notes[1]
    assert critic_calls["n"] == 2
    assert result["status"] == "completed"


def test_per_child_formula_is_stamped(tmp_path, monkeypatch):
    """A child carrying its own formula (minimal-task) is stamped with it, not the
    default — formula-per-bead-size."""
    epic_id = "rig-fmt1"
    run_dir = tmp_path / ".planning" / "agentic-epic" / epic_id
    plan = {
        "children": [
            {"key": "1", "title": "big", "description": "d", "depends_on": []},
            {
                "key": "2",
                "title": "trivial link",
                "description": "d",
                "depends_on": [],
                "formula": "minimal-task",
            },
        ]
    }
    _patch_common(monkeypatch, run_dir, plan)
    stamped: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        ae, "_bd_set_metadata", lambda i, k, v, rp: stamped.append((i, k, v))
    )
    monkeypatch.setattr(
        ae.sb,
        "create_integration_branch",
        lambda rp, eid, **k: {
            "branch": f"epic/{eid}",
            "created": True,
            "pushed": True,
            "remote": True,
        },
    )
    monkeypatch.setattr(
        ae.sb,
        "open_draft_pr",
        lambda rp, **k: {"opened": True, "url": "u", "reason": ""},
    )
    monkeypatch.setattr(
        ae.sb, "mark_pr_ready", lambda rp, **k: {"ready": True, "reason": ""}
    )
    monkeypatch.setattr(ae.sb, "cleanup_integration_worktree", lambda rp, eid: None)
    monkeypatch.setattr(ae, "graph_run", lambda **k: {"status": "ok"})

    ae.agentic_epic.fn(epic_id=epic_id, rig="r", rig_path=str(tmp_path))

    assert (f"{epic_id}.1", "po.formula", "software-dev-agentic") in stamped
    assert (f"{epic_id}.2", "po.formula", "minimal-task") in stamped
