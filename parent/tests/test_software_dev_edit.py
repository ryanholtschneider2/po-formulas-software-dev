"""Smoke test: software_dev_edit runs plan→build→close with no lint step."""

import json
import inspect
import logging
from pathlib import Path

import pytest

import po_formulas.software_dev as sd_mod
from po_formulas.software_dev import software_dev_edit
from prefect_orchestration.artifact_contract import (
    classify_work_type,
    format_handoff_note,
    write_artifact_manifest,
)


def test_no_lint_step_in_edit():
    src = inspect.getsource(software_dev_edit)
    assert "linter" not in src, "software_dev_edit must not call the linter"
    assert 'step="lint"' not in src and "step='lint'" not in src


def test_docstring_matches_pipeline():
    doc = software_dev_edit.__doc__ or ""
    assert "plan → build → close" in doc
    # Docstring header must not name lint as a pipeline step (listing it as excluded is fine)
    first_line = doc.strip().splitlines()[0]
    assert "lint" not in first_line.lower()


def test_artifact_contract_work_type_mapping():
    assert (
        classify_work_type(complexity="simple", is_docs_only=False, has_ui=False)
        == "backend-code"
    )
    assert (
        classify_work_type(complexity="moderate", is_docs_only=False, has_ui=True)
        == "ui"
    )
    assert (
        classify_work_type(complexity="simple", is_docs_only=True, has_ui=False)
        == "docs-only"
    )
    assert (
        classify_work_type(complexity="complex", is_docs_only=False, has_ui=False)
        == "verification-heavy"
    )


def test_ui_manifest_marks_required_and_optional_entries(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    review_dir = run_dir / "review-artifacts"
    review_dir.mkdir()
    (review_dir / "summary.md").write_text("# Summary\n")

    manifest_path = write_artifact_manifest(
        run_dir,
        complexity="complex",
        is_docs_only=False,
        has_ui=True,
    )
    payload = json.loads(manifest_path.read_text())

    assert payload["locations"]["review_artifacts"] == "review-artifacts"
    entries = {entry["artifact_type"]: entry for entry in payload["artifacts"]}
    assert entries["handoff-summary"]["status"] == "present"
    assert entries["overview"]["status"] == "skipped"
    assert entries["smoke-output"]["status"] == "missing"
    assert entries["demo-video"]["status"] == "skipped"


def test_verification_heavy_manifest_requires_overview(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    review_dir = run_dir / "review-artifacts"
    review_dir.mkdir()
    (review_dir / "summary.md").write_text("# Summary\n")

    manifest_path = write_artifact_manifest(
        run_dir,
        complexity="complex",
        is_docs_only=False,
        has_ui=False,
    )
    payload = json.loads(manifest_path.read_text())

    entries = {entry["artifact_type"]: entry for entry in payload["artifacts"]}
    assert entries["overview"]["required"] is True
    assert entries["overview"]["status"] == "missing"


def test_trivial_close_note_points_at_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = tmp_path / "rig"
    rig.mkdir()
    test_logger = logging.getLogger("po_formulas.software_dev.artifact_contract")
    test_logger.setLevel(logging.INFO)

    monkeypatch.setattr(sd_mod, "get_run_logger", lambda: test_logger)
    monkeypatch.setattr(sd_mod, "_agent_step_task", lambda **kw: {"status": "ok"})
    monkeypatch.setattr(
        sd_mod,
        "_read_triage_flags",
        lambda *a, **kw: {
            "complexity": "trivial",
            "is_docs_only": False,
            "has_ui": False,
        },
    )
    monkeypatch.setattr(sd_mod, "claim_issue", lambda *a, **kw: None)
    monkeypatch.setattr(sd_mod, "publish_run_artifacts", lambda *a, **kw: None)

    note_store: dict[str, str] = {}

    def fake_close_issue(
        issue_id: str,
        notes: str | None = None,
        rig_path: Path | None = None,
    ) -> None:
        del issue_id, rig_path
        if notes is not None:
            note_store["value"] = notes

    run_dir = rig / ".planning" / "software-dev-full" / "issue-1"
    expected_note = format_handoff_note(
        "po simple-mode complete (trivial path)",
        run_dir,
    )
    monkeypatch.setattr(sd_mod, "close_issue", fake_close_issue)

    sd_mod.software_dev_full.fn(
        issue_id="issue-1",
        rig="rig",
        rig_path=str(rig),
        claim=True,
        dry_run=False,
    )

    assert note_store["value"] == expected_note
    assert (run_dir / "artifact-manifest.json").is_file()
    assert (run_dir / "review-artifacts" / "summary.md").is_file()
