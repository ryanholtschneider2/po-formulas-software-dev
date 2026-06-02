"""Content tests for the software-dev-agentic role prompts (po-formulas-software-dev-716).

The agentic flow is driven entirely by its role prompts: the worker prompt
tells the single implementer to right-size its rigor and run the full
PR-level workflow on real features, and the reviewer prompt must judge
step-adherence *scaled to the size of the ask* while leaving the mechanical
facts (tree clean, work landed, no mocked prod code, lint/tests/regression)
to the machine. These assertions pin that intent so a future prompt edit
can't silently drop it.

Mirrors the prompt-content style of the wts pack's ``test_pack_skeleton``.
"""

from __future__ import annotations

import pytest

import po_formulas.agentic as ag

_AGENTS = ag._AGENTS_DIR
_REPO_ROOT = _AGENTS.parents[2]


def _read(rel: str) -> str:
    return (_AGENTS / rel).read_text()


# ─────────────────────── worker prompt ──────────────────────────────


def test_worker_prompt_has_rigor_scaling_and_pr_checklist() -> None:
    text = _read("agentic-worker/prompt.md").lower()
    # Rigor-scales-to-the-ask is explicit (issue point 2).
    assert "rigor scales to the ask" in text
    assert "small ask" in text
    assert ("large / pr-level ask" in text) or ("pr-level ask" in text)
    # PR-level checklist is distilled in (issue point 1): the load-bearing
    # steps a real PR demands.
    assert "pr-level workflow checklist" in text
    for needle in ("plan", "test", "doc", "commit"):
        assert needle in text, f"PR checklist missing mention of {needle!r}"
    # error paths are part of a real feature's tests, not just happy path.
    assert "error path" in text


def test_worker_prompt_keeps_mechanical_layer_machine_owned() -> None:
    text = _read("agentic-worker/prompt.md").lower()
    # The worker must understand the machine owns facts and the reviewer
    # owns judgment (issue point 3) — the two are not folded together.
    assert "machine" in text
    assert "reviewer" in text
    assert "{{role_step_bead_id}}" in _read("agentic-worker/prompt.md")
    assert "{{role_step_close_block}}" in _read("agentic-worker/prompt.md")


def test_worker_task_signals_chosen_mode() -> None:
    text = _read("agentic-worker/task.md").lower()
    assert "right-size your process to the ask" in text
    # The worker is asked to declare which mode it picked so the reviewer
    # can judge step-adherence against the right bar.
    assert "mode" in text
    # Template vars stay intact (the flow renders these).
    raw = _read("agentic-worker/task.md")
    assert "{{seed_id}}" in raw and "{{iter}}" in raw


# ─────────────────────── reviewer prompt ────────────────────────────


def test_reviewer_scales_adherence_to_ask_size() -> None:
    for rel in ("agentic-reviewer/prompt.md", "agentic-reviewer/task.md"):
        text = _read(rel).lower()
        assert "size of the ask" in text, f"{rel} missing size-scaled adherence"


def test_reviewer_does_not_own_mechanical_checks() -> None:
    text = _read("agentic-reviewer/prompt.md").lower()
    # Reviewer is judgment-only; the machine already ran lint/tests.
    assert "do not re-run" in text
    assert "machine" in text


# ─────────────────────── slash command (optional entry point) ────────


def test_agentic_slash_command_present() -> None:
    cmd = _REPO_ROOT / ".claude" / "commands" / "agentic.md"
    if not cmd.is_file():
        pytest.skip("slash command not materialized in this layout")
    body = cmd.read_text()
    assert "software-dev-agentic" in body
    assert "$ARGUMENTS" in body
    assert "po run" in body
