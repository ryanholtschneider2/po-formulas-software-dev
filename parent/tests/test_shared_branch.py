"""Unit tests for `po_formulas.shared_branch` — the shared-integration-branch
transport for agentic-epic (po-formulas-software-dev-18m).

All git/gh shell-outs are monkeypatched: these tests assert the *commands* the
module issues (branch creation off the right base, idempotent reuse, draft-PR
open + graceful no-op, mark-ready, integrate merge + conflict abort, lock
serialization) without touching a real repo. A separate real-git round-trip
(run manually / in e2e) proves the plumbing end-to-end.
"""

from __future__ import annotations

import subprocess

import pytest

from po_formulas import shared_branch as sb


# ── identifiers ──────────────────────────────────────────────────────────────


def test_branch_names():
    assert sb.epic_branch_name("rig-epic1") == "epic/rig-epic1"
    # dots in child ids are sanitized so the branch name is a valid refname.
    assert sb.child_branch_name("rig-epic1.2") == "agentic-rig-epic1_2"


# ── a fake `git`/`gh` recorder ───────────────────────────────────────────────


class FakeRun:
    """Records subprocess.run calls and returns scripted results.

    `script` maps a substring-of-the-joined-argv → (returncode, stdout, stderr).
    First matching key wins; unmatched calls default to rc=0, empty output.
    """

    def __init__(self, script: dict[str, tuple[int, str, str]] | None = None):
        self.calls: list[list[str]] = []
        self.script = script or {}

    def __call__(self, argv, *a, **k):
        self.calls.append(list(argv))
        joined = " ".join(argv)
        for needle, (rc, out, err) in self.script.items():
            if needle in joined:
                return subprocess.CompletedProcess(argv, rc, out, err)
        return subprocess.CompletedProcess(argv, 0, "", "")

    def ran(self, *needles: str) -> bool:
        """True iff some recorded call contains every needle (in one argv)."""
        return any(all(n in " ".join(c) for n in needles) for c in self.calls)


@pytest.fixture
def gh_present(monkeypatch):
    monkeypatch.setattr(sb, "_gh_available", lambda: True)


# ── create_integration_branch ────────────────────────────────────────────────


def test_create_integration_branch_off_remote_base(monkeypatch, tmp_path):
    fake = FakeRun(
        {
            "remote": (0, "origin\n", ""),  # has a remote
            "rev-parse --verify --quiet refs/heads/epic/": (1, "", ""),  # branch absent
            "rev-parse --verify --quiet refs/remotes/origin/main": (0, "abc\n", ""),
        }
    )
    monkeypatch.setattr(subprocess, "run", fake)

    info = sb.create_integration_branch(tmp_path, "rig-e1", base_branch="main")

    assert info == {
        "branch": "epic/rig-e1",
        "created": True,
        "pushed": True,
        "remote": True,
    }
    # Cut off the fetched remote tip, then pushed (no PR yet — opened at finalize).
    assert fake.ran("branch", "epic/rig-e1", "origin/main")
    assert fake.ran("push", "-u", "origin", "epic/rig-e1")


def test_create_integration_branch_idempotent_reuse(monkeypatch, tmp_path):
    fake = FakeRun(
        {
            "remote": (0, "origin\n", ""),
            "rev-parse --verify --quiet refs/heads/epic/": (0, "abc\n", ""),  # exists
        }
    )
    monkeypatch.setattr(subprocess, "run", fake)

    info = sb.create_integration_branch(tmp_path, "rig-e1")
    assert info["created"] is False
    # Reuse must NOT re-create the branch.
    assert not fake.ran("branch", "epic/rig-e1")


def test_create_integration_branch_local_only(monkeypatch, tmp_path):
    fake = FakeRun(
        {
            "remote": (0, "", ""),  # no remote
            "rev-parse --verify --quiet refs/heads/epic/": (1, "", ""),
        }
    )
    monkeypatch.setattr(subprocess, "run", fake)

    info = sb.create_integration_branch(tmp_path, "rig-e1")
    assert info == {
        "branch": "epic/rig-e1",
        "created": True,
        "pushed": False,
        "remote": False,
    }
    # No remote → branch cut off the local base, never pushed.
    assert fake.ran("branch", "epic/rig-e1", "main")
    assert not fake.ran("push")


# ── open_draft_pr / mark_pr_ready ────────────────────────────────────────────


