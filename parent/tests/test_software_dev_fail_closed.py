"""Quality gates must not silently turn rejection into flow completion."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import logging

import pytest

from po_formulas import software_dev as sd


def test_rejected_plan_exhaustion_fails_flow(tmp_path: Path, monkeypatch) -> None:
    verdicts = {
        "triage": "complex",
        "plan-critic": "rejected",
    }

    def agent_step(**kwargs):
        step = kwargs["step"]
        if step == "triage":
            (tmp_path / ".planning/software-dev-full/issue-1/triage.md").write_text(
                "complex"
            )
        if step == "plan":
            (tmp_path / ".planning/software-dev-full/issue-1/plan.md").write_text(
                "plan"
            )
        return SimpleNamespace(
            verdict=verdicts.get(step, "approved"), summary="rejected", bead_id="iter"
        )

    monkeypatch.setattr(sd, "_agent_step_task", agent_step)
    monkeypatch.setattr(sd, "get_run_logger", lambda: logging.getLogger(__name__))
    monkeypatch.setattr(
        sd,
        "_read_triage_flags",
        lambda *args: {"complexity": "complex", "is_docs_only": False, "has_ui": False},
    )
    monkeypatch.setattr(sd, "_load_rig_env", lambda _path: None)
    monkeypatch.setattr(sd, "claim_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(sd, "_tag_flow_run_with_issue_id", lambda *args: None)

    with pytest.raises(RuntimeError, match="plan review did not approve"):
        sd.software_dev_full.fn(
            issue_id="issue-1",
            rig="test",
            rig_path=str(tmp_path),
            plan_iter_cap=1,
        )
