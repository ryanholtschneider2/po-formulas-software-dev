from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import po_formulas_wts.epic_wts as epic_wts_mod
import po_formulas_wts.epic_finalize as finalize_mod
import po_formulas_wts.graph as graph_mod


class _Future:
    def __init__(self, value: dict[str, Any]) -> None:
        self.value = value

    def result(self, raise_on_failure: bool = False) -> dict[str, Any]:
        return self.value


class _RunNodeTask:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def submit(self, formula_callable: Any, **kwargs: Any) -> _Future:
        call_kwargs = dict(kwargs)
        call_kwargs.pop("wait_for", None)
        self.calls.append(call_kwargs)
        return _Future(formula_callable(**call_kwargs))


def test_dispatch_nodes_filters_extra_formula_kwargs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _RunNodeTask()
    monkeypatch.setattr(graph_mod, "_run_node_task", runner)
    monkeypatch.setattr(
        graph_mod,
        "topo_sort_blocks",
        lambda nodes: nodes,
    )
    monkeypatch.setattr(
        graph_mod,
        "_resolve_per_bead_formula",
        lambda _node, default_callable, **_kw: default_callable,
    )

    def formula(
        issue_id: str,
        rig: str,
        rig_path: str,
        parent_epic_worktree: str | None = None,
    ) -> dict[str, Any]:
        return {
            "issue_id": issue_id,
            "worktree": parent_epic_worktree,
            "rig": rig,
            "rig_path": rig_path,
        }

    out = graph_mod._dispatch_nodes(
        nodes=[{"id": "child-1", "block_deps": []}],
        rig="rig",
        rig_path="/tmp/rig",
        formula_callable=formula,
        parent_bead="epic",
        iter_caps={},
        dry_run=False,
        max_issues=None,
        logger=SimpleNamespace(info=lambda *_a, **_kw: None),
        extra_formula_kwargs={
            "parent_epic_worktree": "/tmp/rig.wt-epic",
            "not_accepted": "ignored",
        },
    )

    assert out["results"]["child-1"]["worktree"] == "/tmp/rig.wt-epic"
    assert "not_accepted" not in runner.calls[0]


def test_epic_wts_sets_up_one_shared_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = tmp_path / "rig"
    rig.mkdir()
    shared = tmp_path / "rig.wt-epic"
    shared.mkdir()
    setup_calls: list[tuple[Path, str, bool]] = []
    metadata: dict[str, str] = {}
    epic_run_kwargs: dict[str, Any] = {}
    finalize_kwargs: dict[str, Any] = {}

    def fake_setup(rig_path: Path, epic_id: str, *, for_epic: bool = False) -> Path:
        setup_calls.append((Path(rig_path), epic_id, for_epic))
        return shared

    def fake_epic_run(**kwargs: Any) -> dict[str, Any]:
        epic_run_kwargs.update(kwargs)
        return {"status": "ok", "results": {"child-1": {"status": "completed"}}}

    def fake_finalize(**kwargs: Any) -> dict[str, Any]:
        finalize_kwargs.update(kwargs)
        return {"status": "PASSED", "failures": []}

    monkeypatch.setattr(epic_wts_mod, "setup_worktree", fake_setup)
    monkeypatch.setattr(
        epic_wts_mod,
        "get_run_logger",
        lambda: logging.getLogger(__name__),
    )
    monkeypatch.setattr(epic_wts_mod, "epic_run", fake_epic_run)
    monkeypatch.setattr(
        epic_wts_mod,
        "pre_pr_review",
        lambda **_kwargs: {"validation": "passed", "bead_ids": []},
    )
    monkeypatch.setattr(epic_wts_mod, "epic_finalize", fake_finalize)
    monkeypatch.setattr(
        epic_wts_mod,
        "pr_writer",
        lambda **_kwargs: {"verdict": "PASS", "branch": _kwargs.get("branch")},
    )
    monkeypatch.setattr(
        epic_wts_mod,
        "_stamp_metadata",
        lambda _epic_id, _rig_path, values: metadata.update(values),
    )

    out = epic_wts_mod.epic_wts.fn(
        epic_id="epic",
        rig="rig",
        rig_path=str(rig),
        skip_remote_ci=True,
    )

    assert out["verdict"] == "passed"
    assert setup_calls == [(rig.resolve(), "epic", True)]
    assert metadata["work_dir"] == str(shared)
    assert metadata["branch"] == "wts-epic"
    assert epic_run_kwargs["parent_epic_worktree"] == str(shared)
    assert epic_run_kwargs["parent_epic_branch"] == "wts-epic"
    assert finalize_kwargs["worktree_path"] == str(shared)
    assert finalize_kwargs["branch"] == "wts-epic"


def test_parallel_epics_isolated(tmp_path: Path) -> None:
    rig = tmp_path / "rig"
    rig.mkdir()
    first = epic_wts_mod.WorktreePaths.for_epic(rig, "epic.one")
    second = epic_wts_mod.WorktreePaths.for_epic(rig, "epic.two")

    assert first.worktree != second.worktree
    assert first.branch != second.branch
    assert first.worktree.name == "rig.wt-epic_one"
    assert second.worktree.name == "rig.wt-epic_two"


def test_epic_finalize_merges_shared_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = tmp_path / "rig"
    rig.mkdir()
    shared = tmp_path / "rig.wt-epic"
    shared.mkdir()
    merge_calls: list[dict[str, Any]] = []
    closed: list[str] = []

    monkeypatch.setattr(finalize_mod, "list_epic_children", lambda *_a, **_kw: [])
    monkeypatch.setattr(
        finalize_mod,
        "get_run_logger",
        lambda: logging.getLogger(__name__),
    )
    monkeypatch.setattr(finalize_mod, "_run_make", lambda *_a, **_kw: (0, "ok"))
    monkeypatch.setattr(
        finalize_mod,
        "_agent_step_task",
        lambda **_kwargs: SimpleNamespace(verdict="approved", summary=""),
    )
    monkeypatch.setattr(
        finalize_mod,
        "merge_worktree",
        lambda *args, **kwargs: merge_calls.append(
            {"args": args, "kwargs": kwargs}
        )
        or "main",
    )
    monkeypatch.setattr(
        finalize_mod,
        "close_issue",
        lambda epic_id, **_kwargs: closed.append(epic_id),
    )

    out = finalize_mod.epic_finalize.fn(
        epic_id="epic",
        rig="rig",
        rig_path=str(rig),
        spec_path="",
        skip_walkthrough=True,
        skip_demo_video=True,
        skip_remote_ci=True,
        worktree_path=str(shared),
        branch="wts-epic",
        merge_target_branch="main",
    )

    assert out["status"] == "PASSED"
    assert out["merged_into"] == "main"
    assert closed == ["epic"]
    assert merge_calls == [
        {
            "args": (rig.resolve(), "epic"),
            "kwargs": {
                "target_branch": "main",
                "cleanup": True,
                "for_epic": True,
            },
        }
    ]
