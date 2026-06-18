"""Fast-mode close gate + tester collection-error contract (po-formulas-software-dev-7fl).

Two independent gates that let 6 real regressions through the pd-17lv CDR-A series:

1. ``software_dev_fast`` used to close the seed as "complete" regardless of the
   lint / test verdicts — including when a verdict was *empty* (the step never
   wrote a verdict file, i.e. didn't run / unknown result). It must now close
   only when lint == "clean" AND unit == "passed"; anything else (empty or
   non-clean / non-passed) leaves the seed open with status "needs-review".
2. The tester's task.md must instruct a ``--collect-only`` pass and treat
   ``collection_errors >= 1`` (a module that fails to import) as a hard FAILED,
   not a silent skip.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

import po_formulas.software_dev as sd_mod
from po_formulas.software_dev import _AGENTS_DIR


def _run_fast(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    lint: str,
    unit: str,
) -> tuple[dict, list[str]]:
    """Drive ``software_dev_fast`` with stubbed per-role verdicts.

    Returns (result_dict, closed_issue_ids).
    """
    rig = tmp_path / "rig"
    rig.mkdir()

    monkeypatch.setattr(sd_mod, "get_run_logger", lambda: logging.getLogger("test"))
    monkeypatch.setattr(sd_mod, "_load_rig_env", lambda *a, **kw: None)
    monkeypatch.setattr(sd_mod, "_tag_flow_run_with_issue_id", lambda *a, **kw: None)
    monkeypatch.setattr(sd_mod, "claim_issue", lambda *a, **kw: None)
    monkeypatch.setattr(sd_mod, "_read_artifact", lambda *a, **kw: "")
    monkeypatch.setattr(sd_mod, "_write_artifact_contract", lambda **kw: None)

    def fake_step(**kw: object) -> SimpleNamespace:
        step = kw.get("step")
        if step == "lint":
            return SimpleNamespace(verdict=lint)
        if step == "test-unit":
            return SimpleNamespace(verdict=unit)
        return SimpleNamespace(verdict="")

    monkeypatch.setattr(sd_mod, "_agent_step_task", fake_step)

    closed: list[str] = []

    def fake_close(
        issue_id: str, notes: str | None = None, rig_path: Path | None = None
    ) -> None:
        del notes, rig_path
        closed.append(issue_id)

    monkeypatch.setattr(sd_mod, "close_issue", fake_close)

    result = sd_mod.software_dev_fast.fn(
        issue_id="issue-1",
        rig="rig",
        rig_path=str(rig),
        claim=True,
        dry_run=False,
    )
    return result, closed


def test_clean_and_passed_closes_seed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result, closed = _run_fast(monkeypatch, tmp_path, lint="clean", unit="passed")
    assert closed == ["issue-1"]
    assert result["status"] == "completed"


def test_empty_lint_verdict_blocks_close(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The exact incident: lint verdict file never written → empty string.
    result, closed = _run_fast(monkeypatch, tmp_path, lint="", unit="passed")
    assert closed == [], "empty lint verdict must NOT auto-close the seed"
    assert result["status"] == "needs-review"
    assert result["lint_verdict"] == ""


def test_failed_unit_verdict_blocks_close(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A real test regression / collection error surfaced as FAILED must hold the seed.
    result, closed = _run_fast(monkeypatch, tmp_path, lint="clean", unit="failed")
    assert closed == [], "failed unit verdict must NOT auto-close the seed"
    assert result["status"] == "needs-review"


def test_empty_unit_verdict_blocks_close(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result, closed = _run_fast(monkeypatch, tmp_path, lint="clean", unit="")
    assert closed == []
    assert result["status"] == "needs-review"


def _tester_task_text() -> str:
    return (_AGENTS_DIR / "tester" / "task.md").read_text()


def test_tester_prompt_runs_collect_only() -> None:
    text = _tester_task_text()
    assert "--collect-only" in text, "tester must run a collect-only pass"


def test_tester_prompt_gates_on_collection_errors() -> None:
    text = _tester_task_text()
    assert "collection_errors" in text, "tester verdict must report collection_errors"
    # The gate language: >= 1 collection error forces a failed verdict.
    lowered = text.lower()
    assert "collection_errors" in lowered and "failed" in lowered
    assert "≥ 1" in text or ">= 1" in text or "≥1" in text
