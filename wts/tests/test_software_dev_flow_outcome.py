"""Flow-outcome logging for software_dev_full (nanocorps-6q4) — wts fork.

Mirrors packs/po-formulas-software-dev/tests/test_software_dev_flow_outcome.py
so fork-pack drift on the failure-logging contract is caught at test time.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

import pytest

import po_formulas_wts.software_dev as sd_mod


def test_flow_outcome_work_landed_predicate(tmp_path: Path) -> None:
    rd = tmp_path / "rd"
    rd.mkdir()

    assert sd_mod._compute_work_landed(rd) is False
    assert sd_mod._compute_work_landed(tmp_path / "nope") is False

    (rd / "build-iter-1.diff").write_bytes(b"")
    assert sd_mod._compute_work_landed(rd) is False

    (rd / "build-iter-1.diff").write_bytes(b"diff --git a/x b/x\n+hello\n")
    assert sd_mod._compute_work_landed(rd) is True

    rd2 = tmp_path / "rd2"
    rd2.mkdir()
    (rd2 / "build-iter-1.diff").write_bytes(b"")
    (rd2 / "build-iter-2.diff").write_bytes(b"diff --git a/y b/y\n")
    assert sd_mod._compute_work_landed(rd2) is True

    rd3 = tmp_path / "rd3"
    rd3.mkdir()
    (rd3 / "decision-log.md").write_text("# notes")
    assert sd_mod._compute_work_landed(rd3) is False


def test_flow_outcome_reads_bd_seed_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    test_logger = logging.getLogger("po_formulas_wts.software_dev.test_flow_outcome")
    test_logger.setLevel(logging.INFO)

    monkeypatch.setattr(sd_mod, "get_run_logger", lambda: test_logger)

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
    assert data["work_landed"] is False


def test_flow_outcome_logger_swallows_own_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_write_text = sd_mod.Path.write_text

    def explode_on_outcome(self: Path, *args: object, **kwargs: object) -> int:
        if self.name == "flow_outcome.json":
            raise PermissionError("simulated outcome write failure")
        return original_write_text(self, *args, **kwargs)

    monkeypatch.setattr(sd_mod.Path, "write_text", explode_on_outcome)

    sd_mod._record_flow_outcome(
        tmp_path,
        RuntimeError("original error"),
        "seed-x",
        str(tmp_path),
    )
