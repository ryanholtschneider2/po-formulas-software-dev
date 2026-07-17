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
        bead = f"{kw['seed_id']}-{step}-iter{kw.get('iter_n')}"
        if step == "review":
            verdict = seq.pop(0) if seq else "fail"
            return AgentStepResult(bead_id=bead, verdict=verdict, closed_by="agent")
        if step == "merge-back":
            return AgentStepResult(bead_id=bead, verdict="merged", closed_by="agent")
        return AgentStepResult(bead_id=bead, verdict="complete", closed_by="agent")

    return fake


def _patch_common(monkeypatch: pytest.MonkeyPatch, closed: list[str]) -> None:
    monkeypatch.setattr(ag, "get_run_logger", lambda: _NULL_LOGGER)
    monkeypatch.setattr(ag, "claim_issue", lambda *a, **kw: None)
    monkeypatch.setattr(ag, "close_issue", lambda iid, *a, **kw: closed.append(iid))
    monkeypatch.setattr(
        ag.agentic_sizing,
        "read_sizing",
        lambda run_dir: ag.agentic_sizing.SizingDecision(
            "proceed", "small", "low", ("tests",), 2, "Scoped work.", ""
        ),
    )
    monkeypatch.setattr(
        ag.delivery_truth,
        "branch_truth",
        lambda *a, **kw: {
            "base_branch": kw["base_branch"],
            "base_sha": "base-sha",
            "head_branch": kw["branch"],
            "head_sha": "head-sha",
        },
    )
    monkeypatch.setattr(ag.delivery_truth, "require_ancestor", lambda *a, **kw: None)
    monkeypatch.setattr(ag.delivery_truth, "pull_request_truth", lambda *a, **kw: None)
    monkeypatch.setattr(
        ag.delivery_truth,
        "worktree_for_branch",
        lambda repo, branch: Path(repo).parent / f"{Path(repo).name}.{branch}",
    )
    monkeypatch.setattr(
        ag.shared_branch,
        "preflight_child_ancestry",
        lambda *a, **kw: {
            "status": "fresh",
            "epic_branch": kw["epic_branch"],
            "epic_sha": "epic-sha",
            "child_branch": ag.shared_branch.child_branch_name(kw["child_id"]),
            "child_sha": "",
            "worktree": "",
        },
    )
    monkeypatch.setattr(
        ag.delivery_truth,
        "integration_truth",
        lambda *a, **kw: {
            "base_sha": "base-sha",
            "child_sha": "head-sha",
            "integration_sha": "integration-sha",
        },
    )


def _run_dir_for(rig: Path, issue_id: str = "seed-1") -> Path:
    return rig / ".planning" / "software-dev-agentic" / issue_id


def test_has_prior_iter_state_discriminates(tmp_path: Path) -> None:
    rd = tmp_path / "rd"
    rd.mkdir()
    assert ag._has_prior_iter_state(rd) is False  # empty -> first dispatch
    (rd / "metadata.json").write_text("{}")
    assert ag._has_prior_iter_state(rd) is False  # retry-fresh: metadata only
    (rd / "iter-bead-ids.json").write_text("{}")
    assert ag._has_prior_iter_state(rd) is True  # a prior iteration ran


def _go_archive(monkeypatch, rig: Path, *, dry_run: bool = False) -> dict:
    calls: list[dict] = []
    closed: list[str] = []
    monkeypatch.setattr(ag, "agent_step", _fake_agent_step(calls, ["pass"]))
    _patch_common(monkeypatch, closed)
    return ag.software_dev_agentic.fn(
        issue_id="seed-1",
        rig="rig",
        rig_path=str(rig),
        iter_cap=1,
        dry_run=dry_run,
    )


def _worker_ctx(calls: list[dict]) -> dict:
    """Pull the ctx dict the worker (non-review) step was rendered with."""
    worker = next(c for c in calls if c.get("step") == "agentic")
    return dict(worker["ctx"])


def test_base_branch_defaults_to_main_in_worker_ctx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Default: the worker prompt renders against `main` (byte-for-byte unchanged).
    calls: list[dict] = []
    closed: list[str] = []
    monkeypatch.delenv("PO_RESUME", raising=False)
    monkeypatch.setattr(ag, "agent_step", _fake_agent_step(calls, ["pass"]))
    _patch_common(monkeypatch, closed)
    ag.software_dev_agentic.fn(
        issue_id="seed-1", rig="rig", rig_path=str(tmp_path / "rig"), iter_cap=1
    )
    assert _worker_ctx(calls)["base_branch"] == "main"


