"""Content tests for the software-dev-agentic role prompts (po-formulas-software-dev-175).

The agentic flow is driven entirely by its two role prompts: the actor
prompt tells the single implementer to work in a worktree off main, run
the repo's own tests, and open a PR (right-sizing its rigor to the ask),
and the critic prompt verifies *goal accomplishment* and returns a
concrete fix list on fail. These assertions pin that intent so a future
prompt edit can't silently drop it.

Mirrors the prompt-content style of the wts pack's ``test_pack_skeleton``.
"""

from __future__ import annotations

import pytest

import po_formulas.agentic as ag

_AGENTS = ag._AGENTS_DIR
_REPO_ROOT = _AGENTS.parents[2]


def _read(rel: str) -> str:
    return (_AGENTS / rel).read_text()


# ─────────────────────── worker (actor) prompt ──────────────────────


def test_worker_prompt_drives_worktree_tests_and_pr() -> None:
    text = _read("agentic-worker/prompt.md").lower()
    # The core of the prompt-driven flow: worktree off main, run tests, PR.
    assert "worktree" in text
    assert "main" in text
    assert "pr" in text or "pull request" in text
    assert "test" in text
    # The actor must NOT merge to main — the PR is the deliverable.
    assert "never merge" in text or "do not merge" in text


def test_worker_prompt_has_rigor_scaling_and_pr_checklist() -> None:
    text = _read("agentic-worker/prompt.md").lower()
    assert "rigor scales to the ask" in text
    assert "small ask" in text
    assert ("large / pr-level ask" in text) or ("pr-level ask" in text)
    assert "pr-level workflow checklist" in text
    for needle in ("plan", "test", "doc", "commit"):
        assert needle in text, f"PR checklist missing mention of {needle!r}"
    # error paths are part of a real feature's tests, not just happy path.
    assert "error path" in text


def test_worker_prompt_names_design_and_goal_critics() -> None:
    text = _read("agentic-worker/prompt.md").lower()
    # The actor must understand the design-review critic runs before the
    # goal-accomplishment critic, and that there is no mechanical checker.
    assert "critic" in text
    assert "design-review critic" in text
    assert "component-system discipline" in text or "component-system" in text
    assert "goal accomplishment" in text or "accomplish the goal" in text
    raw = _read("agentic-worker/prompt.md")
    assert "{{role_step_bead_id}}" in raw
    assert "{{role_step_close_block}}" in raw


def test_worker_task_signals_chosen_mode_and_pr() -> None:
    text = _read("agentic-worker/task.md").lower()
    assert "right-size your process to the ask" in text
    assert "mode" in text
    assert "worktree" in text
    assert "pull request" in text or "open a pr" in text
    # Template vars stay intact (the flow renders these).
    raw = _read("agentic-worker/task.md")
    assert "{{seed_id}}" in raw and "{{iter}}" in raw
    assert "{{revision_note}}" in raw


def test_worker_prompts_component_discipline_evidence() -> None:
    combined = (
        _read("agentic-worker/prompt.md") + "\n" + _read("agentic-worker/task.md")
    ).lower()
    assert "design-system" in combined
    assert "component-discipline" in combined
    assert "quality check" in combined
    assert "ds-override" in combined


# ─────────────────────── critic (reviewer) prompt ───────────────────


def test_critic_verifies_goal_accomplishment() -> None:
    for rel in ("agentic-reviewer/prompt.md", "agentic-reviewer/task.md"):
        text = _read(rel).lower()
        assert "goal accomplishment" in text, f"{rel} missing goal-accomplishment"


def test_critic_returns_pass_fail_and_fix_list() -> None:
    for rel in ("agentic-reviewer/prompt.md", "agentic-reviewer/task.md"):
        text = _read(rel).lower()
        assert "pass" in text and "fail" in text, f"{rel} missing pass/fail verdict"
        # On fail the critic must write a concrete fix list the flow feeds back.
        assert "fix list" in text, f"{rel} missing the fix-list instruction"
        assert "critique-iter-" in _read(rel), f"{rel} missing critique artifact path"


def test_critic_does_not_merge_and_scales_to_ask() -> None:
    text = _read("agentic-reviewer/prompt.md").lower()
    assert "size of the ask" in text or "right-sized" in text
    assert "do not merge" in text or "not merge" in text


def test_design_review_critic_prompt_contract() -> None:
    for rel in ("agentic-design-review/prompt.md", "agentic-design-review/task.md"):
        text = _read(rel).lower()
        assert "design-system" in text
        assert "component-discipline" in text
        assert "pass" in text and "fail" in text
        assert "design-critique-iter-" in text
        assert "evidence" in text
        assert "component contract" in text
        assert "docs reference" in text
        assert "remediation" in text


# ─────────────────────── slash command (optional entry point) ────────


def test_agentic_slash_command_present() -> None:
    cmd = _REPO_ROOT / ".claude" / "commands" / "agentic.md"
    if not cmd.is_file():
        pytest.skip("slash command not materialized in this layout")
    body = cmd.read_text()
    assert "software-dev-agentic" in body
    assert "$ARGUMENTS" in body
    assert "po run" in body
