"""Flow-outcome logging for software_dev_full (nanocorps-6q4).

Covers AC2: every flow-body exception writes <run_dir>/flow_outcome.json
with structured failure metadata before re-raising. Existing Prefect
terminal-state semantics preserved.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

import pytest

import po_formulas.software_dev as sd_mod


def _stub_triage_returning_trivial(monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_flow_outcome_work_landed_predicate(tmp_path: Path) -> None:
    """Non-empty build-iter-*.diff in run_dir -> True; otherwise False."""
    rd = tmp_path / "rd"
    rd.mkdir()

    # Empty dir → False
    assert sd_mod._compute_work_landed(rd) is False
    # Missing dir → False
    assert sd_mod._compute_work_landed(tmp_path / "nope") is False

    # Empty diff file → False (builder ran but committed nothing)
    (rd / "build-iter-1.diff").write_bytes(b"")
    assert sd_mod._compute_work_landed(rd) is False

    # Non-empty diff → True
    (rd / "build-iter-1.diff").write_bytes(b"diff --git a/x b/x\n+hello\n")
    assert sd_mod._compute_work_landed(rd) is True

    # Multi-iter: any non-empty wins, regardless of order
    rd2 = tmp_path / "rd2"
    rd2.mkdir()
    (rd2 / "build-iter-1.diff").write_bytes(b"")
    (rd2 / "build-iter-2.diff").write_bytes(b"diff --git a/y b/y\n")
    assert sd_mod._compute_work_landed(rd2) is True

    # Non-build-iter files in the dir don't trigger the True branch
    rd3 = tmp_path / "rd3"
    rd3.mkdir()
    (rd3 / "decision-log.md").write_text("# notes")
    assert sd_mod._compute_work_landed(rd3) is False


def test_flow_outcome_reads_bd_seed_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_record_flow_outcome shells `bd show <seed> --json` and parses
    `status`. TimeoutExpired → bd_seed_closed=None + bd_lookup_error='timeout'."""

    # Happy path: bd returns closed seed.
    def fake_run_closed(cmd: list[str], **_kw: object) -> object:
        return type(
            "P",
            (),
            {
                "returncode": 0,
                "stdout": json.dumps([{"status": "closed"}]),
                "stderr": "",
            },
        )()

    monkeypatch.setattr(sd_mod.subprocess, "run", fake_run_closed)
    run_dir = tmp_path / "run-closed"
    run_dir.mkdir()
    sd_mod._record_flow_outcome(run_dir, RuntimeError("boom"), "seed-1", str(tmp_path))
    out = json.loads((run_dir / "flow_outcome.json").read_text())
    assert out["bd_seed_closed"] is True
    assert out["bd_lookup_error"] is None
    assert out["exception_class"] == "RuntimeError"
    assert "boom" in out["exception_msg"]

    # Timeout path.
    def fake_run_timeout(cmd: list[str], **_kw: object) -> object:
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=5)

    monkeypatch.setattr(sd_mod.subprocess, "run", fake_run_timeout)
    run_dir2 = tmp_path / "run-timeout"
    run_dir2.mkdir()
    sd_mod._record_flow_outcome(run_dir2, RuntimeError("boom"), "seed-2", str(tmp_path))
    out2 = json.loads((run_dir2 / "flow_outcome.json").read_text())
    assert out2["bd_seed_closed"] is None
    assert out2["bd_lookup_error"] == "timeout"


def test_flow_outcome_written_on_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raise inside the flow body triggers flow_outcome.json write and
    re-raises the original exception."""
    test_logger = logging.getLogger("po_formulas.software_dev.test_flow_outcome")
    test_logger.setLevel(logging.INFO)

    monkeypatch.setattr(sd_mod, "get_run_logger", lambda: test_logger)

    # First agent_step call (triage) raises — body's wrap should catch
    # and log a flow_outcome.json.
    def boom(**_kw: object) -> None:
        raise RuntimeError("triage exploded")

    monkeypatch.setattr(sd_mod, "_agent_step_task", boom)
    monkeypatch.setattr(sd_mod, "claim_issue", lambda *a, **kw: None)
    monkeypatch.setattr(sd_mod, "close_issue", lambda *a, **kw: None)
    monkeypatch.setattr(
        sd_mod.subprocess,
        "run",
        lambda *a, **kw: type("P", (), {"returncode": 1, "stdout": "", "stderr": ""})(),
    )

    rig = tmp_path / "rig"
    rig.mkdir()

    with pytest.raises(RuntimeError, match="triage exploded"):
        sd_mod.software_dev_full.fn(
            issue_id="seed-fo1",
            rig="rig",
            rig_path=str(rig),
            claim=False,
            dry_run=True,
        )

    outcome_path = (
        rig / ".planning" / "software-dev-full" / "seed-fo1" / "flow_outcome.json"
    )
    assert outcome_path.is_file()
    data = json.loads(outcome_path.read_text())
    assert data["exception_class"] == "RuntimeError"
    assert "triage exploded" in data["exception_msg"]
    assert data["work_landed"] is False  # no build-iter-*.diff written yet


def test_flow_outcome_logger_swallows_own_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure inside _record_flow_outcome (e.g., disk full when
    writing flow_outcome.json) must not raise — the helper is
    best-effort diagnostic that exists to surface the original flow
    exception, not to obscure it with logging errors."""
    original_write_text = sd_mod.Path.write_text

    def explode_on_outcome(self: Path, *args: object, **kwargs: object) -> int:
        if self.name == "flow_outcome.json":
            raise PermissionError("simulated outcome write failure")
        return original_write_text(self, *args, **kwargs)

    monkeypatch.setattr(sd_mod.Path, "write_text", explode_on_outcome)

    # Should NOT raise. Caller is responsible for re-raising the original.
    sd_mod._record_flow_outcome(
        tmp_path,
        RuntimeError("original error"),
        "seed-x",
        str(tmp_path),
    )
