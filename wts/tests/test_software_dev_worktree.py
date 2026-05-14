from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import logging

import pytest

import po_formulas_wts.software_dev as sd_mod
import po_formulas_wts.worktree as wt_mod


def _fake_agent_step(**_kwargs: object) -> SimpleNamespace:
    return SimpleNamespace(verdict="approved", summary="", bead_id="step")


def test_shared_epic_context_skips_child_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = tmp_path / "rig"
    main.mkdir()
    shared = tmp_path / "rig.wt-epic"
    shared.mkdir()
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(sd_mod, "_agent_step_task", _fake_agent_step)
    monkeypatch.setattr(sd_mod, "get_run_logger", lambda: logging.getLogger(__name__))
    monkeypatch.setattr(
        sd_mod,
        "_read_triage_flags",
        lambda _rig_path, _seed_id: {"complexity": "simple"},
    )
    monkeypatch.setattr(wt_mod, "_is_git_repo", lambda _path: True)
    monkeypatch.setattr(
        wt_mod,
        "setup_worktree",
        lambda _path, issue_id, **_kw: calls.append(("setup", issue_id)) or shared,
    )
    monkeypatch.setattr(
        wt_mod,
        "merge_worktree",
        lambda _path, issue_id, **_kw: calls.append(("merge", issue_id)) or "main",
    )
    monkeypatch.setattr(sd_mod, "_stamp_metadata", lambda *_args, **_kw: None)
    monkeypatch.setattr(sd_mod, "_bd_metadata", lambda *_args, **_kw: {})

    out = sd_mod.software_dev_full.fn(
        issue_id="child-1",
        rig="rig",
        rig_path=str(main),
        claim=False,
        dry_run=True,
        parent_epic_worktree=str(shared),
        parent_epic_branch="wts-epic",
        parent_epic_id="epic",
        parent_epic_merge_target="main",
    )

    assert out["status"] == "completed"
    assert out["epic_managed_worktree"] is True
    assert calls == []


def test_retry_uses_existing_worktree_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = tmp_path / "rig"
    main.mkdir()
    shared = tmp_path / "rig.wt-epic"
    shared.mkdir()

    monkeypatch.setattr(
        sd_mod,
        "_bd_metadata",
        lambda *_args, **_kw: {
            "work_dir": str(shared),
            "branch": "wts-epic",
            "merge_target_branch": "main",
            "epic_id": "epic",
        },
    )

    ctx = sd_mod._resolve_epic_worktree_context(
        "child-1",
        main,
        parent_epic_worktree=None,
        parent_epic_branch=None,
        parent_epic_id=None,
        parent_epic_merge_target=None,
    )

    assert ctx == {
        "work_dir": str(shared.resolve()),
        "branch": "wts-epic",
        "merge_target_branch": "main",
        "epic_id": "epic",
    }


@pytest.mark.parametrize(
    "metadata",
    [
        {"work_dir": "/tmp/rig.wt-epic", "branch": "wts-epic"},
        {"work_dir": "/tmp/rig.wt-epic", "epic_id": "epic"},
        {"branch": "wts-epic", "epic_id": "epic"},
    ],
)
def test_retry_ignores_incomplete_worktree_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, metadata: dict[str, str]
) -> None:
    main = tmp_path / "rig"
    main.mkdir()

    monkeypatch.setattr(sd_mod, "_bd_metadata", lambda *_args, **_kw: metadata)

    ctx = sd_mod._resolve_epic_worktree_context(
        "child-1",
        main,
        parent_epic_worktree=None,
        parent_epic_branch=None,
        parent_epic_id=None,
        parent_epic_merge_target=None,
    )

    assert ctx is None


def test_shared_epic_context_stamps_retry_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = tmp_path / "rig"
    main.mkdir()
    shared = tmp_path / "rig.wt-epic"
    shared.mkdir()
    stamped: dict[str, str] = {}
    claimed: list[str] = []
    closed: list[str] = []

    monkeypatch.setattr(sd_mod, "_agent_step_task", _fake_agent_step)
    monkeypatch.setattr(sd_mod, "get_run_logger", lambda: logging.getLogger(__name__))
    monkeypatch.setattr(
        sd_mod,
        "_read_triage_flags",
        lambda _rig_path, _seed_id: {"complexity": "simple"},
    )
    monkeypatch.setattr(sd_mod, "_bd_metadata", lambda *_args, **_kw: {})
    monkeypatch.setattr(
        sd_mod,
        "_stamp_metadata",
        lambda _issue_id, _rig_path, values: stamped.update(values),
    )
    monkeypatch.setattr(
        sd_mod,
        "claim_issue",
        lambda issue_id, **_kwargs: claimed.append(issue_id),
    )
    monkeypatch.setattr(
        sd_mod,
        "close_issue",
        lambda issue_id, **_kwargs: closed.append(issue_id),
    )

    out = sd_mod.software_dev_full.fn(
        issue_id="child-1",
        rig="rig",
        rig_path=str(main),
        claim=True,
        dry_run=False,
        parent_epic_worktree=str(shared),
        parent_epic_branch="wts-epic",
        parent_epic_id="epic",
        parent_epic_merge_target="release",
    )

    assert out["status"] == "completed"
    assert out["epic_managed_worktree"] is True
    assert claimed == ["child-1"]
    assert closed == ["child-1"]
    assert stamped == {
        "work_dir": str(shared.resolve()),
        "branch": "wts-epic",
        "merge_target_branch": "release",
        "epic_id": "epic",
    }


