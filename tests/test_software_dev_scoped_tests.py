"""`software_dev_full` must write `tests-changed.txt` before tester /
regression-gate roles run, otherwise both fall through to running the
full project test suite (763+ tests in the prefect-orchestration rig)
and CPU-thrash when several flows share a rig.

This module covers the helper directly (no Prefect server needed) — the
helper is what the build loop calls right after the build agent_step.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from po_formulas.software_dev import _write_scoped_tests_artifact


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "rig"
    repo.mkdir()
    _git(repo, "init", "-b", "main", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("seed\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


def test_writes_artifact_with_smoke_only_when_no_diff(tmp_path: Path) -> None:
    """Cross-repo case (etl/dgr): rig has no commits in this branch
    because edits landed in a sibling repo. Artifact must still exist
    so agents skip their full-suite fallback; contents = smoke set."""
    repo = _init_repo(tmp_path)
    run_dir = tmp_path / "run"

    changed = _write_scoped_tests_artifact(repo, run_dir, force_full=False)

    artifact = run_dir / "tests-changed.txt"
    assert artifact.is_file(), "artifact must be written even when diff is empty"
    body = artifact.read_text()
    assert "__FULL__" not in body, "no diff → no __FULL__ sentinel"
    assert changed == [], "no commits since merge-base → empty changed list"


def test_writes_force_full_sentinel_when_caller_demands(tmp_path: Path) -> None:
    """`force_full_regression=True` (caller-set) writes __FULL__ so the
    agent runs the full suite — opt-in, not the default."""
    repo = _init_repo(tmp_path)
    run_dir = tmp_path / "run"

    _write_scoped_tests_artifact(repo, run_dir, force_full=True)

    body = (run_dir / "tests-changed.txt").read_text()
    assert "__FULL__" in body


def test_returns_changed_paths_for_regression_ctx(tmp_path: Path) -> None:
    """Return value feeds the regression-gate `ctx.changed_files` slot
    so the agent can summarize what it ran scoped tests for."""
    repo = _init_repo(tmp_path)
    # Diverge from main so merge-base..HEAD has content.
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "src.py").write_text("x = 1\n")
    _git(repo, "add", "src.py")
    _git(repo, "commit", "-q", "-m", "add src")

    changed = _write_scoped_tests_artifact(
        repo, tmp_path / "run", force_full=False,
    )
    assert "src.py" in changed


def test_artifact_write_failure_does_not_crash_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Belt-and-suspenders: the helper swallows exceptions so a transient
    git/IO error never breaks the build loop. Agents fall through to
    full-suite via the missing-artifact branch in their task.md."""
    from po_formulas import software_dev as mod

    def boom(*a: object, **kw: object) -> object:
        raise RuntimeError("simulated git failure")

    monkeypatch.setattr(mod, "compute_changed_files", boom)
    # Should NOT raise.
    got = _write_scoped_tests_artifact(
        tmp_path / "missing-rig", tmp_path / "run", force_full=False,
    )
    assert got == []