def test_base_branch_threads_custom_value_to_worker_ctx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `--base-branch <x>` makes the worker cut its worktree + open its PR
    # against <x>, never `main` — the durable fix for child PRs leaking onto
    # the deploy branch.
    calls: list[dict] = []
    closed: list[str] = []
    monkeypatch.delenv("PO_RESUME", raising=False)
    monkeypatch.setattr(ag, "agent_step", _fake_agent_step(calls, ["pass"]))
    _patch_common(monkeypatch, closed)
    ag.software_dev_agentic.fn(
        issue_id="seed-1",
        rig="rig",
        rig_path=str(tmp_path / "rig"),
        iter_cap=1,
        base_branch="redesign-2026-06-28",
    )
    assert _worker_ctx(calls)["base_branch"] == "redesign-2026-06-28"


def test_fresh_redispatch_archives_stale_run_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # prefect-orchestration-17xa: a plain re-dispatch (PO_RESUME unset) of an
    # issue whose run_dir carries prior-iteration state must archive it so the
    # worker isn't short-circuited by the prior run's closed iter beads.
    monkeypatch.delenv("PO_RESUME", raising=False)
    rig = tmp_path / "rig"
    run_dir = _run_dir_for(rig)
    run_dir.mkdir(parents=True)
    (run_dir / "iter-bead-ids.json").write_text('{"seed-1.agentic.1": "seed-1-xyz"}')

    _go_archive(monkeypatch, rig)

    baks = list(run_dir.parent.glob("seed-1.bak-*"))
    assert len(baks) == 1, "stale run_dir should be archived to a .bak-<UTC> sibling"
    assert (baks[0] / "iter-bead-ids.json").exists()  # stale map moved aside
    assert not (run_dir / "iter-bead-ids.json").exists()  # fresh run_dir is clean


def test_resume_keeps_run_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # PO_RESUME=1 is a real continuation — the run_dir (and its cache) must
    # survive so completed iters are correctly skipped.
    monkeypatch.setenv("PO_RESUME", "1")
    rig = tmp_path / "rig"
    run_dir = _run_dir_for(rig)
    run_dir.mkdir(parents=True)
    (run_dir / "iter-bead-ids.json").write_text("{}")

    _go_archive(monkeypatch, rig)

    assert not list(run_dir.parent.glob("seed-1.bak-*"))  # NOT archived
    assert (run_dir / "iter-bead-ids.json").exists()  # preserved


def test_retry_fresh_run_dir_not_re_archived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `po retry` already archived and left a fresh run_dir with only
    # metadata.json — it must not be archived again.
    monkeypatch.delenv("PO_RESUME", raising=False)
    rig = tmp_path / "rig"
    run_dir = _run_dir_for(rig)
    run_dir.mkdir(parents=True)
    (run_dir / "metadata.json").write_text("{}")

    _go_archive(monkeypatch, rig)

    assert not list(run_dir.parent.glob("seed-1.bak-*"))  # no double-archive
    assert (run_dir / "metadata.json").exists()


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
    assert steps == ["sizing", "agentic", "review"]
    review_calls = [c for c in calls if c.get("step") == "review"]
    assert review_calls[0]["verdict_keywords"] == ("pass", "fail")
    assert review_calls[0]["required_artifacts"] == ("learning-receipt.md",)
    assert "Complete the learning receipt" in review_calls[0]["artifact_nudge"]
    assert review_calls[0]["ctx"]["learning_receipt_path"].endswith(
        "/learning-receipt.md"
    )


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
        bead = f"{kw['seed_id']}-{step}-iter{kw.get('iter_n')}"
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
        "sizing",
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
    monkeypatch.setattr(
        ag.agentic_sizing,
        "read_sizing",
        lambda run_dir: ag.agentic_sizing.SizingDecision(
            "proceed", "small", "low", ("worker",), 1, "Scoped work.", ""
        ),
    )

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


