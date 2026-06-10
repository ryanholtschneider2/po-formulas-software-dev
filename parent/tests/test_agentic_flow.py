"""Unit tests for `software_dev_agentic` (po-formulas-software-dev-175).

The flow is prompt-driven and minimal: one actor (worktree off main →
build → test → PR) looped against one goal-verifying critic. There is no
mechanical gate layer. These tests pin the *flow's* close decision:

  * the seed closes iff the critic passes,
  * the *flow* (not the actor) performs the close,
  * a failing critic feeds its fix list back to the actor and iterates,
  * non-convergence raises (leaving the seed open) and never merges,
  * a worker exception writes flow_outcome.json and re-raises.

No real agents: `agentic.agent_step` is monkeypatched to return canned
`AgentStepResult`s, mirroring `test_software_dev_flow_outcome.py`.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from prefect_orchestration.agent_step import AgentStepResult

import po_formulas.agentic as ag

_NULL_LOGGER = logging.getLogger("po_formulas.agentic.test")


# ─────────────────────── _revision_note ─────────────────────────────


def test_revision_note_empty_when_no_fix_list() -> None:
    assert ag._revision_note("") == ""
    assert ag._revision_note("   \n  ") == ""


def test_revision_note_wraps_fix_list() -> None:
    note = ag._revision_note("1. do the thing\n2. fix the bug")
    assert "Prior critic verdict: FAIL" in note
    assert "do the thing" in note
    assert "fix the bug" in note


# ─────────────────────── flow close decision ────────────────────────


def _fake_agent_step(calls: list[dict], critic_verdicts: list[str]):
    """Return canned results; pop a critic verdict per `review` step."""
    seq = list(critic_verdicts)

    def fake(**kw: object) -> AgentStepResult:
        calls.append(dict(kw))
        step = kw.get("step")
        bead = f"{kw['seed_id']}.{step}.iter{kw.get('iter_n')}"
        if step == "review":
            verdict = seq.pop(0) if seq else "fail"
            return AgentStepResult(bead_id=bead, verdict=verdict, closed_by="agent")
        return AgentStepResult(bead_id=bead, verdict="complete", closed_by="agent")

    return fake


def _patch_common(monkeypatch: pytest.MonkeyPatch, closed: list[str]) -> None:
    monkeypatch.setattr(ag, "get_run_logger", lambda: _NULL_LOGGER)
    monkeypatch.setattr(ag, "claim_issue", lambda *a, **kw: None)
    monkeypatch.setattr(ag, "close_issue", lambda iid, *a, **kw: closed.append(iid))


@pytest.mark.parametrize(
    "critic, expect_closed, expect_raise",
    [
        ("pass", True, False),
        ("fail", False, True),
    ],
)
def test_close_decision_single_iter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    critic: str,
    expect_closed: bool,
    expect_raise: bool,
) -> None:
    calls: list[dict] = []
    closed: list[str] = []
    monkeypatch.setattr(ag, "agent_step", _fake_agent_step(calls, [critic]))
    _patch_common(monkeypatch, closed)

    rig = tmp_path / "rig"
    rig.mkdir()

    def go() -> dict:
        return ag.software_dev_agentic.fn(
            issue_id="seed-1",
            rig="rig",
            rig_path=str(rig),
            iter_cap=1,
        )

    if expect_raise:
        with pytest.raises(RuntimeError, match="did not converge"):
            go()
        assert closed == []
    else:
        result = go()
        assert closed == ["seed-1"]
        assert result["critic_verdict"] == "pass"

    # Exactly one actor + one critic call this iter, in that order, with the
    # pass/fail keyword set — and NO baseline step (it was dropped).
    steps = [c.get("step") for c in calls]
    assert steps == ["agentic", "review"]
    review_calls = [c for c in calls if c.get("step") == "review"]
    assert review_calls[0]["verdict_keywords"] == ("pass", "fail")


def test_critic_fail_then_pass_iterates_and_feeds_fix_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fail → write critique → next actor turn gets it as revision_note → pass."""
    rig = tmp_path / "rig"
    rig.mkdir()
    run_dir = rig / ".planning" / "software-dev-agentic" / "seed-2"
    run_dir.mkdir(parents=True)

    calls: list[dict] = []
    closed: list[str] = []
    seq = ["fail", "pass"]

    def fake(**kw: object) -> AgentStepResult:
        calls.append(dict(kw))
        step = kw.get("step")
        bead = f"{kw['seed_id']}.{step}.iter{kw.get('iter_n')}"
        if step == "review":
            verdict = seq.pop(0)
            if verdict == "fail":
                (run_dir / f"critique-iter-{kw.get('iter_n')}.md").write_text(
                    "1. missing error-path test"
                )
            return AgentStepResult(bead_id=bead, verdict=verdict, closed_by="agent")
        return AgentStepResult(bead_id=bead, verdict="complete", closed_by="agent")

    monkeypatch.setattr(ag, "agent_step", fake)
    _patch_common(monkeypatch, closed)

    result = ag.software_dev_agentic.fn(
        issue_id="seed-2", rig="rig", rig_path=str(rig), iter_cap=2
    )
    assert result["critic_verdict"] == "pass"
    assert closed == ["seed-2"]

    # The iter-2 worker turn must carry the critic's fix list as revision_note.
    worker_calls = [c for c in calls if c.get("step") == "agentic"]
    assert len(worker_calls) == 2
    assert worker_calls[0]["ctx"]["revision_note"] == ""  # first turn: clean
    assert "missing error-path test" in worker_calls[1]["ctx"]["revision_note"]


