"""Prefect flow: `epic_finalize`.

End-of-epic batch verification. Runs once after all epic children are closed.
Steps (in order):
  1. Defensive check — log warning if any children are still open.
  2. Run `make test-unit` + `make test-e2e` via subprocess.
  3. Run `make lint` via subprocess.
  4. Run optional smoke_cmd via subprocess.
  5. Docs update via agent_step (documenter role).
  6. Write post-flight artifact to .planning/epics/<epic_id>/post-flight.md.
  7. Close the epic if all make steps passed (rc==0).
"""

from __future__ import annotations

import datetime
import subprocess
from pathlib import Path
from typing import Any

from prefect import flow, get_run_logger

from prefect_orchestration.beads_meta import close_issue, list_epic_children

from po_formulas.software_dev import _agent_dir, _agent_step_task, _task_md


def _run_make(target: str, cwd: Path) -> tuple[int, str]:
    """Run `make <target>` in cwd; return (returncode, combined output)."""
    result = subprocess.run(
        ["make", target],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    return result.returncode, (result.stdout + result.stderr).strip()


@flow(name="epic_finalize", flow_run_name="{epic_id}", log_prints=True)
def epic_finalize(
    epic_id: str,
    rig: str,
    rig_path: str,
    smoke_cmd: str | None = None,
    dry_run: bool = False,
    claim: bool = True,
) -> dict[str, Any]:
    """End-of-epic batch verification formula."""
    logger = get_run_logger()
    rig_path_p = Path(rig_path).expanduser().resolve()

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

    # 2. Full test suite.
    if not dry_run:
        for target in ("test-unit", "test-e2e"):
            rc, out = _run_make(target, rig_path_p)
            logger.info("make %s: rc=%d\n%s", target, rc, out[:2000])
            if rc != 0:
                failures.append(f"make {target} failed (rc={rc})")

    # 3. Lint.
    if not dry_run:
        rc, out = _run_make("lint", rig_path_p)
        logger.info("make lint: rc=%d\n%s", rc, out[:2000])
        if rc != 0:
            failures.append(f"make lint failed (rc={rc})")

    # 4. Optional smoke command.
    if smoke_cmd and not dry_run:
        result = subprocess.run(
            smoke_cmd, shell=True, cwd=str(rig_path_p), capture_output=True, text=True
        )
        logger.info("smoke_cmd rc=%d\n%s", result.returncode, result.stdout[:2000])
        if result.returncode != 0:
            failures.append(f"smoke_cmd failed (rc={result.returncode})")

    # 5. Docs update via agent (needs judgment — uses documenter role).
    if not dry_run:
        _agent_step_task(
            agent_dir=_agent_dir("documenter"),
            task=_task_md("documenter"),
            seed_id=epic_id,
            rig_path=str(rig_path_p),
            step="docs",
            iter_n=1,
            ctx={"epic_id": epic_id, "failures": failures},
            dry_run=False,
        )

    # 6. Write post-flight artifact.
    epics_dir = rig_path_p / ".planning" / "epics" / epic_id
    epics_dir.mkdir(parents=True, exist_ok=True)
    post_flight = epics_dir / "post-flight.md"
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
    post_flight.write_text("\n".join(lines) + "\n")
    logger.info("post-flight artifact: %s", post_flight)

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
        "post_flight_path": str(post_flight),
    }
