"""Prefect flow: `pre_pr_review`.

Sling-able cumulative review run BEFORE the PR-writer fires. Three
independent pillars per the spec on `nanocorps-fb8`:

  1. Full lint + test + build suite on the worktree, regression-compared
     against `origin/<merge_target>`.
  2. Cumulative-diff critic (dispatched as the `pre-pr-reviewer` agent)
     reading the original epic plan + `git diff` + child summaries,
     answering coherence/fulfilment questions and emitting a structured
     `## Findings` markdown contract.
  3. Real-environment smoke test: `make dev-up` boot + `pre-pr-smoke-tester`
     agent driving a browser against the affected surface.

Output: deterministic `<run_dir>/validation-report.md`. Findings (pillar-1
regressions + pillar-2 critic findings) become `type=bug priority=1` beads
under the epic. The epic is stamped `metadata.validation = passed | blocked`.

Invocation:

    po run pre-pr-review --epic-id <id> --rig <name> --rig-path <path>
    po run pre-pr-review --branch <name> --rig <name> --rig-path <path>

`--epic-id` and `--branch` are mutually exclusive (exactly one required).
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from prefect import flow, get_run_logger
from prefect.exceptions import MissingContextError

from po_formulas_wts.software_dev import _agent_dir, _agent_step_task, _task_md

# ─────────────────────── data shapes ─────────────────────────────────


@dataclass
class PillarResult:
    """One pillar's outcome. `verdict` ∈ {PASSED, FAILED, SKIPPED}."""

    name: str
    verdict: str
    summary: str = ""
    findings: list[tuple[str, str]] = field(default_factory=list)
    artifacts: list[Path] = field(default_factory=list)


_VERDICT_PASSED = "PASSED"
_VERDICT_FAILED = "FAILED"
_VERDICT_SKIPPED = "SKIPPED"


# ─────────────────────── helpers ─────────────────────────────────────


def _logger() -> Any:
    try:
        return get_run_logger()
    except MissingContextError:
        return logging.getLogger(__name__)


def _run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=check,
    )


def _bd_show_metadata(bead_id: str, rig_path: Path) -> dict[str, Any]:
    """Return the bead's metadata dict (empty on any failure)."""
    proc = _run(["bd", "show", bead_id, "--json"], cwd=rig_path)
    if proc.returncode != 0 or not proc.stdout.strip():
        return {}
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}
    if isinstance(data, list) and data:
        data = data[0]
    if not isinstance(data, dict):
        return {}
    md = data.get("metadata") or {}
    return md if isinstance(md, dict) else {}


def _resolve_worktree(
    epic_id: str | None,
    branch: str | None,
    rig_path: Path,
    merge_target_branch: str,
) -> tuple[Path, str, str, list[str]]:
    """Resolve (work_dir, branch_name, merge_target, prelude_warnings).

    Mutual exclusion of epic_id/branch is the caller's responsibility.
    """
    warnings: list[str] = []
    if epic_id is not None:
        md = _bd_show_metadata(epic_id, rig_path)
        work_dir_raw = (md.get("work_dir") or "").strip()
        no_worktree = str(md.get("no_worktree", "")).lower() in ("1", "true", "yes")
        epic_branch = (md.get("branch") or "").strip()
        epic_target = (
            md.get("merge_target_branch") or ""
        ).strip() or merge_target_branch
        if work_dir_raw:
            return Path(work_dir_raw), epic_branch or "(unknown)", epic_target, warnings
        if no_worktree:
            warnings.append(
                "epic metadata.no_worktree=true; running pillars 1+2 against rig root"
            )
            return rig_path, epic_branch or "(unknown)", epic_target, warnings
        warnings.append(
            f"epic {epic_id} has no metadata.work_dir and metadata.no_worktree is not true"
        )
        return rig_path, epic_branch or "(unknown)", epic_target, warnings

    # branch mode
    assert branch is not None
    proc = _run(["git", "worktree", "list", "--porcelain"], cwd=rig_path)
    work_dir = rig_path
    target_ref = f"refs/heads/{branch}"
    current_path: Path | None = None
    for line in proc.stdout.splitlines():
        if line.startswith("worktree "):
            current_path = Path(line[len("worktree ") :].strip())
        elif line.startswith("branch ") and current_path is not None:
            if line[len("branch ") :].strip() == target_ref:
                work_dir = current_path
                break
    if work_dir == rig_path and proc.stdout:
        warnings.append(
            f"branch {branch!r} not found in `git worktree list`; falling back to rig root"
        )
    return work_dir, branch, merge_target_branch, warnings


