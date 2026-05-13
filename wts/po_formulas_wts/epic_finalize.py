"""Prefect flow: `epic_finalize`.

End-of-epic batch verification. Runs once after all epic children are closed.
Steps (in order):
  1. Defensive check — log warning if any children are still open.
  2. Spec-audit via agent_step (spec-auditor role). Runs before the
     mechanical checks so a spec gap is reported even if tests pass.
  3. Run `make test-unit` + `make test-e2e` via subprocess.
  4. Run `make lint` via subprocess.
  5. Real-env smoke walkthrough (structured gate). Invokes the rig's
     Playwright walkthrough, parses `report.md` Verdict, stamps
     `metadata.smoke = passed/failed/skipped` on the epic, writes
     `verdicts/smoke.json`. Aborts finalize on FAIL.
  6. Optional smoke_cmd (legacy shell-blob) via subprocess.
  7. Demo video — ONE per epic, invoked against the smoke artifacts.
     Lands at `research/<branch-slug>/demo-<utc>/demo_final.mp4`.
  8. Remote-CI gate — push the branch, poll `gh pr checks`, stamp
     `metadata.ci = passed/failed/timeout`. Aborts on failed CI.
  9. Docs update via agent_step (documenter role).
 10. Write post-flight artifact to .planning/epics/<epic_id>/post-flight.md.
 11. Close the epic if all checks passed (rc==0 AND spec-audit PASSED).
"""

from __future__ import annotations

import datetime
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from prefect import flow, get_run_logger

from prefect_orchestration.beads_meta import close_issue, list_epic_children
from prefect_orchestration.parsing import read_verdict

from po_formulas_wts.software_dev import _agent_dir, _agent_step_task, _task_md
from po_formulas_wts.worktree import merge_worktree

# Default rig-relative path to the smoke walkthrough script. Override via
# the `walkthrough_script` param to epic_finalize. Repos that don't ship a
# walkthrough at this default leave the smoke gate skipped (not failed).
DEFAULT_WALKTHROUGH_SCRIPT = "scripts/smoke-move-ub/ui-walkthrough.py"

# Verdict regex over walkthrough report.md. The harness emits
# `- **Verdict: \`PASS\`**` on success.
_VERDICT_RE = re.compile(r"\*\*Verdict:\s*`?(PASS|FAIL|UNKNOWN)`?\*\*", re.IGNORECASE)

# Remote-CI gate defaults.
DEFAULT_CI_TIMEOUT_S = 30 * 60  # 30 min
CI_POLL_INTERVAL_S = 20


def _stamp_metadata(epic_id: str, key: str, value: str, rig_path: Path) -> None:
    """Best-effort `bd update --set-metadata key=value`. Logs on failure
    but doesn't raise — metadata stamping shouldn't break finalize."""
    try:
        subprocess.run(
            ["bd", "update", epic_id, "--set-metadata", f"{key}={value}"],
            cwd=str(rig_path),
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        pass


def _bd_metadata(bead_id: str, rig_path: Path) -> dict[str, Any]:
    proc = subprocess.run(
        ["bd", "show", bead_id, "--json"],
        cwd=str(rig_path),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return {}
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}
    row = data[0] if isinstance(data, list) and data else data
    if not isinstance(row, dict):
        return {}
    metadata = row.get("metadata") or {}
    return metadata if isinstance(metadata, dict) else {}


def _smoke_has_ui_evidence(smoke_out_dir: Path) -> bool:
    """Heuristic: the smoke walkthrough produced UI screenshots iff its
    `evidence/` dir contains any `.png`. Backend-only smokes (curl loops,
    pytest harnesses) don't drop PNGs; skip demo-video in that case."""
    evidence_dir = smoke_out_dir / "evidence"
    if not evidence_dir.is_dir():
        return False
    return any(evidence_dir.glob("*.png"))


def _current_branch(rig_path: Path) -> str | None:
    """`git rev-parse --abbrev-ref HEAD` from the rig, or None on failure."""
    proc = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(rig_path), capture_output=True, text=True, check=False,
    )
    branch = (proc.stdout or "").strip()
    return branch or None