def test_ui_delivery_runs_complete_live_proof_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict] = []
    closed: list[str] = []

    def fake(**kwargs: object) -> AgentStepResult:
        calls.append(dict(kwargs))
        step = kwargs["step"]
        run_dir = Path(str(kwargs["run_dir"]))
        if step == "deploy-smoke":
            (run_dir / "smoke-test-output.txt").write_text("SMOKE PASSED\n")
        if step == "demo-video":
            demo_path = (
                tmp_path
                / "rig/.planning/software-dev-agentic/seed-ui/review-artifacts/demo.mp4"
            )
            demo_path.parent.mkdir(parents=True, exist_ok=True)
            demo_path.write_bytes(b"current demo")
        if step == "review-artifacts":
            review_dir = run_dir / "review-artifacts"
            review_dir.mkdir(parents=True, exist_ok=True)
            (review_dir / "summary.md").write_text("# Review\n")
        if step == "verify":
            (run_dir / f"verification-report-iter-{kwargs['iter_n']}.md").write_text(
                "# Verification\n\nPASS\n"
            )
        verdicts = {
            "review": "pass",
            "verify": "approved",
            "demo-video": "recorded",
        }
        return AgentStepResult(
            bead_id=f"seed-{step}",
            verdict=verdicts.get(str(step), "complete"),
            closed_by="agent",
        )

    monkeypatch.setenv("PO_DEMO_VIDEO", "1")
    monkeypatch.setattr(ag, "agent_step", fake)
    _patch_common(monkeypatch, closed)
    monkeypatch.setattr(
        ag.agentic_sizing,
        "read_sizing",
        lambda run_dir: ag.agentic_sizing.SizingDecision(
            "proceed", "medium", "medium", ("web UI",), 1, "Scoped.", "", ("ui",)
        ),
    )
    rig = tmp_path / "rig"
    rig.mkdir()

    result = ag.software_dev_agentic.fn(
        issue_id="seed-ui", rig="rig", rig_path=str(rig), iter_cap=1
    )

    assert [call["step"] for call in calls] == [
        "sizing",
        "agentic",
        "review",
        "deploy-smoke",
        "demo-video",
        "review-artifacts",
        "verify",
    ]
    assert result["verifier_verdict"] == "approved"
    assert result["delivery_plan"] == {
        "review_artifacts": True,
        "live_verifier": True,
        "deploy_smoke": True,
        "demo": True,
    }
    expected_checkout = tmp_path / "rig.agentic-seed-ui"
    proof_steps = {
        "review",
        "deploy-smoke",
        "demo-video",
        "review-artifacts",
        "verify",
    }
    for call in calls:
        if call["step"] in proof_steps:
            assert call["ctx"]["pack_path"] == str(expected_checkout)
            assert call["rig_path"] == str(rig)
    assert closed == ["seed-ui"]


def test_required_demo_skipped_or_missing_retries_and_never_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict] = []
    closed: list[str] = []
    rig = tmp_path / "rig"
    run_dir = rig / ".planning/software-dev-agentic/seed-ui-missing"
    stale_demo = run_dir / "review-artifacts/demo.mp4"
    stale_demo.parent.mkdir(parents=True)
    stale_demo.write_bytes(b"stale demo")

    def fake(**kwargs: object) -> AgentStepResult:
        calls.append(dict(kwargs))
        step = str(kwargs["step"])
        if step == "deploy-smoke":
            (Path(str(kwargs["run_dir"])) / "smoke-test-output.txt").write_text(
                "SMOKE PASSED\n"
            )
        verdict = {"review": "pass", "demo-video": "skipped"}.get(step, "complete")
        return AgentStepResult(
            bead_id=f"seed-{step}", verdict=verdict, closed_by="agent"
        )

    monkeypatch.setenv("PO_DEMO_VIDEO", "1")
    monkeypatch.setattr(ag, "agent_step", fake)
    _patch_common(monkeypatch, closed)
    monkeypatch.setattr(
        ag.agentic_sizing,
        "read_sizing",
        lambda run_dir: ag.agentic_sizing.SizingDecision(
            "proceed", "medium", "medium", ("web UI",), 2, "Scoped.", "", ("ui",)
        ),
    )

    with pytest.raises(RuntimeError, match="did not converge"):
        ag.software_dev_agentic.fn(
            issue_id="seed-ui-missing", rig="rig", rig_path=str(rig), iter_cap=2
        )

    assert [call["step"] for call in calls].count("demo-video") == 2
    assert not stale_demo.exists()
    assert not any(call["step"] == "verify" for call in calls)
    worker_calls = [call for call in calls if call["step"] == "agentic"]
    assert "instead of recorded" in worker_calls[1]["ctx"]["revision_note"]
    assert closed == []