@contextmanager
def _baseline_checkout(work_dir: Path, merge_target: str) -> Any:
    """Stash + checkout origin/<merge_target>; restore on exit."""
    log = _logger()
    stash_proc = _run(["git", "stash", "create"], cwd=work_dir)
    stash_ref = stash_proc.stdout.strip()
    head_proc = _run(["git", "rev-parse", "HEAD"], cwd=work_dir)
    prior = head_proc.stdout.strip()
    try:
        co = _run(["git", "checkout", f"origin/{merge_target}"], cwd=work_dir)
        if co.returncode != 0:
            log.warning("baseline checkout failed: %s", co.stderr.strip())
            raise RuntimeError(f"baseline checkout failed: {co.stderr.strip()}")
        yield
    finally:
        if prior:
            _run(["git", "checkout", prior], cwd=work_dir)
        if stash_ref:
            _run(["git", "stash", "apply", stash_ref], cwd=work_dir)


_FINDING_RE = re.compile(r"^### Finding (\d+): (.+)$", re.MULTILINE)


def _parse_pillar2_findings(critique_md: str) -> list[tuple[str, str]]:
    """Extract `(title, body)` tuples from the structured critique markdown.

    The body is the text from the line after the heading up to the next
    `### Finding ` heading or EOF, trimmed.
    """
    matches = list(_FINDING_RE.finditer(critique_md))
    out: list[tuple[str, str]] = []
    for idx, m in enumerate(matches):
        title = m.group(2).strip()
        body_start = m.end()
        body_end = (
            matches[idx + 1].start() if idx + 1 < len(matches) else len(critique_md)
        )
        body = critique_md[body_start:body_end].strip()
        out.append((title, body))
    return out


def _epic_plan_path(rig_path: Path, epic_id: str | None) -> Path | None:
    if epic_id is None:
        return None
    candidates = [
        rig_path / ".planning" / "epics" / epic_id / "plan.md",
        rig_path / ".planning" / "software-dev-full" / epic_id / "plan.md",
        rig_path / ".planning" / epic_id / "plan.md",
    ]
    for p in candidates:
        if p.is_file():
            return p
    return None


def _aggregate_child_summaries(rig_path: Path, epic_id: str) -> str:
    """Collect decision-log + lessons-learned text from every child run-dir."""
    bits: list[str] = []
    proc = _run(
        ["bd", "dep", "list", epic_id, "--direction=up", "--type=parent-child"],
        cwd=rig_path,
    )
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        # crude id extraction — first token-like substring matching `<id>` shape
        token = line.split()[0].strip("◐✓○●·")
        for sub in (
            rig_path / ".planning" / "software-dev-full" / token,
            rig_path / ".planning" / token,
        ):
            for fname in ("decision-log.md", "lessons-learned.md"):
                fpath = sub / fname
                if fpath.is_file():
                    bits.append(f"## {token} — {fname}\n\n{fpath.read_text()}")
    return "\n\n---\n\n".join(bits)


# ─────────────────────── pillar 1 ────────────────────────────────────


_TARGETS = ("lint", "test-unit", "test-e2e", "build")


