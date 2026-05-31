from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import logging

import pytest

import po_formulas_wts.software_dev as sd_mod
import po_formulas_wts.worktree as wt_mod


def _fake_agent_step(**_kwargs: object) -> SimpleNamespace:
    return SimpleNamespace(verdict="approved", summary="", bead_id="step")


def test_sheriff_handoff_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = tmp_path / "rig"
    rig.mkdir()
    # Off by default (no .ade/settings.toml, no env).
    monkeypatch.delenv("PO_SHERIFF_HANDOFF", raising=False)
    assert sd_mod._sheriff_handoff_enabled(rig) is False
    # On via env.
    monkeypatch.setenv("PO_SHERIFF_HANDOFF", "1")
    assert sd_mod._sheriff_handoff_enabled(rig) is True
    monkeypatch.delenv("PO_SHERIFF_HANDOFF", raising=False)
    # On via .ade/settings.toml (ADE mode).
    (rig / ".ade").mkdir()
    (rig / ".ade" / "settings.toml").write_text("[involvement]\nmerge_mode='auto'\n")
    assert sd_mod._sheriff_handoff_enabled(rig) is True


def test_write_env_recipe_from_ade_settings(tmp_path: Path) -> None:
    import subprocess as _sp

    rig = tmp_path / "rig"
    rig.mkdir()
    _sp.run(["git", "init", "-q", str(rig)], check=True)
    _sp.run(["git", "-C", str(rig), "remote", "add", "origin", "https://github.com/me/app"], check=True)
    (rig / ".ade").mkdir()
    (rig / ".ade" / "settings.toml").write_text(
        "[env]\nbackend='hetzner'\nsetup_cmd='docker compose up -d'\n"
        "smoke_cmd='curl -sf localhost:8080/health'\nsmoke_instructions='Open :8080.'\n"
    )
    p = sd_mod._write_env_recipe(rig, "f-1", "wts-f_1", logging.getLogger(__name__))
    assert p is not None and p == rig / ".ade" / "envs" / "f-1.toml"
    text = p.read_text()
    assert 'backend = "hetzner"' in text
    assert 'repo = "https://github.com/me/app"' in text
    assert 'branch = "wts-f_1"' in text
    assert 'setup_cmd = "docker compose up -d"' in text
    assert 'box_id = ""' in text
    # never persist an insteadOf-injected credential into the recipe
    assert "@github.com" not in text and "ghp_" not in text


def test_write_env_recipe_no_ade_settings_still_writes(tmp_path: Path) -> None:
    rig = tmp_path / "rig"
    rig.mkdir()
    import subprocess as _sp

    _sp.run(["git", "init", "-q", str(rig)], check=True)
    p = sd_mod._write_env_recipe(rig, "f-2", "wts-f_2", logging.getLogger(__name__))
    assert p is not None
    text = p.read_text()
    assert 'feature_id = "f-2"' in text
    assert 'backend = ""' in text  # empty when no [env]


def test_handoff_to_sheriff_labels_review_and_triggers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = tmp_path / "rig"
    rig.mkdir()
    monkeypatch.setattr(sd_mod, "push_worktree_branch", None, raising=False)
    # stub the worktree push (imported lazily inside the helper)
    monkeypatch.setattr(
        wt_mod, "push_worktree_branch",
        lambda *_a, **_k: {"branch": "wts-f1", "pushed": False, "remote": False},
    )
    runs: list[list[str]] = []
    monkeypatch.setattr(sd_mod.subprocess, "run", lambda cmd, **_k: runs.append(cmd))
    popens: list[list[str]] = []

    class _P:
        def __init__(self, cmd, **_k):
            popens.append(cmd)

    monkeypatch.setattr(sd_mod.subprocess, "Popen", _P)
    out = sd_mod._handoff_to_sheriff(rig, "f1", logging.getLogger(__name__))
    assert out["branch"] == "wts-f1"
    assert out["sheriff_triggered"] is True
    assert any("--add-label" in c and "review" in c for c in runs)
    assert any("pr-sheriff" in c for c in popens)


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
