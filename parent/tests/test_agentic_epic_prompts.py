"""Content tests for the agentic-epic role prompts (po-formulas-software-dev-3ek).

The agentic-epic flow is steered almost entirely by its role prompts: the
brainstorm gate, the PRD author, the planner, and the two critics. These
assertions pin the substance ported from the mature beads-epic skills so a
future edit can't silently drop it — most importantly:

  * decomposition is by LOGICAL CHUNK with **no numeric child-count target or
    cap** anywhere (incl. the removed ``max_children`` hard reject);
  * the plan-critic's deep, code-grounded checks;
  * the brainstorm two-role debate;
  * template-var + verdict contracts stay intact.
"""

from __future__ import annotations

import re

import po_formulas.agentic_epic as ae

_AGENTS = ae._AGENTS_DIR
_EPIC_DIRS = (
    "agentic-epic-brainstorm",
    "agentic-epic-prd",
    "agentic-epic-planner",
    "agentic-epic-plan-critic",
    "agentic-epic-acceptance-critic",
)


def _read(rel: str) -> str:
    return (_AGENTS / rel).read_text()


def _all_epic_prompt_text() -> dict[str, str]:
    """Every prompt.md + task.md across the agentic-epic roles, by relative path."""
    out: dict[str, str] = {}
    for d in _EPIC_DIRS:
        for fname in ("prompt.md", "task.md"):
            rel = f"{d}/{fname}"
            out[rel] = _read(rel)
    return out


# ── No numeric child-count target / cap anywhere (the operator directive) ─────

# A range like "5-10 children", ">10 issues", "2-5 AC", "1-5 files", "7-8 steps".
# Scoped to a count NOUN so it can't false-positive on the incident date
# (2026-06-14), "returns 401", or "8-child" (digit-dash-word, not a range).
_COUNT_RANGE = re.compile(
    r"\b\d+\s*[-–]\s*\d+\s*"
    r"(children|child|issues?|acceptance criteria|ac\b|files?|steps?|epics?)\b",
    re.IGNORECASE,
)
_GT_COUNT = re.compile(r">\s*\d+\s*(children|issues?)\b", re.IGNORECASE)


def test_no_numeric_child_count_target_in_prompts() -> None:
    for rel, text in _all_epic_prompt_text().items():
        assert not _COUNT_RANGE.search(text), f"{rel}: numeric count range present"
        assert not _GT_COUNT.search(text), f"{rel}: '>N children/issues' target present"
        assert "max_children" not in text and "max-children" not in text, (
            f"{rel}: max_children reference present"
        )


def test_no_max_children_in_flow() -> None:
    src = (ae.__file__ and __import__("pathlib").Path(ae.__file__).read_text()) or ""
    assert "max_children" not in src, "agentic_epic.py still references max_children"
    assert not _COUNT_RANGE.search(src), "agentic_epic.py has a numeric count range"


# ── Decomposition by logical chunk (planner) ─────────────────────────────────


def test_planner_decomposes_by_logical_chunk() -> None:
    text = _read("agentic-epic-planner/prompt.md").lower()
    assert "logical separable chunk" in text or "logical chunk" in text
    assert "plan, build, test" in text and "document together" in text
    # Qualitative boundary tests + anti-patterns kept.
    assert "one logical concern per child" in text
    assert "revertable commit" in text
    assert "no gaps" in text and "no overlap" in text
    assert "never pad" in text
    assert "merge before split" in text
    assert "don't size by time" in text or "do not size by time" in text
    # Decompose by capability, not layer.
    assert "capability" in text and "layer" in text
    # Worked examples (too big / too small).
    assert "entire auth system" in text
    assert "rename" in text and "register" in text


def test_planner_acceptance_criteria_are_outcomes() -> None:
    text = _read("agentic-epic-planner/prompt.md").lower()
    assert "acceptance criteria" in text
    assert "401" in text  # the GOOD outcome example
    assert "auth works" in text  # the BAD (vague) example


