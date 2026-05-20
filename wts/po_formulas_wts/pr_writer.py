"""Prefect flow: `pr_writer_wts`.

Drafts + publishes a PR (or fast-forward direct-push) for a single bead
or an epic's aggregated children. Designed to run as the final step of
`epic_wts` after pre-pr-review + epic-finalize have left clean gate
verdicts on the epic bead's metadata (`po.smoke`, `po.ci`,
`po.demo_video`, `po.spec_audit`).

Source assets (ported from `software-dev-pack-wts/`, the gas-city pack):

- `agents/pr-writer/prompt.md`           — role description (verbatim copy)
- `agents/pr-writer/task.md`             — scoped task with `{{...}}` vars
- `agents/pr-writer/pr-format-template.md` — baked copy of
  `~/.claude/commands/pr-format.md` so the pack ships its own template
  rather than relying on user-global skill files (per
  prefect-orchestration-3pt acceptance #3).

Gas-city features that don't translate (and don't need to):

- `wake_mode = fresh` / `work_query` polling: Prefect runs the flow
  one-shot per invocation. The agent reads the verdicts and writes a
  body; the flow returns its verdict file.
- `max_active_sessions = 2`: Prefect concurrency-limit on the
  `pr-writer` task tag covers the same intent.

Idempotency: the agent's `gh pr edit` vs `gh pr create` branch handles
the "PR already exists" case (see prompt.md § Idempotency state machine).
The flow itself is idempotent because re-runs hit agent_step's cache
fast-path on the closed iter bead and return the verdict from bd
metadata.

Refs: prefect-orchestration-3pt.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from prefect import flow, get_run_logger

from prefect_orchestration.parsing import read_bead_verdict

from po_formulas_wts.software_dev import _agent_dir, _agent_step_task, _task_md


def _resolve_run_dir(rig_path: Path, scope_id: str) -> Path:
    """Find the run_dir for the scope (epic or bead). Epics live at
    `<rig>/.planning/epics/<id>/`; beads at
    `<rig>/.planning/software-dev-*/<id>/`. Returns the epic dir when
    present, falls through to the first software-dev-* match, else
    creates a fresh pr-writer-scoped dir."""
    epic_dir = rig_path / ".planning" / "epics" / scope_id
    if epic_dir.is_dir():
        return epic_dir
    planning = rig_path / ".planning"
    if planning.is_dir():
        for sd in sorted(planning.glob("software-dev-*")):
            cand = sd / scope_id
            if cand.is_dir():
                return cand
    fallback = rig_path / ".planning" / "pr-writer" / scope_id
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


@flow(name="pr_writer_wts", flow_run_name="{epic_id}{bead_id}", log_prints=True)
def pr_writer(
    bead_id: str | None = None,
    epic_id: str | None = None,
    rig: str = "",
    rig_path: str = "",
    branch: str | None = None,
    merge_target: str = "main",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Compose + dispatch a PR for the given scope.

    Args:
        bead_id: single-bead scope. Mutually exclusive with epic_id.
        epic_id: epic scope (walks parent-child children).
        rig: rig name (display only).
        rig_path: absolute path to the rig root.
        branch: branch to PR. Defaults to the bead/epic's
            `metadata.branch`; the agent resolves this from bd-show.
        merge_target: base branch for the PR (default `main`).
        dry_run: skip the agent invocation and `gh` dispatch.

    Returns: {"verdict": "PASS|HALT", "pr": int|None, "url": str|None,
              "branch": str|None, "mode": "create|edit|none",
              "scope_id": str, "bead_id": str}.
    """
    logger = get_run_logger()
    if (bead_id is None) == (epic_id is None):
        return {
            "verdict": "HALT",
            "reason": "exactly one of bead_id / epic_id required",
            "pr": None, "url": None, "branch": branch, "mode": "none",
            "scope_id": "", "bead_id": "",
        }
    if not rig_path:
        return {
            "verdict": "HALT",
            "reason": "rig_path required",
            "pr": None, "url": None, "branch": branch, "mode": "none",
            "scope_id": "", "bead_id": "",
        }

    scope_id: str = bead_id or epic_id  # type: ignore[assignment]
    rig_path_p = Path(rig_path).expanduser().resolve()
    run_dir = _resolve_run_dir(rig_path_p, scope_id)

    logger.info(
        "pr_writer: scope=%s rig=%s run_dir=%s merge_target=%s",
        scope_id, rig, run_dir, merge_target,
    )

    iter_bead_id = f"{scope_id}.pr-writer.iter1"

    if dry_run:
        stub = {
            "verdict": "PASS",
            "pr": None,
            "url": None,
            "branch": branch,
            "mode": "none",
            "scope_id": scope_id,
            "bead_id": iter_bead_id,
            "dry_run": True,
        }
        # Stamp the stub onto the iter bead so downstream consumers see
        # the same shape they would in a real run.
        subprocess.run(
            ["bd", "update", iter_bead_id, "--metadata",
             json.dumps({"po.pr_writer": stub})],
            cwd=str(rig_path_p),
            capture_output=True,
            text=True,
            check=False,
        )
        return stub

    # Hand control to the pr-writer agent. The agent reads upstream
    # verdicts from the epic's bd metadata + diff + child summaries,
    # composes a PR body against pr-format-template.md (baked alongside
    # prompt.md), and dispatches via gh. It stamps `po.pr_writer` onto
    # its iter bead on exit.
    result = _agent_step_task(
        agent_dir=_agent_dir("pr-writer"),
        task=_task_md("pr-writer"),
        seed_id=scope_id,
        rig_path=str(rig_path_p),
        run_dir=run_dir,
        step="pr-writer",
        iter_n=1,
        ctx={
            "issue_id": scope_id,
            "epic_id": epic_id or "",
            "bead_id": bead_id or "",
            "rig_path": str(rig_path_p),
            "run_dir": str(run_dir),
            "merge_target": merge_target,
            "branch": branch or "",
        },
        dry_run=False,
    )

    # Read the agent's verdict from bead metadata.
    try:
        verdict = read_bead_verdict(
            result.bead_id, "pr_writer", rig_path=rig_path_p
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "pr_writer: failed to read verdict from bead %s: %s",
            result.bead_id, exc,
        )
        verdict = {}
    if not isinstance(verdict, dict):
        verdict = {}

    # Normalize return shape regardless of what the agent wrote.
    out: dict[str, Any] = {
        "verdict": str(verdict.get("verdict") or "HALT").upper(),
        "pr": verdict.get("pr"),
        "url": verdict.get("url"),
        "branch": verdict.get("branch") or branch,
        "mode": verdict.get("mode") or "none",
        "scope_id": scope_id,
        "bead_id": result.bead_id,
    }
    if out["verdict"] not in {"PASS", "HALT"}:
        out["verdict"] = "HALT"
        out["reason"] = verdict.get("reason") or "agent wrote unexpected verdict shape"
    elif out["verdict"] == "HALT":
        out["reason"] = verdict.get("reason") or "agent halted (no reason given)"
    logger.info(
        "pr_writer: verdict=%s pr=%s mode=%s",
        out["verdict"], out["pr"], out["mode"],
    )
    return out