def test_fast_uses_parent_epic_worktree_when_provided(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = tmp_path / "rig"
    main.mkdir()
    shared = tmp_path / "rig.wt-epic"
    shared.mkdir()
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(sd_mod, "_agent_step_task", _fake_agent_step)
    monkeypatch.setattr(sd_mod, "get_run_logger", lambda: logging.getLogger(__name__))
    monkeypatch.setattr(wt_mod, "_is_git_repo", lambda _path: True)
    monkeypatch.setattr(
        wt_mod,
        "setup_worktree",
        lambda _path, issue_id, **_kw: calls.append(("setup", issue_id)) or shared,
    )
    monkeypatch.setattr(sd_mod, "_stamp_metadata", lambda *_args, **_kw: None)
    monkeypatch.setattr(sd_mod, "_bd_metadata", lambda *_args, **_kw: {})

    out = sd_mod.software_dev_fast.fn(
        issue_id="child-1",
        rig="rig",
        rig_path=str(main),
        claim=False,
        dry_run=True,
        parent_epic_worktree=str(shared),
        parent_epic_branch="wts-epic",
        parent_epic_id="epic",
        parent_epic_merge_target="main",
    )

    assert out["mode"] == "fast"
    assert calls == []


def test_edit_uses_parent_epic_worktree_when_provided(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = tmp_path / "rig"
    main.mkdir()
    shared = tmp_path / "rig.wt-epic"
    shared.mkdir()
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(sd_mod, "_agent_step_task", _fake_agent_step)
    monkeypatch.setattr(sd_mod, "get_run_logger", lambda: logging.getLogger(__name__))
    monkeypatch.setattr(wt_mod, "_is_git_repo", lambda _path: True)
    monkeypatch.setattr(
        wt_mod,
        "setup_worktree",
        lambda _path, issue_id, **_kw: calls.append(("setup", issue_id)) or shared,
    )
    monkeypatch.setattr(sd_mod, "_stamp_metadata", lambda *_args, **_kw: None)
    monkeypatch.setattr(sd_mod, "_bd_metadata", lambda *_args, **_kw: {})

    out = sd_mod.software_dev_edit.fn(
        issue_id="child-1",
        rig="rig",
        rig_path=str(main),
        claim=False,
        dry_run=True,
        parent_epic_worktree=str(shared),
        parent_epic_branch="wts-epic",
        parent_epic_id="epic",
        parent_epic_merge_target="main",
    )

    assert out["mode"] == "edit"
    assert calls == []


def test_standalone_full_wts_still_creates_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = tmp_path / "rig"
    main.mkdir()
    child_wt = tmp_path / "rig.wt-child"
    child_wt.mkdir()
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(sd_mod, "_agent_step_task", _fake_agent_step)
    monkeypatch.setattr(sd_mod, "get_run_logger", lambda: logging.getLogger(__name__))
    monkeypatch.setattr(
        sd_mod,
        "_read_triage_flags",
        lambda _rig_path, _seed_id: {"complexity": "simple"},
    )
    monkeypatch.setattr(sd_mod, "_bd_metadata", lambda *_args, **_kw: {})
    monkeypatch.setattr(wt_mod, "_is_git_repo", lambda _path: True)
    monkeypatch.setattr(
        wt_mod,
        "setup_worktree",
        lambda _path, issue_id, **_kw: calls.append(("setup", issue_id)) or child_wt,
    )
    monkeypatch.setattr(
        wt_mod,
        "merge_worktree",
        lambda _path, issue_id, **_kw: calls.append(("merge", issue_id)) or "main",
    )

    out = sd_mod.software_dev_full.fn(
        issue_id="child-1",
        rig="rig",
        rig_path=str(main),
        claim=False,
        dry_run=True,
    )

    assert out["status"] == "completed"
    assert out["epic_managed_worktree"] is False
    assert calls == [("setup", "child-1"), ("merge", "child-1")]