def test_required_demo_role_error_retries_actor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict] = []
    closed: list[str] = []

    def fake(**kwargs: object) -> AgentStepResult:
        calls.append(dict(kwargs))
        step = str(kwargs["step"])
        if step == "deploy-smoke":
            (Path(str(kwargs["run_dir"])) / "smoke-test-output.txt").write_text(
                "SMOKE PASSED\n"
            )
        if step == "demo-video":
            raise RuntimeError("recorder unavailable")
        verdict = "pass" if step == "review" else "complete"
        return AgentStepResult(
            bead_id=f"seed-{step}", verdict=verdict, closed_by="agent"
        )

    monkeypatch.setenv("PO_DEMO_VIDEO", "1")
    monkeypatch.setattr(ag, "agent_step", fake)
    _patch_common(monkeypatch, closed)
    monkeypatch.setattr(
        ag.agentic_sizing,
        "read_sizing",
        lambda run_dir: ag.agentic_sizing.SizingDecision(
            "proceed", "medium", "medium", ("web UI",), 2, "Scoped.", "", ("ui",)
        ),
    )
    rig = tmp_path / "rig"
    rig.mkdir()

    with pytest.raises(RuntimeError, match="did not converge"):
        ag.software_dev_agentic.fn(
            issue_id="seed-ui-error", rig="rig", rig_path=str(rig), iter_cap=2
        )

    worker_calls = [call for call in calls if call["step"] == "agentic"]
    assert "recorder unavailable" in worker_calls[1]["ctx"]["revision_note"]
    assert closed == []


def test_verifier_rejection_returns_report_to_actor_and_reverifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict] = []
    closed: list[str] = []
    verify_verdicts = iter(("rejected", "approved"))
    rig = tmp_path / "rig"
    run_dir = rig / ".planning/software-dev-agentic/seed-live"
    run_dir.mkdir(parents=True)

    def fake(**kwargs: object) -> AgentStepResult:
        calls.append(dict(kwargs))
        step = kwargs["step"]
        current_run_dir = Path(str(kwargs["run_dir"]))
        if step == "review-artifacts":
            review_dir = current_run_dir / "review-artifacts"
            review_dir.mkdir(parents=True, exist_ok=True)
            (review_dir / "summary.md").write_text("# Review\n")
            (review_dir / "overview.md").write_text("# Overview\n")
        if step == "review":
            verdict = "pass"
        elif step == "verify":
            verdict = next(verify_verdicts)
            if verdict == "rejected":
                (run_dir / "verification-report-iter-1.md").write_text(
                    "Live API returned the old response shape."
                )
            else:
                (
                    run_dir / f"verification-report-iter-{kwargs['iter_n']}.md"
                ).write_text("# Verification\n\nPASS\n")
        else:
            verdict = "complete"
        return AgentStepResult(
            bead_id=f"seed-{step}", verdict=verdict, closed_by="agent"
        )

    monkeypatch.setattr(ag, "agent_step", fake)
    _patch_common(monkeypatch, closed)
    monkeypatch.setattr(
        ag.agentic_sizing,
        "read_sizing",
        lambda run_dir: ag.agentic_sizing.SizingDecision(
            "proceed",
            "medium",
            "medium",
            ("workflow",),
            2,
            "Scoped.",
            "",
            ("workflow",),
        ),
    )

    ag.software_dev_agentic.fn(
        issue_id="seed-live", rig="rig", rig_path=str(rig), iter_cap=2
    )

    worker_calls = [call for call in calls if call["step"] == "agentic"]
    assert len(worker_calls) == 2
    assert "old response shape" in worker_calls[1]["ctx"]["revision_note"]
    assert [call["step"] for call in calls].count("verify") == 2
    assert [call["step"] for call in calls].count("review-artifacts") == 2
    assert closed == ["seed-live"]


@pytest.mark.parametrize(
    ("step", "filename", "body", "message"),
    [
        ("deploy-smoke", "smoke-test-output.txt", "", "no fresh"),
        ("deploy-smoke", "smoke-test-output.txt", "SMOKE FAILED", "records failure"),
        ("review-artifacts", "review-artifacts/summary.md", "", "no fresh"),
        ("verify", "verification-report-iter-1.md", "", "no fresh"),
    ],
)
def test_structural_proof_evidence_fails_closed(
    tmp_path: Path, step: str, filename: str, body: str, message: str
) -> None:
    path = tmp_path / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    assert message in ag._proof_evidence_failure(tmp_path, step=step, iter_n=1)


