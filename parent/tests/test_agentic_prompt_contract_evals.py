"""Coverage for the live worker/reviewer prompt-contract regression harness."""

from __future__ import annotations

import json

from evals import run_agentic_prompt_contract_evals as prompt_evals


def test_cases_render_the_shipped_role_templates(tmp_path) -> None:
    cases = json.loads(prompt_evals.CASES_PATH.read_text())
    rendered = {
        case["id"]: prompt_evals._render_role(case, tmp_path / case["id"])
        for case in cases
    }

    worker = rendered["worker-rejects-skill-driven-scope-expansion"]
    assert "Assignment scope fence" in worker
    assert "auto-loaded skill" in worker
    assert "Stripe billing configuration" in worker
    assert "{{" not in worker

    reviewer = rendered["reviewer-returns-ranked-verdict-at-evidence-stop"]
    assert "Bounded read-only review contract" in reviewer
    assert "declared evidence set is exhausted" in reviewer
    assert "Dagu migration" in reviewer
    assert "{{" not in reviewer


def test_harness_checks_the_two_declared_behavioral_outcomes(
    tmp_path, monkeypatch
) -> None:
    def fake_codex(**kwargs):
        if "worker-rejects" in kwargs["prompt"]:
            return {"outcome": "scope_held", "summary": "Stayed in scope."}
        return {
            "verdict": "fail",
            "findings": [
                {"severity": "blocker", "summary": "Wrong base revision."},
                {"severity": "minor", "summary": "Stale README example."},
            ],
        }

    monkeypatch.setattr(prompt_evals, "_run_codex", fake_codex)
    report = prompt_evals.run(tmp_path / "eval-output", model="fixture")

    assert report["overall_pass"] is True
    assert [case["passed"] for case in report["cases"]] == [True, True]
