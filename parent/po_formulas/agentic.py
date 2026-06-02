"""Prefect flow: ``software-dev-agentic``.

An inverted-decomposition pipeline. Instead of the actor-critic loop
splitting plan/build/lint/test across many roles, **one worker agent owns
the whole loop** (plan → build → lint → test; it may spawn subagents). After
the worker turn, a **machine-owned mechanical gate layer** (pure Python, no
LLM) adjudicates: tests/lint passed, the diff is clean, no regression vs the
baseline, and no mocks leaked into production code. Then **exactly one
reviewer agent** rates intent-match + step-adherence + quality
``HIGH/MEDIUM/LOW``.

The seed closes **iff** the mechanical gates are green **AND** the reviewer is
``>= MEDIUM`` — and the *flow* (machine) performs the close, never the worker.
The worker only ever closes its own iter bead.

Pipeline::

    claim seed
      → agent_step(baseline)                 (one-shot; writes baseline.txt)
      → loop iter in 1..iter_cap:
            agent_step(agentic-worker)        (plan→build→lint→test, commits)
            _mechanical_gates(...)            (pure Python; writes verdict JSON)
            if not gates.passed:              (retry worker, or fail loud on last iter)
                continue / break-to-fail
            agent_step(agentic-reviewer)      (HIGH/MEDIUM/LOW)
            if gates green and review >= MEDIUM: success
      → close_issue(seed)  on success, else raise (forensics)

All the convergence machinery (bead-stamping, session affinity, nudge ladder,
verdict parsing, cache fast-path, run_dir, ``_record_flow_outcome``) is reused
wholesale from ``agent_step`` and ``software_dev`` — this module adds only the
gate layer and the close decision.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from prefect import flow, get_run_logger
from prefect_orchestration.agent_step import agent_step
from prefect_orchestration.beads_meta import claim_issue, close_issue

from po_formulas.software_dev import (
    _load_rig_env,
    _record_flow_outcome,
    _tag_flow_run_with_issue_id,
)

_AGENTS_DIR = Path(__file__).parent / "agents"

# Added-line tokens that indicate a mock leaked into production code. The
# scan is scoped to non-`tests/` files (legitimate mocking lives in tests).
_MOCK_PATTERN = re.compile(
    r"\b(unittest\.mock|MagicMock|AsyncMock|mock\.patch|@patch|@mock\.patch)\b"
)


@dataclass
class GateReport:
    """Result of the machine-owned mechanical gate layer.

    ``checks`` maps each subcheck name (``diff_clean`` / ``anti_mock`` /
    ``lint`` / ``tests`` / ``regression``) to its boolean outcome;
    ``details`` carries a one-line human-readable reason per check.
    ``passed`` is True iff every subcheck passed.
    """

    passed: bool
    checks: dict[str, bool]
    details: dict[str, str] = field(default_factory=dict)


# ─────────────────────── gate subfunctions (pure Python) ─────────────


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False
    )


def _git_head(pack_path: Path) -> str | None:
    """Resolve HEAD of the pack repo, or None if not a git repo."""
    proc = _git(["rev-parse", "HEAD"], pack_path)
    return proc.stdout.strip() if proc.returncode == 0 else None


def _diff_clean(pack_path: Path, baseline_ref: str | None) -> tuple[bool, str]:
    """The worker committed real work and left no uncommitted changes.

    Fails when the working tree is dirty (the worker forgot to commit) OR
    when nothing actually landed (``baseline_ref..HEAD`` is empty — the
    worker did no work).
    """
    status = _git(["status", "--porcelain"], pack_path)
    if status.returncode != 0:
        return False, "git status failed (not a git repo?)"
    if status.stdout.strip():
        n = len(status.stdout.strip().splitlines())
        return False, f"{n} uncommitted change(s) in working tree"
    if baseline_ref is None:
        return False, "no baseline ref captured"
    landed = _git(["diff", "--stat", f"{baseline_ref}..HEAD"], pack_path)
    if landed.returncode != 0:
        return False, "git diff vs baseline failed"
    if not landed.stdout.strip():
        return False, "no commits landed since baseline (worker did nothing)"
    return True, landed.stdout.strip().splitlines()[-1].strip()


def _anti_mock_clean(pack_path: Path, baseline_ref: str | None) -> tuple[bool, str]:
    """No mock constructs added to non-test production files.

    Scans the added (`+`) lines of ``baseline_ref..HEAD``, tracking the
    current file via the diff's ``+++ b/...`` header so a mock token in a
    legitimately-mocking test file is not flagged.
    """
    if baseline_ref is None:
        return True, "no baseline ref; anti-mock scan skipped"
    diff = _git(["diff", f"{baseline_ref}..HEAD"], pack_path)
    if diff.returncode != 0:
        return True, "git diff failed; anti-mock scan skipped"

    cur_file = ""
    offenders: list[str] = []
    for line in diff.stdout.splitlines():
        if line.startswith("+++ b/"):
            cur_file = line[len("+++ b/") :].strip()
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        if _is_test_path(cur_file):
            continue
        if _MOCK_PATTERN.search(line):
            offenders.append(cur_file)
    if offenders:
        uniq = sorted(set(offenders))
        return False, f"mock constructs added to production files: {', '.join(uniq)}"
    return True, "no mocks in production diff"


def _is_test_path(path: str) -> bool:
    """True when ``path`` lives under a tests dir or is a test_/conftest file."""
    parts = path.split("/")
    if any(p in {"tests", "test"} for p in parts):
        return True
    base = parts[-1] if parts else path
    return (
        base.startswith("test_") or base == "conftest.py" or base.endswith("_test.py")
    )


def _resolve_cmd(kind: str, explicit: str | None, pack_path: Path) -> str | None:
    """kwarg → Makefile target autodetect → None.

    ``kind`` is ``tests`` or ``lint``; the autodetected Makefile targets
    are ``test-unit`` and ``lint`` respectively.
    """
    if explicit:
        return explicit
    target = "test-unit" if kind == "tests" else "lint"
    makefile = pack_path / "Makefile"
    if makefile.is_file():
        try:
            text = makefile.read_text()
        except OSError:
            return None
        if re.search(rf"(?m)^{re.escape(target)}\s*:", text):
            return f"make {target}"
    return None


def _cmd_or_captured(
    kind: str, cmd: str | None, pack_path: Path, run_dir: Path
) -> tuple[bool, str]:
    """Re-run the resolved command (preferred) else parse the worker's tee.

    When ``cmd`` is resolvable, run it in ``pack_path`` — the *machine*
    adjudicates real tool output. The combined output is teed to
    ``run_dir/gate-<kind>.txt`` so ``_regression_ok`` can read the current
    test run. When no command is resolvable, fall back to the worker-teed
    ``run_dir/gate-<kind>.txt`` (the worker prompt mandates teeing the real
    command output, not a self-asserted "passed").
    """
    tee = run_dir / f"gate-{kind}.txt"
    if cmd:
        proc = subprocess.run(
            cmd,
            shell=True,
            cwd=str(pack_path),
            capture_output=True,
            text=True,
            check=False,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        try:
            tee.write_text(output)
        except OSError:
            pass
        return proc.returncode == 0, f"rc={proc.returncode} (reran `{cmd}`)"

    if not tee.is_file():
        return False, f"no {kind} command resolvable and no {tee.name} captured"
    try:
        text = tee.read_text()
    except OSError:
        return False, f"could not read {tee.name}"
    if kind == "tests":
        passed, failed = _parse_pytest_summary(text)
        if passed is None:
            return False, "captured test output has no pytest summary"
        return failed == 0, f"captured: {passed} passed, {failed} failed"
    # lint (or generic): pass when no failure markers present.
    ok = not _has_lint_failure(text)
    return (
        ok,
        "captured lint output clean" if ok else "captured lint output has failures",
    )


def _has_lint_failure(text: str) -> bool:
    """Heuristic failure detection for captured lint/format output."""
    markers = (
        r"would reformat",
        r"\berror\b",
        r"\berrors\b",
        r"\bFAILED\b",
        r"lint failed",
        r"Found \d+ error",
    )
    return any(re.search(m, text, re.IGNORECASE) for m in markers)


def _parse_pytest_summary(text: str) -> tuple[int | None, int | None]:
    """Extract ``(passed, failed)`` from a pytest summary line.

    ``"58 passed, 1 warning"`` → ``(58, 0)``. ``"3 failed, 55 passed"`` →
    ``(55, 3)``. Output with no ``passed`` token → ``(None, None)`` so the
    regression check degrades to pass-with-warning rather than block.
    """
    passed_m = re.search(r"(\d+)\s+passed", text)
    if passed_m is None:
        return None, None
    failed_m = re.search(r"(\d+)\s+failed", text)
    failed = int(failed_m.group(1)) if failed_m else 0
    return int(passed_m.group(1)), failed


def _regression_ok(run_dir: Path, baseline_txt: str) -> tuple[bool, str]:
    """Current run shows no regression vs the baseline.

    Compares the current full-suite run (``run_dir/gate-tests.txt``) against
    the baseline snapshot: ``current_passed >= baseline_passed`` AND
    ``current_failed <= baseline_failed``. A higher passed count (the worker
    adds new tests) still passes. Unparseable on either side → pass-with-warning.
    """
    cur_text = ""
    tee = run_dir / "gate-tests.txt"
    if tee.is_file():
        try:
            cur_text = tee.read_text()
        except OSError:
            cur_text = ""
    base_passed, base_failed = _parse_pytest_summary(baseline_txt)
    cur_passed, cur_failed = _parse_pytest_summary(cur_text)
    if base_passed is None or cur_passed is None:
        return True, "regression check skipped: unparseable pytest summary"
    ok = cur_passed >= base_passed and cur_failed <= base_failed
    detail = (
        f"baseline {base_passed}p/{base_failed}f vs current {cur_passed}p/{cur_failed}f"
    )
    return ok, detail


def _mechanical_gates(
    *,
    run_dir: Path,
    pack_path: Path,
    baseline_ref: str | None,
    baseline_txt: str,
    test_cmd: str | None,
    lint_cmd: str | None,
) -> GateReport:
    """Run every machine-owned subcheck and write the verdict JSON.

    The reviewer never sees these — they are deterministically checkable
    facts (git-clean, anti-mock grep, regression count delta, real tool
    output) and must stay out of the LLM's hands.
    """
    resolved_tests = _resolve_cmd("tests", test_cmd, pack_path)
    resolved_lint = _resolve_cmd("lint", lint_cmd, pack_path)

    diff_ok, diff_detail = _diff_clean(pack_path, baseline_ref)
    mock_ok, mock_detail = _anti_mock_clean(pack_path, baseline_ref)
    lint_ok, lint_detail = _cmd_or_captured("lint", resolved_lint, pack_path, run_dir)
    tests_ok, tests_detail = _cmd_or_captured(
        "tests", resolved_tests, pack_path, run_dir
    )
    regr_ok, regr_detail = _regression_ok(run_dir, baseline_txt)

    checks = {
        "diff_clean": diff_ok,
        "anti_mock": mock_ok,
        "lint": lint_ok,
        "tests": tests_ok,
        "regression": regr_ok,
    }
    details = {
        "diff_clean": diff_detail,
        "anti_mock": mock_detail,
        "lint": lint_detail,
        "tests": tests_detail,
        "regression": regr_detail,
    }
    report = GateReport(passed=all(checks.values()), checks=checks, details=details)
    _write_gate_report(run_dir, report)
    return report


def _write_gate_report(run_dir: Path, report: GateReport) -> None:
    verdicts = run_dir / "verdicts"
    verdicts.mkdir(parents=True, exist_ok=True)
    payload = {
        "passed": report.passed,
        "checks": report.checks,
        "details": report.details,
    }
    (verdicts / "mechanical-gates.json").write_text(json.dumps(payload, indent=2))


def _failed_checks(report: GateReport) -> list[str]:
    return [name for name, ok in report.checks.items() if not ok]


def _revision_note(report: GateReport | None, reviewer_verdict: str) -> str:
    """Compose the retry guidance fed to the worker as ``revision_note``."""
    if report is not None and not report.passed:
        lines = [
            f"- **{name}**: {report.details.get(name, '(no detail)')}"
            for name in _failed_checks(report)
        ]
        return (
            "## Prior mechanical-gate failure\n\n"
            "The machine gates rejected your last build. Fix each failing check, "
            "commit, and exit the turn:\n\n" + "\n".join(lines)
        )
    if reviewer_verdict == "low":
        return (
            "## Prior reviewer verdict: LOW\n\n"
            "The reviewer rated the implementation LOW (intent mismatch, skipped "
            "steps, or poor quality). Read the reviewer's notes on your prior iter "
            "bead, address them, commit, and exit the turn."
        )
    return ""


# ─────────────────────── flow ───────────────────────────────────────


@flow(name="software_dev_agentic", flow_run_name="{issue_id}", log_prints=True)
def software_dev_agentic(
    issue_id: str,
    rig: str,
    rig_path: str,
    pack_path: str | None = None,
    iter_cap: int = 2,
    test_cmd: str | None = None,
    lint_cmd: str | None = None,
    parent_bead: str | None = None,
    dry_run: bool = False,
    claim: bool = True,
) -> dict[str, Any]:
    """Agent-owned-decomposition pipeline gated by machine checks + one reviewer.

    One worker agent owns plan → build → lint → test (it may spawn
    subagents). A pure-Python mechanical gate layer then adjudicates the
    result, and exactly one reviewer agent rates HIGH/MEDIUM/LOW. The seed
    closes iff the gates are green AND the reviewer is >= MEDIUM, and the
    flow (not the worker) performs the close.

    Parameters mirror the ``software_dev_full`` subset fanout dispatchers
    care about, plus ``test_cmd`` / ``lint_cmd`` for rigs that need an
    explicit gate command (the gates auto-detect a Makefile ``test-unit`` /
    ``lint`` target, else fall back to the worker-teed output).
    """
    logger = get_run_logger()
    rig_path_p = Path(rig_path).expanduser().resolve()
    pack_path_p = Path(pack_path).expanduser().resolve() if pack_path else rig_path_p
    run_dir = rig_path_p / ".planning" / "software-dev-agentic" / issue_id
    run_dir.mkdir(parents=True, exist_ok=True)

    if claim and not dry_run:
        claim_issue(issue_id, assignee=f"po-{os.getpid()}", rig_path=rig_path_p)

    _load_rig_env(rig_path_p)
    _tag_flow_run_with_issue_id(issue_id, logger)

    try:
        # Capture HEAD BEFORE any worker commit. Every gate diffs
        # `baseline_ref..HEAD` (NOT HEAD~1..HEAD) so a multi-commit worker
        # is fully covered.
        baseline_ref = _git_head(pack_path_p)

        # One-shot baseline capture (reuses the existing baseline role).
        agent_step(
            agent_dir=_AGENTS_DIR / "baseline",
            task=_AGENTS_DIR / "baseline" / "task.md",
            seed_id=issue_id,
            rig_path=str(rig_path_p),
            run_dir=run_dir,
            step="baseline",
            iter_n=1,
            ctx={"issue_id": issue_id, "pack_path": str(pack_path_p)},
            verdict_keywords=("complete", "failed"),
            dry_run=dry_run,
        )
        baseline_txt = _read_text(run_dir / "baseline.txt")

        gates: GateReport | None = None
        reviewer_verdict = ""
        success = False
        for iter_n in range(1, iter_cap + 1):
            worker = agent_step(
                agent_dir=_AGENTS_DIR / "agentic-worker",
                task=_AGENTS_DIR / "agentic-worker" / "task.md",
                seed_id=issue_id,
                rig_path=str(rig_path_p),
                run_dir=run_dir,
                step="agentic",
                iter_n=iter_n,
                ctx={
                    "iter": iter_n,
                    "pack_path": str(pack_path_p),
                    "revision_note": _revision_note(gates, reviewer_verdict),
                },
                verdict_keywords=("complete", "failed"),
                dry_run=dry_run,
            )
            logger.info(
                "agentic: worker iter %s closed_by=%s", iter_n, worker.closed_by
            )

            if dry_run:
                # StubBackend does no real commits, so the real git/test
                # gates can't pass. Synthesize a green report so the
                # `--dry-run` wiring smoke runs worker→gates→reviewer→close
                # end to end.
                gates = GateReport(
                    passed=True,
                    checks={
                        k: True
                        for k in (
                            "diff_clean",
                            "anti_mock",
                            "lint",
                            "tests",
                            "regression",
                        )
                    },
                    details={"dry_run": "synthetic pass (StubBackend)"},
                )
                _write_gate_report(run_dir, gates)
            else:
                gates = _mechanical_gates(
                    run_dir=run_dir,
                    pack_path=pack_path_p,
                    baseline_ref=baseline_ref,
                    baseline_txt=baseline_txt,
                    test_cmd=test_cmd,
                    lint_cmd=lint_cmd,
                )
            logger.info(
                "agentic: iter %s gates passed=%s failing=%s",
                iter_n,
                gates.passed,
                _failed_checks(gates),
            )

            if not gates.passed:
                # Skip the reviewer on the final iter: judging known-red
                # gates burns a model turn and produces a confusing
                # "reviewer ran but seed didn't close" artifact.
                if iter_n == iter_cap:
                    break
                continue

            review = agent_step(
                agent_dir=_AGENTS_DIR / "agentic-reviewer",
                task=_AGENTS_DIR / "agentic-reviewer" / "task.md",
                seed_id=issue_id,
                rig_path=str(rig_path_p),
                run_dir=run_dir,
                step="review",
                iter_n=iter_n,
                ctx={"iter": iter_n, "pack_path": str(pack_path_p)},
                verdict_keywords=("high", "medium", "low"),
                dry_run=dry_run,
            )
            reviewer_verdict = review.verdict
            logger.info("agentic: iter %s reviewer=%s", iter_n, reviewer_verdict)
            if gates.passed and reviewer_verdict in {"high", "medium"}:
                success = True
                break

        if not success:
            # Leave the seed open and raise for forensics — run_dir artifacts
            # (gate JSON, diffs, sessions) stay for `po retry` / inspection.
            failing = _failed_checks(gates) if gates else ["(no gate report)"]
            raise RuntimeError(
                f"software-dev-agentic: did not converge after {iter_cap} iter(s) — "
                f"gates_passed={getattr(gates, 'passed', None)} "
                f"failing={failing} reviewer={reviewer_verdict or '(skipped)'}"
            )

        if claim and not dry_run:
            close_issue(
                issue_id,
                notes=(
                    f"po software-dev-agentic complete: gates green, "
                    f"reviewer={reviewer_verdict}"
                ),
                rig_path=rig_path_p,
            )

        return {
            "status": "completed",
            "gates_passed": gates.passed if gates else False,
            "gate_checks": gates.checks if gates else {},
            "reviewer_verdict": reviewer_verdict,
        }
    except Exception as exc:
        _record_flow_outcome(run_dir, exc, issue_id, str(rig_path_p))
        raise


def _read_text(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


__all__ = ["software_dev_agentic", "GateReport"]