def _make_target_outcomes(work_dir: Path) -> dict[str, tuple[int, str]]:
    """Run every make target; return {target: (rc, log_excerpt)}."""
    out: dict[str, tuple[int, str]] = {}
    for tgt in _TARGETS:
        proc = _run(["make", tgt], cwd=work_dir)
        log_excerpt = (proc.stdout + proc.stderr)[-4000:]
        out[tgt] = (proc.returncode, log_excerpt)
    return out


def _run_pillar_1(
    work_dir: Path,
    merge_target: str,
    report_dir: Path,
) -> PillarResult:
    """Run lint + test-unit + test-e2e + build on branch and on baseline; diff."""
    log = _logger()
    branch_outcomes = _make_target_outcomes(work_dir)
    baseline_outcomes: dict[str, tuple[int, str]] = {}
    try:
        with _baseline_checkout(work_dir, merge_target):
            baseline_outcomes = _make_target_outcomes(work_dir)
    except Exception as exc:  # noqa: BLE001
        log.warning("pillar-1 baseline checkout failed: %s", exc)

    regressions: list[tuple[str, str]] = []
    for tgt in _TARGETS:
        branch_rc, branch_log = branch_outcomes[tgt]
        base_rc = baseline_outcomes.get(tgt, (None, ""))[0]
        if base_rc == 0 and branch_rc != 0:
            regressions.append(
                (f"make {tgt}", f"baseline rc=0, branch rc={branch_rc}\n\n{branch_log}")
            )

    log_path = report_dir / "pillar-1-results.txt"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w") as f:
        for tgt in _TARGETS:
            br_rc, br_log = branch_outcomes[tgt]
            ba_rc = baseline_outcomes.get(tgt, (None, ""))[0]
            f.write(
                f"## make {tgt}\nbranch rc={br_rc}  baseline rc={ba_rc}\n\n{br_log}\n\n"
            )

    verdict = _VERDICT_FAILED if regressions else _VERDICT_PASSED
    summary = (
        f"{len(regressions)} regression(s)"
        if regressions
        else "no regressions vs baseline"
    )
    return PillarResult(
        name="pillar-1",
        verdict=verdict,
        summary=summary,
        findings=regressions,
        artifacts=[log_path],
    )


# ─────────────────────── pillar 2 ────────────────────────────────────


def _stage_pillar2_inputs(
    epic_id: str | None,
    branch: str,
    work_dir: Path,
    merge_target: str,
    rig_path: Path,
    report_dir: Path,
) -> None:
    """Pre-stage epic-plan.md + cumulative.diff + child-summaries.md for the agent."""
    plan_path = _epic_plan_path(rig_path, epic_id)
    if plan_path is not None:
        (report_dir / "epic-plan.md").write_text(plan_path.read_text())
    diff_proc = _run(
        ["git", "diff", f"origin/{merge_target}..{branch}"],
        cwd=work_dir,
    )
    (report_dir / "cumulative.diff").write_text(diff_proc.stdout)
    if epic_id is not None:
        summary_text = _aggregate_child_summaries(rig_path, epic_id)
        if summary_text:
            (report_dir / "child-summaries.md").write_text(summary_text)


def _run_pillar_2(
    epic_id: str | None,
    branch: str,
    work_dir: Path,
    rig_path: Path,
    merge_target: str,
    report_dir: Path,
    *,
    seed_id: str,
    iter_n: int = 1,
    dry_run: bool = False,
) -> PillarResult:
    """Dispatch the pre-pr-reviewer agent, parse `pillar-2-critique.md`."""
    _stage_pillar2_inputs(epic_id, branch, work_dir, merge_target, rig_path, report_dir)
    critique_path = report_dir / "pillar-2-critique.md"
    result = _agent_step_task(
        agent_dir=_agent_dir("pre-pr-reviewer"),
        task=_task_md("pre-pr-reviewer"),
        seed_id=seed_id,
        rig_path=str(rig_path),
        run_dir=report_dir,
        step="pre-pr-review",
        iter_n=iter_n,
        ctx={
            "branch": branch,
            "merge_target_branch": merge_target,
            "work_dir": str(work_dir),
        },
        verdict_keywords=("approved", "rejected"),
        dry_run=dry_run,
    )
    findings: list[tuple[str, str]] = []
    if critique_path.is_file():
        findings = _parse_pillar2_findings(critique_path.read_text())
    verdict = _VERDICT_PASSED if result.verdict == "approved" else _VERDICT_FAILED
    summary = result.summary or f"{len(findings)} finding(s)"
    return PillarResult(
        name="pillar-2",
        verdict=verdict,
        summary=summary,
        findings=findings,
        artifacts=[critique_path] if critique_path.is_file() else [],
    )