def test_no_merge_and_seed_left_open_on_persistent_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Critic fails every iter → raise, seed never closed, flow never merges."""
    calls: list[dict] = []
    closed: list[str] = []
    monkeypatch.setattr(ag, "agent_step", _fake_agent_step(calls, ["fail", "fail"]))
    _patch_common(monkeypatch, closed)
    # The flow module imports nothing that merges; assert there is no merge hook.
    assert not hasattr(ag, "merge_worktree")

    rig = tmp_path / "rig"
    rig.mkdir()
    with pytest.raises(RuntimeError, match="did not converge"):
        ag.software_dev_agentic.fn(
            issue_id="seed-r", rig="rig", rig_path=str(rig), iter_cap=2
        )
    assert closed == []
    # Both iters ran (actor + critic each).
    assert [c.get("step") for c in calls] == [
        "agentic",
        "review",
        "agentic",
        "review",
    ]


def test_dry_run_treats_critic_as_pass_and_closes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--dry-run` exercises actor→critic→close wiring without real closes."""
    calls: list[dict] = []
    closed: list[str] = []
    # Even if the (stub) critic verdict is empty, dry_run forces a pass.
    monkeypatch.setattr(ag, "agent_step", _fake_agent_step(calls, [""]))
    _patch_common(monkeypatch, closed)

    rig = tmp_path / "rig"
    rig.mkdir()
    result = ag.software_dev_agentic.fn(
        issue_id="seed-d", rig="rig", rig_path=str(rig), iter_cap=1, dry_run=True
    )
    assert result["critic_verdict"] == "pass"
    # dry_run skips the real bd close.
    assert closed == []


def test_pr_sheriff_dispatched_on_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A critic pass announces the PR to po-director (the Sheriff trigger)."""
    calls: list[dict] = []
    closed: list[str] = []
    dispatched: list[tuple[str, str]] = []
    monkeypatch.setattr(ag, "agent_step", _fake_agent_step(calls, ["pass"]))
    _patch_common(monkeypatch, closed)
    monkeypatch.setattr(
        ag,
        "_dispatch_pr_sheriff",
        lambda rig_path, issue_id, logger: dispatched.append((str(rig_path), issue_id)),
    )

    rig = tmp_path / "rig"
    rig.mkdir()
    ag.software_dev_agentic.fn(
        issue_id="seed-s", rig="rig", rig_path=str(rig), iter_cap=1
    )
    assert dispatched == [(str(rig.resolve()), "seed-s")]


def test_pr_sheriff_not_dispatched_on_dry_run_or_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No PR exists on a dry-run or a non-converging run — don't dispatch."""
    dispatched: list[tuple[str, str]] = []
    monkeypatch.setattr(
        ag,
        "_dispatch_pr_sheriff",
        lambda rig_path, issue_id, logger: dispatched.append((str(rig_path), issue_id)),
    )

    # dry_run pass — wiring runs, but no real PR, so no dispatch.
    closed: list[str] = []
    monkeypatch.setattr(ag, "agent_step", _fake_agent_step([], [""]))
    _patch_common(monkeypatch, closed)
    rig = tmp_path / "rig"
    rig.mkdir()
    ag.software_dev_agentic.fn(
        issue_id="seed-dr", rig="rig", rig_path=str(rig), iter_cap=1, dry_run=True
    )
    assert dispatched == []

    # Persistent fail — raises before the dispatch point.
    monkeypatch.setattr(ag, "agent_step", _fake_agent_step([], ["fail", "fail"]))
    rig2 = tmp_path / "rig2"
    rig2.mkdir()
    with pytest.raises(RuntimeError, match="did not converge"):
        ag.software_dev_agentic.fn(
            issue_id="seed-f", rig="rig2", rig_path=str(rig2), iter_cap=2
        )
    assert dispatched == []


def test_flow_outcome_written_on_worker_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raise inside the flow body writes flow_outcome.json and re-raises."""

    def boom(**kw: object) -> AgentStepResult:
        if kw.get("step") == "agentic":
            raise RuntimeError("worker exploded")
        return AgentStepResult(bead_id="x", verdict="complete")

    monkeypatch.setattr(ag, "agent_step", boom)
    monkeypatch.setattr(ag, "get_run_logger", lambda: _NULL_LOGGER)
    monkeypatch.setattr(ag, "claim_issue", lambda *a, **kw: None)
    monkeypatch.setattr(ag, "close_issue", lambda *a, **kw: None)

    rig = tmp_path / "rig"
    rig.mkdir()
    with pytest.raises(RuntimeError, match="worker exploded"):
        ag.software_dev_agentic.fn(
            issue_id="seed-fo", rig="rig", rig_path=str(rig), claim=False
        )
    outcome = (
        rig / ".planning" / "software-dev-agentic" / "seed-fo" / "flow_outcome.json"
    )
    assert outcome.is_file()
    data = json.loads(outcome.read_text())
    assert data["exception_class"] == "RuntimeError"