def _run_remote_ci_gate(
    rig_path: Path,
    branch: str,
    timeout_s: int,
) -> dict[str, Any]:
    """Push the branch, locate / open the PR, poll `gh pr checks` until
    terminal. Returns {"verdict": "passed|failed|timeout|skipped",
    "pr": int|None, "summary": str, "tail": str}.

    Verdict "skipped" when there's no remote, no PR can be opened
    (e.g. local-only repo), or gh is missing.
    """
    result: dict[str, Any] = {
        "verdict": "skipped", "pr": None, "summary": "", "tail": "",
    }

    push = subprocess.run(
        ["git", "push", "origin", branch],
        cwd=str(rig_path), capture_output=True, text=True, check=False,
    )
    push_out = (push.stdout or "") + (push.stderr or "")
    result["tail"] = push_out[-1000:]
    if push.returncode != 0:
        result["verdict"] = "failed"
        result["summary"] = f"git push failed (rc={push.returncode})"
        return result

    pr_view = subprocess.run(
        ["gh", "pr", "view", branch, "--json", "number,state,url"],
        cwd=str(rig_path), capture_output=True, text=True, check=False,
    )
    if pr_view.returncode != 0:
        # No PR for this branch — open a draft.
        create = subprocess.run(
            [
                "gh", "pr", "create", "--draft",
                "--head", branch, "--fill",
            ],
            cwd=str(rig_path), capture_output=True, text=True, check=False,
        )
        if create.returncode != 0:
            result["verdict"] = "skipped"
            result["summary"] = (
                f"no PR for {branch} and `gh pr create --draft` failed "
                f"(rc={create.returncode}); skipping CI gate"
            )
            result["tail"] = (create.stdout + create.stderr)[-1000:]
            return result
        pr_view = subprocess.run(
            ["gh", "pr", "view", branch, "--json", "number,state,url"],
            cwd=str(rig_path), capture_output=True, text=True, check=False,
        )

    try:
        pr_info = json.loads(pr_view.stdout or "{}")
    except json.JSONDecodeError:
        pr_info = {}
    pr_number = pr_info.get("number")
    result["pr"] = pr_number
    if not pr_number:
        result["verdict"] = "skipped"
        result["summary"] = "couldn't resolve PR number after push"
        return result

    deadline = time.monotonic() + timeout_s
    last_tail = ""
    while time.monotonic() < deadline:
        checks = subprocess.run(
            ["gh", "pr", "checks", str(pr_number), "--json", "bucket,name,state"],
            cwd=str(rig_path), capture_output=True, text=True, check=False,
        )
        last_tail = (checks.stdout + checks.stderr)[-1500:]
        try:
            rows = json.loads(checks.stdout or "[]")
        except json.JSONDecodeError:
            rows = []
        if rows:
            buckets = {r.get("bucket") for r in rows}
            if "pending" not in buckets:
                if "fail" in buckets:
                    failed = [r["name"] for r in rows if r.get("bucket") == "fail"]
                    result["verdict"] = "failed"
                    result["summary"] = f"PR #{pr_number}: failed checks: {failed}"
                    result["tail"] = last_tail
                    return result
                if buckets <= {"pass", "skipping"}:
                    result["verdict"] = "passed"
                    result["summary"] = f"PR #{pr_number}: all checks green"
                    result["tail"] = last_tail
                    return result
        time.sleep(CI_POLL_INTERVAL_S)

    result["verdict"] = "timeout"
    result["summary"] = (
        f"PR #{pr_number}: timed out after {timeout_s}s waiting for terminal state"
    )
    result["tail"] = last_tail
    return result


def _discover_spec_path(rig_path: Path) -> Path | None:
    """Look for a spec.md next to the rig (dogfood convention: rig is
    `<project>/target/` and spec is `<project>/spec.md`)."""
    candidate = rig_path.parent / "spec.md"
    return candidate if candidate.is_file() else None