def test_planner_ordering_and_serial_chain_antipattern() -> None:
    text = _read("agentic-epic-planner/prompt.md").lower()
    # Ordering principles.
    assert "infrastructure" in text and "first" in text
    assert "core before polish" in text
    assert "shared utilities before" in text or "shared utils before" in text
    assert "tests live in finalize" in text or "tests in finalize" in text
    # Serial-chain anti-pattern with the incident.
    assert "serial-chain" in text
    assert "2026-06-14" in text
    # Dep direction is the planner's own depends_on (no auto-inference).
    assert "depends_on" in text
    assert "re-verify" in text  # independent re-verification mandate


# ── Critics much more thorough ───────────────────────────────────────────────


def test_plan_critic_has_deep_code_grounded_checks() -> None:
    text = _read("agentic-epic-plan-critic/prompt.md").lower()
    # Coverage walk per PRD AC (pulled forward from acceptance).
    assert "walk the prd acceptance criteria" in text
    assert "one by one" in text
    # Code-grounded: actually open/grep the cited files.
    assert "open" in text and "grep" in text
    assert "do not trust" in text
    # The full check set.
    assert "no overlap" in text
    assert "sizing" in text
    assert "coupling" in text and "touches" in text
    assert "buildability" in text
    assert "layer-decomposition" in text or "layer decomposition" in text
    assert "infra" in text
    assert "ordering" in text
    # Verdict discipline.
    assert "default to fail" in text
    # Re-verify mandate.
    assert "re-verify" in text


def test_acceptance_critic_keeps_strengths_and_notes_finalize() -> None:
    prompt = _read("agentic-epic-acceptance-critic/prompt.md").lower()
    task = _read("agentic-epic-acceptance-critic/task.md").lower()
    # Keeps: per-criterion table, hard constraints, wholeness, default-fail.
    assert "per-criterion" in task or "one by one" in task
    assert "hard constraint" in task
    assert "wholeness" in task
    assert "default to **fail**" in task or "default to fail" in task
    # The finalize step ran the suite — the critic reads its artifacts.
    assert "finalize" in prompt
    assert "post-flight" in prompt or "post-flight" in task


# ── Brainstorm two-role debate (net-new, gated, unattended) ──────────────────


def test_brainstorm_two_role_debate_present() -> None:
    prompt = _read("agentic-epic-brainstorm/prompt.md")
    low = prompt.lower()
    assert "product visionary" in low
    assert "technical architect" in low
    # Question-driven convergence, not round-capped.
    assert "NO MORE QUESTIONS" in prompt
    assert "not artificially cap" in low or "do not artificially cap" in low or (
        "not round-count-driven" in low
    )
    # Gated: skip-when-overkill; unattended (no human approval).
    assert "skip" in low
    assert "unattended" in low
    # Sequential, full accumulated dialogue, explores real code.
    assert "sequential" in low
    assert "accumulated dialogue" in low
    assert "real code" in low


# ── Template-var + verdict contracts intact ──────────────────────────────────


def test_template_vars_and_verdict_keywords_intact() -> None:
    # Every role still renders its bead-id + close block, and reads seed/run_dir.
    for d in _EPIC_DIRS:
        prompt = _read(f"{d}/prompt.md")
        assert "{{role_step_bead_id}}" in prompt, f"{d}/prompt.md missing bead-id var"
        assert "{{role_step_close_block}}" in prompt, f"{d}/prompt.md missing close block"
        task = _read(f"{d}/task.md")
        assert "{{seed_id}}" in task, f"{d}/task.md missing seed_id"
        assert "{{run_dir}}" in task, f"{d}/task.md missing run_dir"
    # The verdict-file artifacts the flow reads stay named as the flow expects.
    assert "{{prd_file}}" in _read("agentic-epic-prd/task.md")
    assert "{{plan_file}}" in _read("agentic-epic-planner/task.md")
    assert "{{design_file}}" in _read("agentic-epic-brainstorm/task.md")
    assert "critique-epic-plan-iter-{{iter}}.md" in _read(
        "agentic-epic-plan-critic/task.md"
    )
    assert "critique-epic-acceptance.md" in _read(
        "agentic-epic-acceptance-critic/task.md"
    )
