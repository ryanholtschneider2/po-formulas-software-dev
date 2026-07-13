"""Contract and flow tests for model-judged agentic sizing."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from prefect_orchestration.agent_step import AgentStepResult

import po_formulas.agentic as ag
from po_formulas import agentic_sizing as sizing

_LOGGER = logging.getLogger(__name__)


def _write(run_dir: Path, **overrides: object) -> None:
    payload: dict[str, object] = {
        "decision": "proceed",
        "size": "medium",
        "risk": "medium",
        "surfaces": ["workflow", "tests"],
        "iteration_budget": 3,
        "rationale": "One coherent but integration-heavy PR.",
        "decomposition_reason": "",
    }
    payload.update(overrides)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / sizing.SIZING_FILE).write_text(json.dumps(payload))


def test_read_sizing_validates_structured_judgment(tmp_path: Path) -> None:
    _write(tmp_path)
    decision = sizing.read_sizing(tmp_path)
    assert decision.decision == "proceed"
    assert decision.surfaces == ("workflow", "tests")
    assert decision.iteration_budget == 3


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"iteration_budget": 0}, "between 1 and 4"),
        ({"iteration_budget": True}, "must be an integer"),
        ({"surfaces": []}, "non-empty strings"),
        ({"decision": "maybe"}, "decision must be one of"),
    ],
)
def test_read_sizing_rejects_invalid_structure(
    tmp_path: Path, overrides: dict[str, object], message: str
) -> None:
    _write(tmp_path, **overrides)
    with pytest.raises(sizing.SizingContractError, match=message):
        sizing.read_sizing(tmp_path)


def test_read_sizing_requires_artifact(tmp_path: Path) -> None:
    with pytest.raises(sizing.SizingContractError, match="wrote no"):
        sizing.read_sizing(tmp_path)


def test_operator_cap_is_a_ceiling_not_a_reclassification(tmp_path: Path) -> None:
    _write(tmp_path, iteration_budget=4, size="large", risk="high")
    capped = sizing.apply_operator_cap(sizing.read_sizing(tmp_path), 2)
    assert capped.iteration_budget == 2
    assert capped.size == "large"
    assert capped.risk == "high"


def _patch_flow(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    def fake_step(**kwargs: object) -> AgentStepResult:
        step = str(kwargs["step"])
        calls.append(step)
        run_dir = Path(str(kwargs["run_dir"]))
        if step == "review-artifacts":
            review_dir = run_dir / "review-artifacts"
            review_dir.mkdir(parents=True, exist_ok=True)
            (review_dir / "summary.md").write_text("# Review\n")
        if step == "verify":
            (run_dir / f"verification-report-iter-{kwargs['iter_n']}.md").write_text(
                "# Verification\n\nPASS\n"
            )
        verdict = {"review": "pass", "verify": "approved"}.get(step, "complete")
        return AgentStepResult(bead_id=f"iter-{step}", verdict=verdict)

    monkeypatch.setattr(ag, "agent_step", fake_step)
    monkeypatch.setattr(ag, "get_run_logger", lambda: _LOGGER)
    monkeypatch.setattr(ag, "claim_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(ag, "close_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(ag, "_record_sizing_labels", lambda *args, **kwargs: None)
    monkeypatch.setattr(ag, "_dispatch_pr_sheriff", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        ag.delivery_truth,
        "branch_truth",
        lambda repo, *, branch, base_branch: {
            "base_branch": base_branch,
            "base_sha": "base-sha",
            "head_branch": branch,
            "head_sha": "head-sha",
        },
    )
    monkeypatch.setattr(
        ag.delivery_truth, "pull_request_truth", lambda *args, **kwargs: None
    )


def test_sizing_precedes_worker_and_budget_is_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / ".planning/software-dev-agentic/seed"
    _write(run_dir, iteration_budget=3)
    calls: list[str] = []
    _patch_flow(monkeypatch, calls)

    result = ag.software_dev_agentic.fn(
        issue_id="seed", rig="rig", rig_path=str(tmp_path), pack_path=str(tmp_path)
    )

    assert calls == ["sizing", "agentic", "review", "review-artifacts", "verify"]
    assert result["verified_delivery"]["sizing"]["iteration_budget"] == 3


def test_strict_proof_mode_extends_adaptive_plan_without_reclassification() -> None:
    adaptive = sizing.DeliveryPlan(False, False, False, False)
    strict = sizing.apply_proof_mode(adaptive, "strict")
    assert strict == sizing.DeliveryPlan(True, True, False, False)
    assert sizing.apply_proof_mode(adaptive, "adaptive") is adaptive
    with pytest.raises(ValueError, match="proof mode"):
        sizing.apply_proof_mode(adaptive, "mandatory")


def test_decomposition_refusal_never_dispatches_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / ".planning/software-dev-agentic/storybook-rebuild"
    _write(
        run_dir,
        decision="decompose",
        size="oversized",
        risk="high",
        iteration_budget=4,
        surfaces=["editor", "onboarding", "commerce", "publishing"],
        decomposition_reason="Independent owner-journey surfaces need separate proof.",
    )
    calls: list[str] = []
    _patch_flow(monkeypatch, calls)

    with pytest.raises(sizing.DecompositionRequiredError, match="agentic-epic"):
        ag.software_dev_agentic.fn(
            issue_id="storybook-rebuild",
            rig="rig",
            rig_path=str(tmp_path),
            pack_path=str(tmp_path),
        )

    assert calls == ["sizing"]
    contract = json.loads((run_dir / "verified-delivery.json").read_text())
    assert contract["terminal"]["state"] == "rejected"
    assert contract["sizing"]["decision"] == "decompose"


def test_prompt_contains_no_deterministic_semantic_sizing() -> None:
    prompt = (ag._AGENTS_DIR / "agentic-sizer/prompt.md").read_text().lower()
    task = (ag._AGENTS_DIR / "agentic-sizer/task.md").read_text().lower()
    assert "zero framework cognition" in prompt
    assert "do not use keyword" in task
    assert "sizing.json" in task
    assert "iteration_budget" in task


def test_scale_failure_eval_corpus_pins_courtpro_storybook_and_trivial() -> None:
    corpus_path = Path(__file__).parents[1] / "evals/agentic-sizing-cases.json"
    corpus = json.loads(corpus_path.read_text())
    cases = {case["id"]: case for case in corpus["cases"]}
    assert cases["trivial-doc-fix"]["expected_decision"] == "proceed"
    assert cases["trivial-doc-fix"]["expected_budget"] == [1]
    assert cases["courtpro-full-product-rebuild"]["expected_decision"] == "decompose"
    assert cases["storybook-owner-journey-rebuild"]["expected_decision"] == "decompose"


@pytest.mark.parametrize(
    ("surface_types", "risk", "demo_enabled", "expected"),
    [
        (("code",), "low", False, (False, False, False, False)),
        (("workflow",), "low", False, (True, True, False, False)),
        (("api",), "low", False, (True, True, True, False)),
        (("ui",), "low", True, (True, True, True, True)),
        (("code",), "high", False, (True, True, False, False)),
    ],
)
def test_delivery_plan_applies_declared_proof_policy(
    surface_types: tuple[str, ...],
    risk: str,
    demo_enabled: bool,
    expected: tuple[bool, bool, bool, bool],
) -> None:
    decision = sizing.SizingDecision(
        "proceed", "small", risk, ("feature",), 1, "Scoped.", "", surface_types
    )

    plan = sizing.delivery_plan(decision, demo_enabled=demo_enabled)

    assert (
        plan.review_artifacts,
        plan.live_verifier,
        plan.deploy_smoke,
        plan.demo,
    ) == expected
    assert plan.steps() == [name for name, enabled in plan.as_dict().items() if enabled]


def test_read_sizing_rejects_unknown_surface_type(tmp_path: Path) -> None:
    _write(tmp_path, surface_types=["magic"])

    with pytest.raises(sizing.SizingContractError, match="surface_types"):
        sizing.read_sizing(tmp_path)