def test_proof_mode_defaults_adaptive_and_strict_is_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PO_AGENTIC_PROOF_MODE", raising=False)
    assert ag._resolve_proof_mode() == "adaptive"
    monkeypatch.setenv("PO_AGENTIC_PROOF_MODE", " STRICT ")
    assert ag._resolve_proof_mode() == "strict"
    monkeypatch.setenv("PO_AGENTIC_PROOF_MODE", "legacy-value")
    assert ag._resolve_proof_mode() == "adaptive"


# ─────────────────────── _dispatch_pr_sheriff (diagnosability) ───────
#
# Every outcome of the PR-sheriff dispatch must leave a log line so a stuck
# PR is debuggable from the run log alone (po-formulas-software-dev-2wp): the
# previously-silent "declined" path made a dispatch that *fired* (downstream
# problem) indistinguishable from one that *never fired* (problem here).


class _RecordingLogger:
    """Captures `logger.info(msg, *args)` calls, rendered like the run logger."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def info(self, msg: str, *args: object) -> None:
        self.lines.append(msg % args if args else msg)


def _sheriff_module(*, returns: bool | None = None, raises: Exception | None = None):
    """A stand-in `po_*.sheriff_dispatch` with a controllable `on_pr_opened`."""
    import types

    mod = types.ModuleType("fake.sheriff_dispatch")

    def on_pr_opened(workspace_dir: str, feature_id: str) -> bool:
        if raises is not None:
            raise raises
        assert returns is not None
        return returns

    mod.on_pr_opened = on_pr_opened  # type: ignore[attr-defined]
    return mod


def _patch_import(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, object]) -> None:
    """Patch `importlib.import_module`; names absent from `mapping` raise ImportError."""

    def fake_import(name: str):
        if name not in mapping:
            raise ImportError(f"No module named {name!r}")
        return mapping[name]

    monkeypatch.setattr(ag.importlib, "import_module", fake_import)


def test_dispatch_logs_start_and_dispatched_then_short_circuits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First module to dispatch wins; the second is never tried."""
    log = _RecordingLogger()
    _patch_import(
        monkeypatch,
        {
            "po_soloco.sheriff_dispatch": _sheriff_module(returns=True),
            "po_director.sheriff_dispatch": _sheriff_module(returns=True),
        },
    )
    ag._dispatch_pr_sheriff(Path("/ws"), "feat-1", log)
    joined = "\n".join(log.lines)
    assert "PR sheriff dispatch — start (issue=feat-1" in joined
    assert "dispatched soloco-sheriff for feat-1" in joined
    # short-circuit: director never reached, no terminal "none" line
    assert "pr-sheriff" not in joined
    assert "no PR sheriff dispatched" not in joined


def test_dispatch_logs_declined_then_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `False` from the first module is logged, then the second is tried."""
    log = _RecordingLogger()
    _patch_import(
        monkeypatch,
        {
            "po_soloco.sheriff_dispatch": _sheriff_module(returns=False),
            "po_director.sheriff_dispatch": _sheriff_module(returns=True),
        },
    )
    ag._dispatch_pr_sheriff(Path("/ws"), "feat-2", log)
    joined = "\n".join(log.lines)
    assert "soloco-sheriff declined feat-2" in joined
    assert "dispatched pr-sheriff for feat-2" in joined
    assert "no PR sheriff dispatched" not in joined


def test_dispatch_logs_terminal_line_when_none_fire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both decline → a terminal line names what was tried (never silent)."""
    log = _RecordingLogger()
    _patch_import(
        monkeypatch,
        {
            "po_soloco.sheriff_dispatch": _sheriff_module(returns=False),
            "po_director.sheriff_dispatch": _sheriff_module(returns=False),
        },
    )
    ag._dispatch_pr_sheriff(Path("/ws"), "feat-3", log)
    joined = "\n".join(log.lines)
    assert "soloco-sheriff declined feat-3" in joined
    assert "pr-sheriff declined feat-3" in joined
    assert (
        "no PR sheriff dispatched for feat-3 (tried: soloco-sheriff, pr-sheriff)"
        in joined
    )