def test_open_draft_pr_opens(monkeypatch, tmp_path, gh_present):
    fake = FakeRun(
        {
            "remote": (0, "origin\n", ""),
            "pr view": (1, "", "no pr"),  # none exists yet
            "pr create": (0, "https://github.com/x/y/pull/7\n", ""),
        }
    )
    monkeypatch.setattr(subprocess, "run", fake)

    info = sb.open_draft_pr(
        tmp_path, branch="epic/e1", base_branch="main", title="t", body="b"
    )
    assert info["opened"] is True
    assert info["url"] == "https://github.com/x/y/pull/7"
    assert fake.ran("pr", "create", "--draft", "--base", "main", "--head", "epic/e1")


def test_open_draft_pr_ready_omits_draft_flag(monkeypatch, tmp_path, gh_present):
    """Finalize opens the epic PR ready-for-review (draft=False): no --draft."""
    fake = FakeRun(
        {
            "remote": (0, "origin\n", ""),
            "pr view": (1, "", "no pr"),
            "pr create": (0, "https://github.com/x/y/pull/9\n", ""),
        }
    )
    monkeypatch.setattr(subprocess, "run", fake)

    info = sb.open_draft_pr(
        tmp_path, branch="epic/e1", base_branch="main", title="t", body="b", draft=False
    )
    assert info["opened"] is True
    assert fake.ran("pr", "create", "--base", "main", "--head", "epic/e1")
    assert not fake.ran("pr", "create", "--draft")


def test_commits_ahead_counts_and_is_error_safe(monkeypatch, tmp_path):
    fake = FakeRun({"rev-list --count main..epic/e1": (0, "3\n", "")})
    monkeypatch.setattr(subprocess, "run", fake)
    assert sb.commits_ahead(tmp_path, "main", "epic/e1") == 3

    # Non-numeric / git error → 0 (treat "can't tell" as "nothing to PR").
    bad = FakeRun({"rev-list": (128, "", "fatal: bad revision")})
    monkeypatch.setattr(subprocess, "run", bad)
    assert sb.commits_ahead(tmp_path, "main", "epic/e1") == 0


def test_open_draft_pr_idempotent_when_pr_exists(monkeypatch, tmp_path, gh_present):
    fake = FakeRun(
        {
            "remote": (0, "origin\n", ""),
            "pr view": (0, "https://github.com/x/y/pull/3\n", ""),  # already open
        }
    )
    monkeypatch.setattr(subprocess, "run", fake)
    info = sb.open_draft_pr(
        tmp_path, branch="epic/e1", base_branch="main", title="t", body="b"
    )
    assert info["opened"] is False
    assert info["url"] == "https://github.com/x/y/pull/3"
    assert not fake.ran("pr", "create")


def test_open_draft_pr_no_remote_is_graceful(monkeypatch, tmp_path, gh_present):
    fake = FakeRun({"remote": (0, "", "")})
    monkeypatch.setattr(subprocess, "run", fake)
    info = sb.open_draft_pr(
        tmp_path, branch="epic/e1", base_branch="main", title="t", body="b"
    )
    assert info["opened"] is False
    assert "no remote" in info["reason"]


def test_mark_pr_ready(monkeypatch, tmp_path, gh_present):
    fake = FakeRun({"remote": (0, "origin\n", ""), "pr ready": (0, "", "")})
    monkeypatch.setattr(subprocess, "run", fake)
    info = sb.mark_pr_ready(tmp_path, branch="epic/e1")
    assert info["ready"] is True
    assert fake.ran("pr", "ready", "epic/e1")


# ── integrate_child ──────────────────────────────────────────────────────────


def test_integrate_child_merges_and_pushes(monkeypatch, tmp_path):
    fake = FakeRun(
        {
            "rev-parse --verify --quiet refs/heads/agentic-": (
                0,
                "abc\n",
                "",
            ),  # child branch exists
            "remote": (0, "origin\n", ""),
            "merge --no-edit": (0, "", ""),
        }
    )
    monkeypatch.setattr(subprocess, "run", fake)

    info = sb.integrate_child(
        tmp_path, "rig-e1", "rig-e1.2", integration_worktree=tmp_path / "wt"
    )
    assert info["merged"] is True
    assert info["conflict"] is False
    assert info["child_branch"] == "agentic-rig-e1_2"
    assert fake.ran("merge", "--no-edit", "agentic-rig-e1_2")
    assert fake.ran("push", "origin", "epic/rig-e1")


