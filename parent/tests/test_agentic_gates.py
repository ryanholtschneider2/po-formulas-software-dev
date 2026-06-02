"""Unit tests for `software_dev_agentic` (po-formulas-software-dev-58m).

Covers the pure-Python mechanical gate layer (diff-clean, anti-mock grep,
pytest-summary parse, regression-vs-baseline, command-or-captured) and the
flow's close decision (seed closes iff gates green AND reviewer >= MEDIUM,
and the flow — not the worker — performs the close).

No real agents: the flow tests monkeypatch `agentic.agent_step` to return
canned `AgentStepResult`s and `agentic._mechanical_gates` to a chosen
`GateReport`, mirroring `test_software_dev_flow_outcome.py`.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

import pytest
from prefect_orchestration.agent_step import AgentStepResult

import po_formulas.agentic as ag

_NULL_LOGGER = logging.getLogger("po_formulas.agentic.test")


# ─────────────────────── git fixtures ───────────────────────────────


def _run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, check=True)


def _init_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-q"], path)
    _run(["git", "config", "user.email", "t@t.io"], path)
    _run(["git", "config", "user.name", "tester"], path)
    (path / "seed.txt").write_text("base\n")
    _run(["git", "add", "seed.txt"], path)
    _run(["git", "commit", "-q", "-m", "baseline"], path)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(path),
        capture_output=True,
        text=True,
        check=True,
    )
    return head.stdout.strip()


def _commit_file(path: Path, rel: str, content: str) -> None:
    fp = path / rel
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(content)
    _run(["git", "add", rel], path)
    _run(["git", "commit", "-q", "-m", f"add {rel}"], path)


# ─────────────────────── _parse_pytest_summary ──────────────────────


def test_parse_pytest_summary() -> None:
    assert ag._parse_pytest_summary(
        "======= 58 passed, 1 warning in 1.79s ======="
    ) == (58, 0)
    assert ag._parse_pytest_summary("3 failed, 55 passed in 2s") == (55, 3)
    assert ag._parse_pytest_summary("errors during collection") == (None, None)
    assert ag._parse_pytest_summary("") == (None, None)


# ─────────────────────── _diff_clean ────────────────────────────────


def test_diff_clean_committed_change(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    _commit_file(repo, "po_formulas/new.py", "x = 1\n")
    ok, detail = ag._diff_clean(repo, base)
    assert ok is True, detail


def test_diff_clean_dirty_tree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    (repo / "uncommitted.py").write_text("y = 2\n")  # staged-or-not, not committed
    _run(["git", "add", "uncommitted.py"], repo)
    ok, detail = ag._diff_clean(repo, base)
    assert ok is False
    assert "uncommitted" in detail


def test_diff_clean_no_change(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base = _init_repo(repo)  # HEAD == base, clean tree
    ok, detail = ag._diff_clean(repo, base)
    assert ok is False
    assert "nothing" in detail.lower() or "did nothing" in detail.lower()


# ─────────────────────── _anti_mock_clean ───────────────────────────


def test_anti_mock_flags_production_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    _commit_file(repo, "po_formulas/svc.py", "from unittest.mock import MagicMock\n")
    ok, detail = ag._anti_mock_clean(repo, base)
    assert ok is False
    assert "po_formulas/svc.py" in detail


def test_anti_mock_allows_test_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    _commit_file(repo, "tests/test_svc.py", "from unittest.mock import MagicMock\n")
    ok, _ = ag._anti_mock_clean(repo, base)
    assert ok is True


# ─────────────────────── _cmd_or_captured ───────────────────────────


def test_cmd_or_captured_rerun_pass(tmp_path: Path) -> None:
    ok, detail = ag._cmd_or_captured("lint", "true", tmp_path, tmp_path)
    assert ok is True
    assert "rc=0" in detail
    assert (tmp_path / "gate-lint.txt").is_file()


def test_cmd_or_captured_rerun_fail(tmp_path: Path) -> None:
    ok, _ = ag._cmd_or_captured("tests", "false", tmp_path, tmp_path)
    assert ok is False


def test_cmd_or_captured_captured_tests(tmp_path: Path) -> None:
    (tmp_path / "gate-tests.txt").write_text("10 passed in 1s")
    assert ag._cmd_or_captured("tests", None, tmp_path, tmp_path)[0] is True
    (tmp_path / "gate-tests.txt").write_text("2 failed, 8 passed in 1s")
    assert ag._cmd_or_captured("tests", None, tmp_path, tmp_path)[0] is False


def test_cmd_or_captured_missing_capture(tmp_path: Path) -> None:
    ok, detail = ag._cmd_or_captured("tests", None, tmp_path, tmp_path)
    assert ok is False
    assert "captured" in detail


def test_cmd_or_captured_captured_lint(tmp_path: Path) -> None:
    (tmp_path / "gate-lint.txt").write_text("All checks passed!\n")
    assert ag._cmd_or_captured("lint", None, tmp_path, tmp_path)[0] is True
    (tmp_path / "gate-lint.txt").write_text("Found 3 errors.\n")
    assert ag._cmd_or_captured("lint", None, tmp_path, tmp_path)[0] is False


# ─────────────────────── _regression_ok ─────────────────────────────


def test_regression_higher_passed_count_ok(tmp_path: Path) -> None:
    """Builder note 1: a HIGHER current passed count still passes (worker
    adds new tests, so current > baseline is legitimate)."""
    (tmp_path / "gate-tests.txt").write_text("60 passed in 2s")
    ok, _ = ag._regression_ok(tmp_path, "58 passed, 1 warning in 1.8s")
    assert ok is True


def test_regression_new_failure_blocks(tmp_path: Path) -> None:
    (tmp_path / "gate-tests.txt").write_text("1 failed, 57 passed in 2s")
    ok, _ = ag._regression_ok(tmp_path, "58 passed in 1.8s")
    assert ok is False


def test_regression_unparseable_degrades_to_pass(tmp_path: Path) -> None:
    (tmp_path / "gate-tests.txt").write_text("internal error, no summary")
    ok, detail = ag._regression_ok(tmp_path, "58 passed in 1.8s")
    assert ok is True
    assert "skipped" in detail


# ─────────────────────── _mechanical_gates aggregator ───────────────


def _green_repo(tmp_path: Path) -> tuple[Path, Path, str]:
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    _commit_file(repo, "po_formulas/feat.py", "def feat():\n    return 1\n")
    run_dir = tmp_path / "rd"
    run_dir.mkdir()
    (run_dir / "gate-lint.txt").write_text("All checks passed!\n")
    return repo, run_dir, base


def test_mechanical_gates_all_green_writes_json(tmp_path: Path) -> None:
    repo, run_dir, base = _green_repo(tmp_path)
    (run_dir / "gate-tests.txt").write_text("58 passed in 1s")
    report = ag._mechanical_gates(
        run_dir=run_dir,
        pack_path=repo,
        baseline_ref=base,
        baseline_txt="57 passed in 1s",
        test_cmd=None,
        lint_cmd=None,
    )
    assert report.passed is True
    assert set(report.checks) == {
        "diff_clean",
        "anti_mock",
        "lint",
        "tests",
        "regression",
    }
    written = json.loads((run_dir / "verdicts" / "mechanical-gates.json").read_text())
    assert written["passed"] is True
    assert written["checks"]["diff_clean"] is True


def test_mechanical_gates_test_failure_blocks(tmp_path: Path) -> None:
    repo, run_dir, base = _green_repo(tmp_path)
    (run_dir / "gate-tests.txt").write_text("3 failed, 55 passed in 1s")
    report = ag._mechanical_gates(
        run_dir=run_dir,
        pack_path=repo,
        baseline_ref=base,
        baseline_txt="58 passed in 1s",
        test_cmd=None,
        lint_cmd=None,
    )
    assert report.passed is False
    assert report.checks["tests"] is False


# ─────────────────────── flow close decision ────────────────────────


def _fake_agent_step(calls: list[dict], reviewer_verdict: str):
    def fake(**kw: object) -> AgentStepResult:
        calls.append(dict(kw))
        step = kw.get("step")
        bead = f"{kw['seed_id']}.{step}.iter{kw.get('iter_n')}"
        if step == "review":
            return AgentStepResult(
                bead_id=bead, verdict=reviewer_verdict, closed_by="agent"
            )
        return AgentStepResult(bead_id=bead, verdict="complete", closed_by="agent")

    return fake


def _gate_report(passed: bool) -> ag.GateReport:
    return ag.GateReport(
        passed=passed,
        checks={
            k: passed
            for k in ("diff_clean", "anti_mock", "lint", "tests", "regression")
        },
        details={},
    )


@pytest.mark.parametrize(
    "gates_pass, reviewer, expect_closed, expect_raise",
    [
        (True, "high", True, False),
        (True, "medium", True, False),
        (True, "low", False, True),
    ],
)
def test_close_decision_green_gates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gates_pass: bool,
    reviewer: str,
    expect_closed: bool,
    expect_raise: bool,
) -> None:
    calls: list[dict] = []
    closed: list[str] = []
    monkeypatch.setattr(ag, "agent_step", _fake_agent_step(calls, reviewer))
    monkeypatch.setattr(ag, "_mechanical_gates", lambda **kw: _gate_report(gates_pass))
    monkeypatch.setattr(ag, "get_run_logger", lambda: _NULL_LOGGER)
    monkeypatch.setattr(ag, "claim_issue", lambda *a, **kw: None)
    monkeypatch.setattr(ag, "close_issue", lambda iid, *a, **kw: closed.append(iid))

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
        with pytest.raises(RuntimeError):
            go()
        assert closed == []
    else:
        result = go()
        assert closed == ["seed-1"]
        assert result["reviewer_verdict"] == reviewer

    # Exactly one reviewer call, with the HIGH/MEDIUM/LOW keyword set.
    review_calls = [c for c in calls if c.get("step") == "review"]
    assert len(review_calls) == 1
    assert review_calls[0]["verdict_keywords"] == ("high", "medium", "low")
    # Ordering: baseline → worker → review.
    steps = [c.get("step") for c in calls]
    assert steps == ["baseline", "agentic", "review"]


def test_red_gates_skip_reviewer_and_dont_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gates red on the final iter → reviewer skipped, seed not closed, raise."""
    calls: list[dict] = []
    closed: list[str] = []
    monkeypatch.setattr(ag, "agent_step", _fake_agent_step(calls, "high"))
    monkeypatch.setattr(ag, "_mechanical_gates", lambda **kw: _gate_report(False))
    monkeypatch.setattr(ag, "get_run_logger", lambda: _NULL_LOGGER)
    monkeypatch.setattr(ag, "claim_issue", lambda *a, **kw: None)
    monkeypatch.setattr(ag, "close_issue", lambda iid, *a, **kw: closed.append(iid))

    rig = tmp_path / "rig"
    rig.mkdir()
    with pytest.raises(RuntimeError, match="did not converge"):
        ag.software_dev_agentic.fn(
            issue_id="seed-r",
            rig="rig",
            rig_path=str(rig),
            iter_cap=1,
        )
    assert closed == []
    assert not any(c.get("step") == "review" for c in calls)


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
            issue_id="seed-fo",
            rig="rig",
            rig_path=str(rig),
            claim=False,
        )
    outcome = (
        rig / ".planning" / "software-dev-agentic" / "seed-fo" / "flow_outcome.json"
    )
    assert outcome.is_file()
    data = json.loads(outcome.read_text())
    assert data["exception_class"] == "RuntimeError"
