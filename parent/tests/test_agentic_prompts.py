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
    # The PR-level checklist was enriched into the phase-by-phase "full workflow".
    assert "the full workflow" in text
    for needle in ("plan", "test", "doc", "commit"):
        assert needle in text, f"PR checklist missing mention of {needle!r}"
    # error paths are part of a real feature's tests, not just happy path.
    assert "error path" in text


# The anti-mock checklist is the single highest-value ported block — it must
# appear verbatim in BOTH the worker (as a build rule) and the critic (as a
# BLOCKING gate). Pin its presence + a couple distinctive lines so a future
# edit can't silently drop or half-port it.
_ANTI_MOCK_ANCHOR = (
    "Anti-Mock checklist — any violation is a BLOCKING finding, fix before approval."
)


def test_anti_mock_checklist_verbatim_in_both_roles() -> None:
    for rel in ("agentic-worker/prompt.md", "agentic-reviewer/prompt.md"):
        raw = _read(rel)
        assert _ANTI_MOCK_ANCHOR in raw, (
            f"{rel} missing the verbatim anti-mock checklist"
        )
        assert "ship the mock and fix it later" in raw, (
            f"{rel} anti-mock block truncated"
        )
        assert "mock the DB/API/service they integrate with" in raw, (
            f"{rel} anti-mock test-code section missing"
        )


def test_worker_prompt_encodes_phase_howto() -> None:
    # Each workflow phase must encode HOW, not just the name.
    text = _read("agentic-worker/prompt.md").lower()
    for phase in (
        "explore",
        "research",
        "plan",
        "implement",
        "baseline",
        "regression",
        "lint",
        "close the loop",
        "docs",
        "learn",
    ):
        assert phase in text, f"worker prompt missing the {phase!r} phase how-to"
    # the mandatory verification-strategy table ported from the planner discipline
    assert "verification strategy" in text


def test_worker_prompt_has_subagent_fanout_playbook() -> None:
    text = _read("agentic-worker/prompt.md").lower()
    assert "fan-out" in text
    # the concrete passes, not the old vague one-liner
    assert "explorer" in text
    assert "lint-worker" in text
    assert "code-review" in text or "code reviewer" in text


def test_worker_prompt_explicit_research_and_decision_log() -> None:
    text = _read("agentic-worker/prompt.md").lower()
    assert "context7" in text or "web search" in text  # library/best-practice research
    assert "decision-log" in text  # decision-log discipline ported from the builder


def test_worker_prompt_critic_is_the_only_gate() -> None:
    text = _read("agentic-worker/prompt.md").lower()
    # The actor must understand the critic verifies goal accomplishment and
    # is the only gate — there is no separate mechanical checker anymore.
    assert "critic" in text
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
    assert "size of the ask" in text or "right-sized" in text or "true size" in text
    assert "do not merge" in text or "not merge" in text


def test_critic_prompt_has_full_rubric_and_severities() -> None:
    raw = _read("agentic-reviewer/prompt.md")
    text = raw.lower()
    # severity scheme ported from the code-reviewer rubric
    for sev in ("CRITICAL", "IMPORTANT", "MINOR"):
        assert sev in raw, f"critic prompt missing the {sev} severity"
    # the rubric dimensions a single goal-critic now owns end to end
    for dim in ("security", "anti-mock", "performance", "decision-log", "edge case"):
        assert dim in text, f"critic prompt missing rubric dimension {dim!r}"
    # it challenges the worker's self-declared size rather than rubber-stamping it
    assert "self-declared size" in text or "challenge the actor" in text
    # it verifies the tests ACTUALLY ran, not merely that they exist
    assert "actually ran" in text or "tests actually" in text
    # a per-AC evidence table, not a vibe check
    assert "met / unmet" in text or "met/unmet" in text


# ─────────────────────── slash command (optional entry point) ────────


def test_agentic_slash_command_present() -> None:
    cmd = _REPO_ROOT / ".claude" / "commands" / "agentic.md"
    if not cmd.is_file():
        pytest.skip("slash command not materialized in this layout")
    body = cmd.read_text()
    assert "software-dev-agentic" in body
    assert "$ARGUMENTS" in body
    assert "po run" in body