def test_integrate_child_conflict_aborts(monkeypatch, tmp_path):
    fake = FakeRun(
        {
            "rev-parse --verify --quiet refs/heads/agentic-": (0, "abc\n", ""),
            "remote": (0, "origin\n", ""),
            "merge --no-edit": (1, "CONFLICT in foo.py", ""),
        }
    )
    monkeypatch.setattr(subprocess, "run", fake)

    info = sb.integrate_child(
        tmp_path, "rig-e1", "rig-e1.2", integration_worktree=tmp_path / "wt"
    )
    assert info["merged"] is False
    assert info["conflict"] is True
    # A conflict must leave the epic branch clean.
    assert fake.ran("merge", "--abort")
    # And never push a broken merge.
    assert not fake.ran("push", "origin", "epic/rig-e1")


def test_integrate_child_conflict_resolved_by_callback(monkeypatch, tmp_path):
    """on_conflict resolver commits the merge → child is KEPT, not dropped."""
    fake = FakeRun(
        {
            "rev-parse --verify --quiet refs/heads/agentic-": (0, "abc\n", ""),
            "remote": (0, "origin\n", ""),
            "merge --no-edit": (1, "CONFLICT in IdeaCard.tsx", ""),
            "diff --name-only --diff-filter=U": (0, "", ""),  # post-resolve: none unmerged
            "rev-parse -q --verify MERGE_HEAD": (1, "", ""),  # committed → no MERGE_HEAD
        }
    )
    monkeypatch.setattr(subprocess, "run", fake)

    called: dict = {}

    def resolver(wt, files):
        called["wt"] = wt
        return True  # pretend the agent resolved + committed

    info = sb.integrate_child(
        tmp_path, "rig-e1", "rig-e1.2",
        integration_worktree=tmp_path / "wt", on_conflict=resolver,
    )
    assert info["merged"] is True
    assert info["resolved"] is True
    assert info["conflict"] is False
    assert called["wt"] == tmp_path / "wt"
    assert not fake.ran("merge", "--abort")          # NOT dropped
    assert fake.ran("push", "origin", "epic/rig-e1")  # the resolved merge is pushed


def test_integrate_child_conflict_callback_fails_aborts(monkeypatch, tmp_path):
    """on_conflict that can't resolve → abort, child dropped, epic branch clean."""
    fake = FakeRun(
        {
            "rev-parse --verify --quiet refs/heads/agentic-": (0, "abc\n", ""),
            "remote": (0, "origin\n", ""),
            "merge --no-edit": (1, "CONFLICT in IdeaCard.tsx", ""),
            "diff --name-only --diff-filter=U": (0, "IdeaCard.tsx\n", ""),
        }
    )
    monkeypatch.setattr(subprocess, "run", fake)

    info = sb.integrate_child(
        tmp_path, "rig-e1", "rig-e1.2",
        integration_worktree=tmp_path / "wt",
        on_conflict=lambda wt, files: False,
    )
    assert info["merged"] is False
    assert info["conflict"] is True
    assert fake.ran("merge", "--abort")
    assert not fake.ran("push", "origin", "epic/rig-e1")


def test_integrate_child_missing_branch(monkeypatch, tmp_path):
    fake = FakeRun({"rev-parse --verify --quiet refs/heads/agentic-": (1, "", "")})
    monkeypatch.setattr(subprocess, "run", fake)
    info = sb.integrate_child(
        tmp_path, "rig-e1", "rig-e1.2", integration_worktree=tmp_path / "wt"
    )
    assert info["merged"] is False
    assert info["conflict"] is False
    assert "not found" in info["reason"]
    assert not fake.ran("merge")


def test_integration_lock_serializes(monkeypatch, tmp_path):
    """The lock is a real flock — acquiring it twice in the same process is fine
    (re-entrant via separate fds is not, but sequential acquire/release is)."""
    with sb._integration_lock(tmp_path, "rig-e1"):
        lock = tmp_path / ".worktrees" / ".integrate-rig-e1.lock"
        assert lock.exists()
    # Re-acquire after release works (no deadlock).
    with sb._integration_lock(tmp_path, "rig-e1"):
        pass


# ── branch_directive ─────────────────────────────────────────────────────────


def test_branch_directive_overrides_and_names_branch():
    text = sb.branch_directive("epic/rig-e1", "rig-e1.2")
    assert "OVERRIDES" in text
    assert "epic/rig-e1" in text
    assert "agentic-rig-e1_2" in text
    # Must forbid the child from opening its own PR / merging to main…
    assert "gh pr create" in text and "NEVER" in text
    assert "main" in text
    # …and diff against the epic branch (not main) so the critic sees only this
    # child's changes, not prior children's integrated work.
    assert "epic/rig-e1...HEAD" in text