def test_dispatch_logs_unavailable_and_skipped_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Import failure → `unavailable`; on_pr_opened raising → `skipped`."""
    log = _RecordingLogger()
    boom = RuntimeError("prefect unreachable")
    # po_soloco absent entirely (import fails); po_director present but raises.
    _patch_import(
        monkeypatch,
        {"po_director.sheriff_dispatch": _sheriff_module(raises=boom)},
    )
    ag._dispatch_pr_sheriff(Path("/ws"), "feat-4", log)
    joined = "\n".join(log.lines)
    assert "soloco-sheriff unavailable" in joined
    assert "pr-sheriff dispatch skipped (prefect unreachable)" in joined
    assert "no PR sheriff dispatched for feat-4" in joined


# ─────────────────── shared-branch epic mode ────────────────────────


def test_shared_mode_passes_branch_directive_and_integrates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With `epic_branch` set: the worker gets a non-empty branch_directive, the
    CHILD merges itself back via a `merge-back` agent step under the lock (no
    deterministic merge, no PR sheriff), and skips per-child preview stamping."""
    import contextlib

    rig = tmp_path / "rig"
    rig.mkdir()
    calls: list[dict] = []
    closed: list[str] = []
    monkeypatch.setattr(ag, "agent_step", _fake_agent_step(calls, ["pass"]))
    _patch_common(monkeypatch, closed)

    locked: dict = {}
    monkeypatch.setattr(
        ag.shared_branch,
        "ensure_integration_worktree",
        lambda rp, eid: tmp_path / "intwt",
    )

    @contextlib.contextmanager
    def fake_lock(rp, eid):
        locked["eid"] = eid
        yield

    monkeypatch.setattr(ag.shared_branch, "integration_lock", fake_lock)
    # PR sheriff and preview stamping must NOT fire in shared mode.
    monkeypatch.setattr(
        ag,
        "_dispatch_pr_sheriff",
        lambda *a, **k: pytest.fail("shared mode must not dispatch PR sheriff"),
    )
    monkeypatch.setattr(
        ag,
        "_stamp_preview_url",
        lambda *a, **k: pytest.fail("shared mode must not stamp a per-child preview"),
    )

    result = ag.software_dev_agentic.fn(
        issue_id="c1",
        rig="rig",
        rig_path=str(rig),
        iter_cap=1,
        epic_branch="epic/e1",
        parent_epic_id="e1",
    )

    worker_calls = [c for c in calls if c.get("step") == "agentic"]
    directive = worker_calls[0]["ctx"]["branch_directive"]
    assert (
        "epic/e1" in directive and "gh pr create" in directive and "NEVER" in directive
    )
    # The merge-back ran under the epic lock, in the integration worktree.
    mb = [c for c in calls if c.get("step") == "merge-back"]
    assert len(mb) == 1
    assert mb[0]["ctx"]["epic_branch"] == "epic/e1"
    assert mb[0]["ctx"]["child_branch"] == "agentic-c1"
    assert str(mb[0]["ctx"]["worktree"]) == str(tmp_path / "intwt")
    assert locked["eid"] == "e1"
    assert result["integration"]["merged"] is True
    assert closed == ["c1"]


def test_default_mode_directive_empty_and_sheriff_fires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default (no epic_branch): empty branch_directive, no integrate, PR
    sheriff fires — the existing per-child-PR path, unchanged."""
    rig = tmp_path / "rig"
    rig.mkdir()
    calls: list[dict] = []
    closed: list[str] = []
    monkeypatch.setattr(ag, "agent_step", _fake_agent_step(calls, ["pass"]))
    _patch_common(monkeypatch, closed)
    monkeypatch.setattr(
        ag.shared_branch,
        "ensure_integration_worktree",
        lambda *a, **k: pytest.fail(
            "default mode must not touch the integration worktree"
        ),
    )
    monkeypatch.setattr(ag, "_stamp_preview_url", lambda *a, **k: "")
    sheriff: list[str] = []
    monkeypatch.setattr(
        ag, "_dispatch_pr_sheriff", lambda rp, iid, lg: sheriff.append(iid)
    )

    result = ag.software_dev_agentic.fn(
        issue_id="c2",
        rig="rig",
        rig_path=str(rig),
        iter_cap=1,
    )

    worker_calls = [c for c in calls if c.get("step") == "agentic"]
    assert worker_calls[0]["ctx"]["branch_directive"] == ""
    assert sheriff == ["c2"]
    assert result["integration"] is None