# ─────────────────────── pillar 3 ────────────────────────────────────


def _devup_supported(work_dir: Path) -> bool:
    proc = _run(["make", "-n", "dev-up"], cwd=work_dir)
    return proc.returncode == 0


def _run_pillar_3(
    work_dir: Path,
    rig_path: Path,
    report_dir: Path,
    *,
    seed_id: str,
    iter_n: int = 1,
    dry_run: bool = False,
) -> PillarResult:
    """Boot `make dev-up`, dispatch pre-pr-smoke-tester, tear down on any path."""
    if not _devup_supported(work_dir):
        prelude = report_dir / "pillar-3-prelude.md"
        prelude.write_text(
            "# Pillar 3 — Smoke Test\n\n"
            "**Verdict:** SKIPPED  \n"
            "`make -n dev-up` returned non-zero in this worktree; "
            "dev_env_bootable=false. Pillar 3 was not run.\n"
        )
        return PillarResult(
            name="pillar-3",
            verdict=_VERDICT_SKIPPED,
            summary="dev_env_bootable=false (make -n dev-up returned non-zero)",
            artifacts=[prelude],
        )

    proc = subprocess.Popen(  # noqa: S603
        ["make", "dev-up"],
        cwd=str(work_dir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        result = _agent_step_task(
            agent_dir=_agent_dir("pre-pr-smoke-tester"),
            task=_task_md("pre-pr-smoke-tester"),
            seed_id=seed_id,
            rig_path=str(rig_path),
            run_dir=report_dir,
            step="pre-pr-smoke",
            iter_n=iter_n,
            ctx={"work_dir": str(work_dir)},
            verdict_keywords=("approved", "rejected"),
            dry_run=dry_run,
        )
        verdict = _VERDICT_PASSED if result.verdict == "approved" else _VERDICT_FAILED
        summary = result.summary or "(no smoke summary captured)"
    finally:
        teardown_ok = _teardown_devenv(work_dir, proc)
        if not teardown_ok:
            (report_dir / "pillar-3-cleanup-failed.md").write_text(
                "# Pillar 3 — Cleanup Failed\n\n"
                "make dev-down (and pkill fallback) failed to stop the dev env. "
                "Pillar 3 cannot be marked PASSED.\n"
            )

    if not teardown_ok:
        verdict = _VERDICT_FAILED
        summary = f"{summary}; teardown failed"

    return PillarResult(name="pillar-3", verdict=verdict, summary=summary)


def _teardown_devenv(work_dir: Path, proc: subprocess.Popen[bytes]) -> bool:
    """Best-effort teardown. Returns True iff teardown probably succeeded."""
    log = _logger()
    dev_down_probe = _run(["make", "-n", "dev-down"], cwd=work_dir)
    if dev_down_probe.returncode == 0:
        td = _run(["make", "dev-down"], cwd=work_dir)
        if td.returncode == 0:
            return True
        log.warning("make dev-down failed rc=%d", td.returncode)
        return False
    if proc.poll() is None:
        if shutil.which("pkill"):
            _run(["pkill", "-P", str(proc.pid)])
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            return False
    return True


# ─────────────────────── filings + report ────────────────────────────


_BEAD_FANOUT_CAP = 20


def _file_findings_as_beads(
    findings: list[tuple[str, str, str]],
    epic_id: str | None,
    rig_path: Path,
    *,
    dry_run: bool = False,
) -> list[str]:
    """Create one bd bug per finding, capped at `_BEAD_FANOUT_CAP` + rollup.

    `findings` items are `(source_pillar, title, body)`.
    """
    if epic_id is None or dry_run:
        return []
    log = _logger()
    bead_ids: list[str] = []
    head = findings[:_BEAD_FANOUT_CAP]
    tail = findings[_BEAD_FANOUT_CAP:]
    for source, title, body in head:
        proc = _run(
            [
                "bd",
                "create",
                "--type=bug",
                "--priority=1",
                f"--parent={epic_id}",
                f"--title=[{source}] {title[:120]}",
                f"--description={body[:8000]}",
            ],
            cwd=rig_path,
        )
        if proc.returncode == 0:
            bead_ids.append(proc.stdout.strip())
        else:
            log.warning(
                "bd create for finding %r failed: %s", title, proc.stderr.strip()
            )
    if tail:
        rollup_body = "\n\n".join(f"## [{src}] {t}\n\n{b}" for src, t, b in tail)
        proc = _run(
            [
                "bd",
                "create",
                "--type=bug",
                "--priority=1",
                f"--parent={epic_id}",
                f"--title=pre-pr-review rollup ({len(tail)} additional findings)",
                f"--description={rollup_body[:16000]}",
            ],
            cwd=rig_path,
        )
        if proc.returncode == 0:
            bead_ids.append(proc.stdout.strip())
    return bead_ids


def _write_validation_report(
    report_dir: Path,
    p1: PillarResult,
    p2: PillarResult,
    p3: PillarResult,
    bead_ids: list[str],
    branch: str,
    merge_target: str,
) -> Path:
    """Deterministic H1 ordering: Pillar 1 / 2 / 3 / Summary."""
    lines: list[str] = [
        f"# Pre-PR Review: branch `{branch}` vs `origin/{merge_target}`",
        "",
        "# Pillar 1: Test Suite",
        "",
        f"**Verdict:** {p1.verdict}",
        "",
        p1.summary,
        "",
    ]
    if p1.findings:
        lines.append("## Regressions")
        lines.append("")
        for tgt, log in p1.findings:
            lines.append(f"### {tgt}")
            lines.append("")
            lines.append("```")
            lines.append(log[:2000])
            lines.append("```")
            lines.append("")

    lines += [
        "# Pillar 2: Cumulative-Diff Critic",
        "",
        f"**Verdict:** {p2.verdict}",
        "",
        p2.summary,
        "",
    ]
    if p2.findings:
        lines.append("## Findings")
        lines.append("")
        for i, (title, body) in enumerate(p2.findings, 1):
            lines.append(f"### Finding {i}: {title}")
            lines.append("")
            lines.append(body)
            lines.append("")

    lines += [
        "# Pillar 3: Smoke Test",
        "",
        f"**Verdict:** {p3.verdict}",
        "",
        p3.summary,
        "",
        "# Summary",
        "",
        f"- pillar-1: {p1.verdict}",
        f"- pillar-2: {p2.verdict}",
        f"- pillar-3: {p3.verdict}",
    ]
    if bead_ids:
        lines.append("")
        lines.append(f"Filed {len(bead_ids)} bd bug(s):")
        for b in bead_ids:
            lines.append(f"- {b}")
    report_path = report_dir / "validation-report.md"
    report_path.write_text("\n".join(lines) + "\n")
    return report_path


# ─────────────────────── the flow ────────────────────────────────────


@flow(name="pre_pr_review_wts", flow_run_name="{epic_id}{branch}", log_prints=True)
def pre_pr_review(
    epic_id: str | None = None,
    branch: str | None = None,
    rig_path: str = ".",
    pack_path: str | None = None,
    merge_target_branch: str = "main",
    iter_cap: int = 2,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Cumulative pre-PR review run. See module docstring."""
    log = _logger()
    if (epic_id is None) == (branch is None):
        raise ValueError("exactly one of --epic-id / --branch is required")

    rig_path_p = Path(rig_path).expanduser().resolve()
    work_dir, branch_name, merge_target, prelude_warnings = _resolve_worktree(
        epic_id, branch, rig_path_p, merge_target_branch
    )

    sanitized_branch = branch_name.replace("/", "_")
    seed_id = epic_id or f"branch-{sanitized_branch}"
    report_dir = rig_path_p / ".planning" / "pre-pr-review" / sanitized_branch
    report_dir.mkdir(parents=True, exist_ok=True)

    # Plan §N1: when an epic was explicitly given but its worktree is
    # unresolvable AND `metadata.no_worktree` wasn't set, refuse the
    # silent rig-root fallback — all three pillars SKIP and the epic
    # is stamped validation=blocked. _resolve_worktree only emits the
    # "has no metadata.work_dir" warning on this exact path; no_worktree=true
    # produces a different warning that does not trip this guard.
    block_run = bool(
        epic_id and any("has no metadata.work_dir" in w for w in prelude_warnings)
    )

    if block_run:
        prelude = report_dir / "pillar-0-prelude.md"
        prelude.write_text(
            "# Pillar 0 — Prelude\n\n"
            "**Verdict:** BLOCKED  \n\n"
            + "\n".join(f"- {w}" for w in prelude_warnings)
            + "\n\nAll three pillars SKIPPED.\n"
        )
        skip = lambda name: PillarResult(  # noqa: E731
            name=name,
            verdict=_VERDICT_SKIPPED,
            summary="missing-worktree blocked the run",
        )
        p1 = skip("pillar-1")
        p2 = skip("pillar-2")
        p3 = skip("pillar-3")
        report_path = _write_validation_report(
            report_dir, p1, p2, p3, [], branch_name, merge_target
        )
        if epic_id and not dry_run:
            _run(
                ["bd", "update", epic_id, "--set-metadata", "validation=blocked"],
                cwd=rig_path_p,
            )
        return {
            "branch": branch_name,
            "report_path": str(report_path),
            "validation": "blocked",
            "pillars": {
                "pillar-1": p1.verdict,
                "pillar-2": p2.verdict,
                "pillar-3": p3.verdict,
            },
            "bead_ids": [],
        }

    log.info(
        "pre-pr-review: branch=%s work_dir=%s merge_target=%s",
        branch_name,
        work_dir,
        merge_target,
    )

    p1 = _run_pillar_1(work_dir, merge_target, report_dir)
    p2 = _run_pillar_2(
        epic_id,
        branch_name,
        work_dir,
        rig_path_p,
        merge_target,
        report_dir,
        seed_id=seed_id,
        iter_n=1,
        dry_run=dry_run,
    )
    p3 = _run_pillar_3(
        work_dir,
        rig_path_p,
        report_dir,
        seed_id=seed_id,
        iter_n=1,
        dry_run=dry_run,
    )

    findings: list[tuple[str, str, str]] = []
    for tgt, body in p1.findings:
        findings.append(("pillar-1", tgt, body))
    for title, body in p2.findings:
        findings.append(("pillar-2", title, body))
    bead_ids = _file_findings_as_beads(findings, epic_id, rig_path_p, dry_run=dry_run)

    report_path = _write_validation_report(
        report_dir, p1, p2, p3, bead_ids, branch_name, merge_target
    )

    validation = (
        "passed"
        if all(p.verdict == _VERDICT_PASSED for p in (p1, p2, p3))
        else "blocked"
    )
    if epic_id and not dry_run:
        _run(
            ["bd", "update", epic_id, "--set-metadata", f"validation={validation}"],
            cwd=rig_path_p,
        )

    return {
        "branch": branch_name,
        "report_path": str(report_path),
        "validation": validation,
        "pillars": {
            "pillar-1": p1.verdict,
            "pillar-2": p2.verdict,
            "pillar-3": p3.verdict,
        },
        "bead_ids": bead_ids,
    }
