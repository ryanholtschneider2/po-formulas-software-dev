"""Flow integration tests for the verified-delivery run artifact."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from prefect_orchestration.agent_step import AgentStepResult

import po_formulas.agentic as ag
from po_formulas import verified_delivery as vd

_LOGGER = logging.getLogger(__name__)


def _patch_flow(monkeypatch: pytest.MonkeyPatch, *, worker_error: bool = False) -> None:
    def fake_step(**kwargs: object) -> AgentStepResult:
        if worker_error and kwargs["step"] == "agentic":
            raise RuntimeError("worker exploded")
        verdict = "complete" if kwargs["step"] == "agentic" else "pass"
        return AgentStepResult(bead_id="iter", verdict=verdict, closed_by="agent")

    monkeypatch.setattr(ag, "agent_step", fake_step)
    monkeypatch.setattr(ag, "get_run_logger", lambda: _LOGGER)
    monkeypatch.setattr(ag, "claim_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(ag, "close_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(ag, "_dispatch_pr_sheriff", lambda *args, **kwargs: None)
    monkeypatch.setattr(ag, "_tag_flow_run_with_issue_id", lambda *args: None)


def test_flow_records_provenance_and_terminal_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_flow(monkeypatch)
    run_dir = tmp_path / ".planning" / "software-dev-agentic" / "seed"
    run_dir.mkdir(parents=True)
    (run_dir / ".po-dispatch.json").write_text(
        json.dumps(
            {
                "argv": ["po", "run", "software-dev-agentic", "--issue-id", "seed"],
                "formula": "software-dev-agentic",
                "runtime_env": {
                    "PO_BACKEND": "codex-tmux",
                    "PO_ACCOUNT": "codex-personal",
                    "PO_ACCOUNT_CLASS": "personal",
                    "PO_MODEL_CLI": "gpt-test",
                    "PO_EFFORT_CLI": "high",
                },
            }
        )
    )
    monkeypatch.setattr(
        ag,
        "_git_revision",
        lambda repo, revision="HEAD": (
            "base-sha" if revision == "release" else "head-sha"
        ),
    )

    result = ag.software_dev_agentic.fn(
        issue_id="seed",
        rig="test-rig",
        rig_path=str(tmp_path),
        pack_path=str(tmp_path),
        base_branch="release",
        claim=False,
        iter_cap=1,
    )

    contract = vd.read(run_dir)
    assert result["verified_delivery"] == contract
    assert contract["revisions"] == {
        "base": "base-sha",
        "head": "head-sha",
        "integration": None,
    }
    assert contract["pull_request"]["target"] == "release"
    assert contract["terminal"] == {"state": "completed", "reason": None}
    assert contract["provenance"]["backend"] == "codex-tmux"
    assert contract["provenance"]["model"] == "gpt-test"
    assert contract["provenance"]["dispatch_command"] == (
        "po run software-dev-agentic --issue-id seed"
    )


def test_flow_terminalizes_failure_without_masking_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_flow(monkeypatch, worker_error=True)
    monkeypatch.setattr(ag, "_git_revision", lambda *args, **kwargs: "sha")

    with pytest.raises(RuntimeError, match="worker exploded"):
        ag.software_dev_agentic.fn(
            issue_id="seed",
            rig="test-rig",
            rig_path=str(tmp_path),
            pack_path=str(tmp_path),
            claim=False,
            iter_cap=1,
        )

    contract = vd.read(tmp_path / ".planning" / "software-dev-agentic" / "seed")
    assert contract["terminal"] == {
        "state": "failed",
        "reason": "worker exploded",
    }