def _run_make(target: str, cwd: Path) -> tuple[int, str]:
    """Run `make <target>` in cwd; return (returncode, combined output)."""
    result = subprocess.run(
        ["make", target],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    return result.returncode, (result.stdout + result.stderr).strip()


def _run_smoke_walkthrough(
    rig_path: Path,
    walkthrough_script: str,
    run_dir: Path,
) -> dict[str, Any]:
    """Invoke the rig's Playwright smoke walkthrough and parse its
    `report.md` verdict.

    The walkthrough script must accept `--out <dir>` and emit `report.md`
    with a `**Verdict: \\`PASS|FAIL|UNKNOWN\\`**` line. Repos in this
    family use `scripts/smoke-move-ub/ui-walkthrough.py` (renamed via
    the `walkthrough_script` param).

    Returns: {"verdict": "PASS|FAIL|SKIPPED|UNKNOWN", "out_dir": str,
              "report_path": str, "stdout_tail": str, "summary": str}.
    Verdict "SKIPPED" when the script doesn't exist at the rig.
    """
    script_path = (rig_path / walkthrough_script).resolve()
    out_dir = run_dir / "smoke-walkthrough"
    out_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {
        "verdict": "UNKNOWN",
        "out_dir": str(out_dir),
        "report_path": str(out_dir / "report.md"),
        "stdout_tail": "",
        "summary": "",
    }
    if not script_path.is_file():
        result["verdict"] = "SKIPPED"
        result["summary"] = f"walkthrough script not found at {script_path}"
        return result

    # `uv run --with playwright` keeps the dep declaration out of the rig's
    # pyproject. If the rig itself owns playwright (e.g. a tests/ venv),
    # `--with` is a no-op cost; still cheap.
    cmd = [
        "uv", "run", "--with", "playwright",
        "python", str(script_path),
        "--out", str(out_dir),
    ]
    proc = subprocess.run(
        cmd, cwd=str(rig_path), capture_output=True, text=True, check=False
    )
    stdout = (proc.stdout or "") + (proc.stderr or "")
    result["stdout_tail"] = stdout[-2000:]

    report_path = out_dir / "report.md"
    if report_path.is_file():
        match = _VERDICT_RE.search(report_path.read_text())
        result["verdict"] = match.group(1).upper() if match else "UNKNOWN"
    elif proc.returncode != 0:
        result["verdict"] = "FAIL"
        result["summary"] = f"walkthrough exited {proc.returncode}; no report.md"
    else:
        result["verdict"] = "UNKNOWN"
        result["summary"] = "walkthrough exited 0 but produced no report.md"
    if not result["summary"]:
        result["summary"] = f"walkthrough exited rc={proc.returncode}, verdict={result['verdict']}"
    return result


@flow(name="epic_finalize_wts", flow_run_name="{epic_id}", log_prints=True)
def epic_finalize(
    epic_id: str,
    rig: str,
    rig_path: str,
    spec_path: str | None = None,
    smoke_cmd: str | None = None,
    walkthrough_script: str | None = None,
    skip_walkthrough: bool = False,
    skip_demo_video: bool = False,
    skip_remote_ci: bool = False,
    ci_timeout_s: int = DEFAULT_CI_TIMEOUT_S,
    dry_run: bool = False,
    claim: bool = True,
    worktree_path: str | None = None,
    branch: str | None = None,
    merge_target_branch: str = "main",
) -> dict[str, Any]:
    """End-of-epic batch verification formula.

    Args:
        epic_id: bd issue id of the epic.
        rig: rig name (display only).
        rig_path: absolute path to the rig root.
        spec_path: optional path to a spec.md file the implementation
            must match. Defaults to auto-discovery at `<rig>/../spec.md`
            (the dogfood convention). Pass an empty string to skip the
            spec-audit step entirely.
        smoke_cmd: optional shell command to run as a final smoke test
            (legacy free-form blob; structured smoke gate via
            `walkthrough_script` is preferred for new rigs).
        walkthrough_script: rig-relative path to a Playwright walkthrough
            script accepting `--out <dir>` and emitting `report.md` with
            `**Verdict: PASS|FAIL|UNKNOWN**`. Default
            `scripts/smoke-move-ub/ui-walkthrough.py`. Pass an empty
            string to skip the structured walkthrough.
        skip_walkthrough: hard-skip the smoke walkthrough gate (overrides
            walkthrough_script). Useful for backend-only epics where
            running Playwright would just produce SKIPPED noise.
        skip_demo_video: hard-skip the demo-video step. Default false;
            the step also auto-skips when the smoke walkthrough produced
            no PNG artifacts (no UI to demo).
        skip_remote_ci: hard-skip the remote-CI gate. Useful for
            local-only / no-remote rigs.
        ci_timeout_s: max seconds to wait for remote CI to reach a
            terminal state. Default 30 min.
        dry_run: skip all subprocess work; useful for shape tests.
        claim: reserved.
    """
    logger = get_run_logger()
    rig_path_p = Path(rig_path).expanduser().resolve()
    run_dir = rig_path_p / ".planning" / "epics" / epic_id
    run_dir.mkdir(parents=True, exist_ok=True)
    metadata = _bd_metadata(epic_id, rig_path_p)
    worktree_path = worktree_path or str(metadata.get("work_dir") or "")
    repo_path = (
        Path(worktree_path).expanduser().resolve()
        if worktree_path
        else rig_path_p
    )
    branch_name = branch or str(metadata.get("branch") or "") or _current_branch(repo_path)
    merge_target = (
        str(metadata.get("merge_target_branch") or "").strip()
        or merge_target_branch
    )

    # 1. Defensive check — all children should be closed by bead-dep blocking,
    #    but log if something slipped through.
    open_children = list_epic_children(epic_id, mode="both", rig_path=rig_path_p)
    if open_children:
        logger.warning(
            "epic-finalize: %d children still open/in-progress: %s",
            len(open_children),
            [c["id"] for c in open_children],
        )

    failures: list[str] = []
    spec_gaps: list[str] = []

    # 2. Spec-audit (agent step). Resolves spec_path or auto-discovers.
    if spec_path == "":
        resolved_spec: Path | None = None
        logger.info("spec-audit: explicitly skipped (spec_path='')")
    elif spec_path is not None:
        resolved_spec = Path(spec_path).expanduser().resolve()
        if not resolved_spec.is_file():
            logger.warning("spec-audit: spec_path=%s not found", resolved_spec)
            resolved_spec = None
    else:
        resolved_spec = _discover_spec_path(repo_path)
        if resolved_spec is None:
            logger.info(
                "spec-audit: no spec.md found at %s — skipping",
                repo_path.parent / "spec.md",
            )

    if resolved_spec is not None and not dry_run:
        logger.info("spec-audit: auditing against %s", resolved_spec)
        _agent_step_task(
            agent_dir=_agent_dir("spec-auditor"),
            task=_task_md("spec-auditor"),
            seed_id=epic_id,
            rig_path=str(repo_path),
            run_dir=run_dir,
            step="spec-audit",
            iter_n=1,
            ctx={
                "epic_id": epic_id,
                "spec_path": str(resolved_spec),
                "rig_path": str(repo_path),
                "run_dir": str(run_dir),
            },
            dry_run=False,
        )
        verdict_path = run_dir / "verdicts" / "spec-audit.json"
        try:
            verdict = read_verdict(run_dir, "spec-audit")
        except Exception as exc:
            logger.warning("spec-audit: failed to read verdict at %s: %s", verdict_path, exc)
            verdict = {}
        if verdict.get("verdict") == "FAILED":
            spec_gaps = list(verdict.get("gaps") or [])
            failures.append(
                f"spec-audit FAILED — {len(spec_gaps)} gap(s): {json.dumps(spec_gaps)}"
            )
            logger.warning("spec-audit FAILED — %d gap(s)", len(spec_gaps))
        elif verdict.get("verdict") == "PASSED":
            logger.info("spec-audit PASSED")
        else:
            logger.warning(
                "spec-audit: unexpected verdict shape %r; treating as inconclusive",
                verdict,
            )

    # 3. Full test suite.
    if not dry_run:
        for target in ("test-unit", "test-e2e"):
            rc, out = _run_make(target, repo_path)
            logger.info("make %s: rc=%d\n%s", target, rc, out[:2000])
            if rc != 0:
                failures.append(f"make {target} failed (rc={rc})")

    # 3. Lint.
    if not dry_run:
        rc, out = _run_make("lint", repo_path)
        logger.info("make lint: rc=%d\n%s", rc, out[:2000])
        if rc != 0:
            failures.append(f"make lint failed (rc={rc})")

    # 4. Real-env smoke walkthrough (structured gate).
    smoke_result: dict[str, Any] = {"verdict": "SKIPPED"}
    if not skip_walkthrough and walkthrough_script != "" and not dry_run:
        smoke_result = _run_smoke_walkthrough(
            repo_path,
            walkthrough_script or DEFAULT_WALKTHROUGH_SCRIPT,
            run_dir,
        )
        logger.info(
            "smoke-walkthrough: verdict=%s out=%s",
            smoke_result["verdict"],
            smoke_result["out_dir"],
        )
        (run_dir / "verdicts").mkdir(parents=True, exist_ok=True)
        (run_dir / "verdicts" / "smoke.json").write_text(
            json.dumps(smoke_result, indent=2) + "\n"
        )
        verdict_lower = smoke_result["verdict"].lower()
        _stamp_metadata(epic_id, "smoke", verdict_lower, rig_path_p)
        if smoke_result["verdict"] == "FAIL":
            failures.append(
                f"smoke-walkthrough FAIL ({smoke_result.get('summary', '')})"
            )

    # 5a. Demo video — ONE per epic, against the smoke walkthrough's
    #     PNG evidence. Skipped when (a) hard-skip flag, (b) smoke ran
    #     but produced no PNGs (backend-only), (c) smoke was skipped.
    demo_video_path: str | None = None
    if not skip_demo_video and not dry_run:
        smoke_out_dir = Path(smoke_result.get("out_dir", ""))
        has_ui = smoke_result.get("verdict") in {"PASS", "FAIL"} and (
            smoke_out_dir.is_dir() and _smoke_has_ui_evidence(smoke_out_dir)
        )
        if has_ui:
            _agent_step_task(
                agent_dir=_agent_dir("demo-video"),
                task=_task_md("demo-video"),
                seed_id=epic_id,
                rig_path=str(repo_path),
                run_dir=run_dir,
                step="demo-video",
                iter_n=1,
                ctx={
                    "epic_id": epic_id,
                    "smoke_out_dir": str(smoke_out_dir),
                    "smoke_report": smoke_result.get("report_path"),
                    "rig_path": str(repo_path),
                    "run_dir": str(run_dir),
                },
                dry_run=False,
            )
            # Discover the produced mp4: agent writes to
            # research/<branch-slug>/demo-<utc>/demo_final.mp4 per the
            # demo-video skill convention.
            demo_branch = branch_name or _current_branch(repo_path) or "unknown-branch"
            branch_slug = demo_branch.replace("/", "-")
            candidates = sorted(
                (repo_path / "research" / branch_slug).glob("demo-*/demo_final.mp4"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if candidates:
                demo_video_path = str(candidates[0])
                logger.info("demo-video: produced %s", demo_video_path)
            else:
                logger.warning("demo-video: agent ran but no demo_final.mp4 found")
        else:
            logger.info(
                "demo-video: skipped (smoke verdict=%s, has_ui=%s)",
                smoke_result.get("verdict"), has_ui,
            )

    # 5b. Optional legacy smoke command (free-form shell blob).
    if smoke_cmd and not dry_run:
        result = subprocess.run(
            smoke_cmd, shell=True, cwd=str(repo_path), capture_output=True, text=True
        )
        logger.info("smoke_cmd rc=%d\n%s", result.returncode, result.stdout[:2000])
        if result.returncode != 0:
            failures.append(f"smoke_cmd failed (rc={result.returncode})")

    # 5c. Remote-CI gate — push + wait for `gh pr checks` to clear.
    #     Runs only if local gates green so far (avoids burning CI on
    #     work we already know is broken).
    ci_result: dict[str, Any] = {"verdict": "skipped", "summary": "not run"}
    if not skip_remote_ci and not failures and not dry_run:
        if branch_name:
            ci_result = _run_remote_ci_gate(repo_path, branch_name, ci_timeout_s)
            logger.info(
                "remote-ci: pr=%s verdict=%s %s",
                ci_result.get("pr"),
                ci_result["verdict"],
                ci_result.get("summary", ""),
            )
            (run_dir / "verdicts").mkdir(parents=True, exist_ok=True)
            (run_dir / "verdicts" / "ci.json").write_text(
                json.dumps(ci_result, indent=2) + "\n"
            )
            _stamp_metadata(epic_id, "ci", ci_result["verdict"], rig_path_p)
            if ci_result["verdict"] in {"failed", "timeout"}:
                failures.append(
                    f"remote-ci {ci_result['verdict']}: {ci_result.get('summary', '')}"
                )
        else:
            logger.warning("remote-ci: couldn't resolve current branch; skipping gate")
    elif skip_remote_ci:
        ci_result = {"verdict": "skipped", "summary": "skip_remote_ci=True"}
    elif failures:
        ci_result = {
            "verdict": "skipped",
            "summary": f"local gates failed ({len(failures)}); not pushing",
        }

    # 6. Docs update via agent (needs judgment — uses documenter role).
    #    Pass run_dir explicitly so build_context_md doesn't try to write
    #    into the default `.planning/software-dev-full/<seed>/` path (which
    #    doesn't exist for an epic that never went through software-dev-full).
    if not dry_run:
        _agent_step_task(
            agent_dir=_agent_dir("documenter"),
            task=_task_md("documenter"),
            seed_id=epic_id,
            rig_path=str(repo_path),
            run_dir=run_dir,
            step="docs",
            iter_n=1,
            ctx={"epic_id": epic_id, "failures": failures, "run_dir": str(run_dir)},
            dry_run=False,
        )

    # 7. Write post-flight artifact.
    post_flight = run_dir / "post-flight.md"
    status = "PASSED" if not failures else "FAILED"
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    lines = [
        f"# Post-flight: {epic_id}",
        "",
        f"Generated: {ts}  Status: **{status}**",
        "",
        "## Results",
        "",
    ]
    if failures:
        for f in failures:
            lines.append(f"- FAIL: {f}")
    else:
        lines.append("- All checks passed.")
    if resolved_spec is not None:
        lines.extend([
            "",
            f"Spec audited against: `{resolved_spec}`",
            f"See `{run_dir / 'spec-audit.md'}` for the auditor's findings.",
        ])
    lines.extend([
        "",
        "## Gates",
        "",
        f"- smoke walkthrough: **{smoke_result.get('verdict', 'UNKNOWN')}**"
        + (f" — `{smoke_result.get('out_dir')}`" if smoke_result.get("out_dir") else ""),
        f"- demo video: " + (f"`{demo_video_path}`" if demo_video_path else "_not produced_"),
        f"- remote CI: **{ci_result.get('verdict', 'skipped')}**"
        + (f" (PR #{ci_result.get('pr')})" if ci_result.get("pr") else "")
        + (f" — {ci_result.get('summary')}" if ci_result.get("summary") else ""),
    ])
    post_flight.write_text("\n".join(lines) + "\n")
    logger.info("post-flight artifact: %s", post_flight)

    merged_into: str | None = None
    if not failures and not dry_run and worktree_path:
        merged_into = merge_worktree(
            rig_path_p,
            epic_id,
            target_branch=merge_target,
            cleanup=True,
            for_epic=True,
        )
        logger.info("epic-finalize: merged %s into %s", branch_name, merged_into)

    # 7. Close epic if all passed.
    if not failures and not dry_run:
        close_issue(
            epic_id,
            notes=f"epic-finalize: all checks passed ({ts})",
            rig_path=rig_path_p,
        )
        logger.info("epic %s closed", epic_id)
    elif failures:
        logger.warning(
            "epic %s NOT closed — %d failure(s): %s", epic_id, len(failures), failures
        )

    return {
        "epic_id": epic_id,
        "status": status,
        "failures": failures,
        "spec_gaps": spec_gaps,
        "spec_path": str(resolved_spec) if resolved_spec else None,
        "post_flight_path": str(post_flight),
        "smoke": smoke_result,
        "demo_video_path": demo_video_path,
        "ci": ci_result,
        "merged_into": merged_into,
        "worktree_path": str(repo_path),
        "branch": branch_name,
        "merge_target_branch": merge_target,
    }
