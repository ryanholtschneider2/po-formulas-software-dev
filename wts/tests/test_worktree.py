"""Unit tests for po_formulas_wts.worktree.

Each test sets up a throwaway git repo in tmp_path, exercises one
operation (setup/merge/cleanup/sanitize/exclude), and asserts state.
No external services; no Claude/codex calls.

Run from the wts pack dir:
    uv run python -m pytest tests/test_worktree.py -q
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from po_formulas_wts.worktree import (
    WorktreePaths,
    _add_exclude_rules,
    _is_git_repo,
    _worktree_exclude_file,
    cleanup_worktree,
    commit_pending,
    merge_worktree,
    push_worktree_branch,
    sanitize,
    setup_worktree,
)


# ─── helpers ───


def _git(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {args} failed: {proc.stderr}")
    return proc


@pytest.fixture
def rig(tmp_path: Path) -> Path:
    """A throwaway git rig with one tracked file + shared .beads/.planning dirs."""
    repo = tmp_path / "rig"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "a.txt").write_text("hello\n")
    _git("add", "a.txt", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    (repo / ".beads").mkdir()
    (repo / ".beads" / "marker").write_text("beads-data\n")
    (repo / ".planning").mkdir()
    (repo / ".planning" / "marker").write_text("planning-data\n")
    return repo


# ─── sanitize ───


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("simple", "simple"),
        ("x.1.2", "x_1_2"),
        ("with-dash", "with-dash"),
        ("with_underscore", "with_underscore"),
        ("nano/cor.ps-3qf", "nano_cor_ps-3qf"),
        ("UPPER123", "UPPER123"),
        ("emoji😀", "emoji_"),
    ],
)
def test_sanitize_replaces_unsafe_chars(raw: str, expected: str):
    assert sanitize(raw) == expected


# ─── WorktreePaths ───


def test_worktree_paths_for_issue(tmp_path: Path):
    paths = WorktreePaths.for_issue(tmp_path / "myrig", "x.1.2")
    assert paths.main_rig == (tmp_path / "myrig").resolve()
    assert paths.worktree == (tmp_path / "myrig" / ".worktrees" / "wts-x_1_2").resolve()
    assert paths.worktree.name == "wts-x_1_2"
    assert paths.worktree.parent.name == ".worktrees"
    assert paths.branch == "wts-x_1_2"


def test_worktree_paths_for_epic_reuses_issue_naming(tmp_path: Path):
    issue_paths = WorktreePaths.for_issue(tmp_path / "myrig", "epic.1")
    epic_paths = WorktreePaths.for_epic(tmp_path / "myrig", "epic.1")
    assert epic_paths == issue_paths


# ─── push_worktree_branch (PR Sheriff handoff) ───


def test_push_worktree_branch_no_remote_commits_local(rig: Path):
    """No `origin` (local-only rig): commits pending work, leaves the branch
    local for the Sheriff, reports pushed=False."""
    setup_worktree(rig, "demo", shared_dirs=(".beads",))
    wt = rig / ".worktrees" / "wts-demo"
    (wt / "feature.txt").write_text("new work\n")
    out = push_worktree_branch(rig, "demo")
    assert out == {"branch": "wts-demo", "pushed": False, "remote": False}
    # the pending change was committed on the branch
    log = _git("log", "--oneline", cwd=wt).stdout
    assert "pre-handoff snapshot" in log


def test_push_worktree_branch_missing_worktree(rig: Path):
    out = push_worktree_branch(rig, "never-made")
    assert out["pushed"] is False
    assert out["branch"] == "wts-never-made"


def test_push_worktree_branch_with_remote(rig: Path, tmp_path: Path):
    """With an `origin` bare remote, the branch is pushed."""
    bare = tmp_path / "origin.git"
    _git("init", "-q", "--bare", str(bare), cwd=tmp_path)
    _git("remote", "add", "origin", str(bare), cwd=rig)
    setup_worktree(rig, "demo", shared_dirs=(".beads",))
    wt = rig / ".worktrees" / "wts-demo"
    (wt / "feature.txt").write_text("new work\n")
    out = push_worktree_branch(rig, "demo")
    assert out == {"branch": "wts-demo", "pushed": True, "remote": True}
    refs = _git("ls-remote", "--heads", str(bare), cwd=rig).stdout
    assert "wts-demo" in refs


# ─── _is_git_repo ───


def test_is_git_repo_true_for_git_init(rig: Path):
    assert _is_git_repo(rig) is True


def test_is_git_repo_false_for_plain_dir(tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert _is_git_repo(plain) is False


# ─── _add_exclude_rules + _worktree_exclude_file ───


def test_add_exclude_rules_writes_to_worktree_info_excludes(rig: Path):
    """Per-worktree exclude file lands at .git/worktrees/<name>/info/exclude
    after the worktree exists; falls back to .git/info/exclude when no
    per-worktree dir is yet present."""
    setup_worktree(rig, "demo")  # creates the per-worktree dir
    excl = _worktree_exclude_file(rig, "wts-demo")
    assert excl.exists(), f"expected exclude file at {excl}"
    body = excl.read_text()
    assert ".beads" in body
    assert ".planning" in body


def test_add_exclude_rules_idempotent(rig: Path):
    setup_worktree(rig, "demo")
    excl = _worktree_exclude_file(rig, "wts-demo")
    body_first = excl.read_text()
    _add_exclude_rules(rig, "wts-demo", [".beads", ".planning"])
    body_second = excl.read_text()
    assert body_first == body_second, "rerunning must not duplicate rules"


# ─── setup_worktree ───


def test_setup_worktree_creates_branch_and_symlinks(rig: Path):
    wt = setup_worktree(rig, "demo")
    paths = WorktreePaths.for_issue(rig, "demo")

    # Worktree dir exists at predicted path
    assert wt == paths.worktree
    assert wt.is_dir()

    # Branch created
    proc = _git("branch", "--list", "wts-demo", cwd=rig, check=False)
    assert "wts-demo" in proc.stdout

    # Tracked files copied into worktree
    assert (wt / "a.txt").read_text() == "hello\n"

    # Shared dirs symlinked back to main rig
    assert (wt / ".beads").is_symlink()
    assert (wt / ".beads").resolve() == (rig / ".beads").resolve()
    assert (wt / ".planning").is_symlink()
    assert (wt / ".planning").resolve() == (rig / ".planning").resolve()


def test_setup_worktree_idempotent(rig: Path):
    wt1 = setup_worktree(rig, "demo")
    wt2 = setup_worktree(rig, "demo")
    assert wt1 == wt2
    assert wt1.is_dir()


def test_setup_worktree_creates_shared_dirs_when_missing(tmp_path: Path):
    """Missing .beads/.planning in main rig are created + symlinked (not skipped)."""
    repo = tmp_path / "rig"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "a.txt").write_text("hi\n")
    _git("add", "a.txt", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    # No .beads or .planning in main — they should be auto-created and symlinked
    wt = setup_worktree(repo, "no-shared")
    assert wt.is_dir()
    assert (repo / ".beads").is_dir()
    assert (wt / ".beads").is_symlink()
    assert (wt / ".beads").resolve() == (repo / ".beads").resolve()
    assert (repo / ".planning").is_dir()
    assert (wt / ".planning").is_symlink()
    assert (wt / ".planning").resolve() == (repo / ".planning").resolve()


def _repo_with_tracked_beads(tmp_path: Path) -> Path:
    """A git rig that has COMMITTED its `.beads/` (the gastown / dolt-server
    rig shape). `git worktree add` materializes a real `.beads/` in the wt."""
    repo = tmp_path / "rig"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "a.txt").write_text("hello\n")
    (repo / ".beads").mkdir()
    (repo / ".beads" / "metadata.json").write_text('{"dolt_mode": "server"}')
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "init with .beads", cwd=repo)
    return repo


def test_setup_worktree_tracked_beads_writes_redirect(tmp_path: Path):
    """Tracked `.beads/` → no refusal; bd `redirect` file points back to main."""
    repo = _repo_with_tracked_beads(tmp_path)
    wt = setup_worktree(repo, "demo", shared_dirs=(".beads",))

    redirect = wt / ".beads" / "redirect"
    assert redirect.is_file(), "redirect file should be written for tracked .beads"
    # Resolves (from the worktree) to the main rig's .beads.
    target = (wt / redirect.read_text().strip()).resolve()
    assert target == (repo / ".beads").resolve()
    # Checked-out metadata.json is left intact alongside the redirect.
    assert (wt / ".beads" / "metadata.json").is_file()


def test_setup_epic_worktree_tracked_beads_writes_redirect(tmp_path: Path):
    repo = _repo_with_tracked_beads(tmp_path)
    wt = setup_worktree(repo, "epic.1", shared_dirs=(".beads",), for_epic=True)

    redirect = wt / ".beads" / "redirect"
    assert redirect.is_file()
    assert (wt / redirect.read_text().strip()).resolve() == (repo / ".beads").resolve()


def test_setup_worktree_tracked_planning_symlinks_to_main(tmp_path: Path):
    """Tracked `.planning/` → symlinked to main (skip-worktree) so the run_dir
    is identical from the flow's and the agents' POV; a new run-dir created in
    the worktree shows up in main, and the swap is not staged for merge-back."""
    repo = _repo_with_tracked_beads(tmp_path)
    (repo / ".planning").mkdir()
    (repo / ".planning" / "old.txt").write_text("prior artifact\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "track .planning", cwd=repo)

    wt = setup_worktree(repo, "demo2", shared_dirs=(".beads", ".planning"))
    planning = wt / ".planning"
    assert planning.is_symlink()
    assert planning.resolve() == (repo / ".planning").resolve()

    # An agent writing a run-dir in the worktree lands in main (seamless).
    (planning / "software-dev-full" / "demo2").mkdir(parents=True)
    (planning / "software-dev-full" / "demo2" / "CONTEXT.md").write_text("ctx\n")
    assert (repo / ".planning" / "software-dev-full" / "demo2" / "CONTEXT.md").is_file()

    # The symlink swap must not show up as a staged/merge-back change.
    status = _git("status", "--porcelain", cwd=wt).stdout
    assert ".planning" not in status, f"unexpected .planning churn: {status!r}"


def test_setup_worktree_rejects_non_git_dir(tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(RuntimeError, match="not a git repo"):
        setup_worktree(plain, "demo")


# ─── commit_pending ───


def test_commit_pending_returns_true_when_dirty(rig: Path):
    wt = setup_worktree(rig, "demo")
    (wt / "b.txt").write_text("new file in wt\n")
    assert commit_pending(rig, "demo") is True
    # And clean now → returns False on second call
    assert commit_pending(rig, "demo") is False


def test_commit_pending_returns_false_for_clean_tree(rig: Path):
    setup_worktree(rig, "demo")
    assert commit_pending(rig, "demo") is False


def test_commit_pending_returns_false_for_missing_worktree(rig: Path):
    assert commit_pending(rig, "never-setup") is False


# ─── merge_worktree (the canonical round-trip) ───


def test_merge_worktree_round_trip(rig: Path):
    """setup → add file in wt → merge → main has the file → wt is gone."""
    wt = setup_worktree(rig, "demo")
    (wt / "added.txt").write_text("added in worktree\n")
    merged_into = merge_worktree(rig, "demo", cleanup=True)

    assert merged_into == "main"
    assert (rig / "added.txt").read_text() == "added in worktree\n"
    assert not wt.exists(), "worktree should be removed after cleanup"

    # Main rig's HEAD now has the wt commit ahead of init
    log = _git("log", "--oneline", cwd=rig).stdout.strip().splitlines()
    assert len(log) >= 2, f"expected merge to land at least one new commit; log: {log}"


def test_merge_epic_worktree_round_trip(rig: Path):
    wt = setup_worktree(rig, "epic.1", for_epic=True)
    (wt / "epic-added.txt").write_text("added in epic worktree\n")
    merged_into = merge_worktree(rig, "epic.1", cleanup=True, for_epic=True)

    assert merged_into == "main"
    assert (rig / "epic-added.txt").read_text() == "added in epic worktree\n"
    assert not wt.exists()


def test_merge_worktree_auto_detects_target_branch(tmp_path: Path):
    """Default branch may be master, main, or anything else — merge_worktree
    should auto-detect."""
    repo = tmp_path / "master-rig"
    repo.mkdir()
    _git("init", "-q", "-b", "master", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "a.txt").write_text("hi\n")
    _git("add", "a.txt", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)

    wt = setup_worktree(repo, "demo")
    (wt / "b.txt").write_text("in wt\n")
    merged_into = merge_worktree(repo, "demo", cleanup=True)
    assert merged_into == "master"
    assert (repo / "b.txt").read_text() == "in wt\n"


def test_merge_worktree_keeps_worktree_when_cleanup_false(rig: Path):
    wt = setup_worktree(rig, "demo")
    (wt / "b.txt").write_text("in wt\n")
    merge_worktree(rig, "demo", cleanup=False)
    assert wt.exists(), "worktree should survive when cleanup=False"


def test_merge_worktree_idempotent_after_cleanup(rig: Path):
    """Calling merge_worktree against an already-cleaned-up worktree
    should warn and return — not raise."""
    wt = setup_worktree(rig, "demo")
    (wt / "b.txt").write_text("in wt\n")
    merge_worktree(rig, "demo", cleanup=True)
    # Second call: no-op (worktree missing)
    merge_worktree(rig, "demo", cleanup=True)


# ─── cleanup_worktree ───


def test_cleanup_worktree_removes_directory(rig: Path):
    wt = setup_worktree(rig, "demo")
    cleanup_worktree(rig, "demo", force=True)
    assert not wt.exists()


def test_cleanup_worktree_with_delete_branch(rig: Path):
    setup_worktree(rig, "demo")
    proc_before = _git("branch", "--list", "wts-demo", cwd=rig)
    assert "wts-demo" in proc_before.stdout

    # First remove the worktree (branch still attached), then drop the branch
    cleanup_worktree(rig, "demo", force=True, delete_branch=True)
    proc_after = _git("branch", "--list", "wts-demo", cwd=rig)
    assert "wts-demo" not in proc_after.stdout


def test_cleanup_worktree_silent_when_already_gone(rig: Path):
    # No setup called — cleanup_worktree should not raise.
    cleanup_worktree(rig, "never-setup", force=True)


# ─── shared dirs aren't staged by `git add -A` (the bug the exclude rules fix) ───


def test_shared_dir_symlinks_are_not_staged_in_worktree(rig: Path):
    """The bug from the first /tmp smoke test — symlinks for .beads/.planning
    must be locally-ignored so `git add -A` doesn't pick them up, otherwise
    merge into main fails with 'would lose untracked files'."""
    wt = setup_worktree(rig, "demo")
    (wt / "real-change.txt").write_text("yes\n")
    _git("add", "-A", cwd=wt)
    proc_status = _git("status", "--porcelain", cwd=wt)
    # Only real-change.txt should appear staged.
    staged = [line for line in proc_status.stdout.splitlines() if line.strip()]
    assert any("real-change.txt" in line for line in staged), staged
    for line in staged:
        assert ".beads" not in line, f"shared dir leaked into stage: {line}"
        assert ".planning" not in line, f"shared dir leaked into stage: {line}"
