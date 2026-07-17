"""Regression coverage for target-branch, integration, PR, and preview truth."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from po_formulas import delivery_truth as dt


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "base.txt").write_text("base\n")
    _git(tmp_path, "add", "base.txt")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "branch", "release")
    _git(tmp_path, "switch", "-c", "agentic-seed", "release")
    (tmp_path / "child.txt").write_text("child\n")
    _git(tmp_path, "add", "child.txt")
    _git(tmp_path, "commit", "-m", "child")
    return tmp_path


def test_branch_truth_proves_custom_base_and_rejects_wrong_ancestry(repo: Path) -> None:
    truth = dt.branch_truth(repo, branch="agentic-seed", base_branch="release")
    assert truth["head_sha"] == _git(repo, "rev-parse", "agentic-seed")

    unrelated = _git(repo, "commit-tree", "release^{tree}", "-m", "unrelated")
    _git(repo, "branch", "unrelated", unrelated)
    with pytest.raises(dt.DeliveryTruthError, match="ancestry mismatch"):
        dt.branch_truth(repo, branch="unrelated", base_branch="release")


def test_worktree_for_branch_resolves_feature_checkout_not_root(repo: Path) -> None:
    """Regression: post-worker proof must not read the unchanged root checkout."""
    _git(repo, "switch", "main")
    feature_checkout = repo.parent / "feature-checkout"
    _git(repo, "worktree", "add", str(feature_checkout), "agentic-seed")

    assert not (repo / "child.txt").exists()
    assert (feature_checkout / "child.txt").read_text() == "child\n"
    assert dt.worktree_for_branch(repo, "agentic-seed") == feature_checkout.resolve()


def test_integration_truth_rejects_child_falsely_marked_integrated(repo: Path) -> None:
    _git(repo, "branch", "epic/e1", "release")
    with pytest.raises(dt.DeliveryTruthError, match="child is not integrated"):
        dt.integration_truth(
            repo,
            child_branch="agentic-seed",
            integration_branch="epic/e1",
            base_branch="release",
        )

    _git(repo, "switch", "epic/e1")
    _git(repo, "merge", "--ff-only", "agentic-seed")
    truth = dt.integration_truth(
        repo,
        child_branch="agentic-seed",
        integration_branch="epic/e1",
        base_branch="release",
    )
    assert truth["integration_sha"] == truth["child_sha"]


def test_pull_request_truth_rejects_pr_to_main_for_release_target(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dt.shutil, "which", lambda name: "/usr/bin/gh")
    payload = json.dumps(
        {
            "number": 7,
            "url": "https://example.test/pull/7",
            "headRefName": "agentic-seed",
            "baseRefName": "main",
            "state": "OPEN",
        }
    )
    monkeypatch.setattr(
        dt,
        "_run",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 0, payload, ""),
    )
    with pytest.raises(dt.DeliveryTruthError, match="expected release, found main"):
        dt.pull_request_truth(repo, head_branch="agentic-seed", target_branch="release")


def test_localhost_preview_rejects_wrong_app_and_stale_revision(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dt, "_listening_pid", lambda port: 123)
    real_resolve = Path.resolve
    wrong = tmp_path.parent / "wrong-app"
    wrong.mkdir()

    def wrong_resolve(path: Path, strict: bool = False) -> Path:
        if str(path) == "/proc/123/cwd":
            return wrong
        return real_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", wrong_resolve)
    with pytest.raises(dt.DeliveryTruthError, match="preview app mismatch"):
        dt.localhost_preview_truth(
            "http://localhost:8123", expected_repo=repo, expected_revision="sha"
        )

    def repo_resolve(path: Path, strict: bool = False) -> Path:
        if str(path) == "/proc/123/cwd":
            return repo
        return real_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", repo_resolve)
    with pytest.raises(dt.DeliveryTruthError, match="stale preview revision"):
        dt.localhost_preview_truth(
            "http://localhost:8123", expected_repo=repo, expected_revision="wrong-sha"
        )
