"""Prefect flow: `epic_wts` — full epic-producing machine.

Auto-chains the four pieces that previously required four separate `po
run` invocations:

    1. epic_run            — fan out children through software-dev-full-wts
    2. pre_pr_review       — 3-pillar cumulative review (regression, critic, smoke)
    3. epic_finalize       — spec audit + lint/tests + smoke walkthrough +
                             demo video + remote-CI gate + docs + close epic
    4. pr_writer           — populate / refresh the PR body per pr-format

Each step gates on the previous via `metadata.*` stamps left on the epic
bead. The chain short-circuits on the first hard failure:

  - epic_run failure (children didn't all close) →
        skip pre_pr_review / finalize / pr_writer.
  - pre_pr_review.validation == "blocked" →
        skip finalize / pr_writer (regressions or critic findings need
        fixing first).
  - epic_finalize failures (test/lint/smoke/demo/CI) →
        skip pr_writer (PR body is meaningless on a red epic).

pr_writer currently stubs to NotImplementedError in this pack; this
flow catches that and logs a warning so the chain doesn't appear broken
to the operator while prefect-orchestration-3pt is open.

Filed under: prefect-orchestration-54n.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from prefect import flow, get_run_logger

from po_formulas_wts.epic import epic_run
from po_formulas_wts.epic_finalize import epic_finalize
from po_formulas_wts.pre_pr_review import pre_pr_review
from po_formulas_wts.pr_writer import pr_writer


@flow(name="epic_wts", flow_run_name="{epic_id}", log_prints=True)
def epic_wts(
    epic_id: str,
    rig: str,
    rig_path: str,
    *,
    merge_target_branch: str = "main",
    spec_path: str | None = None,
    smoke_cmd: str | None = None,
    walkthrough_script: str | None = None,
    skip_walkthrough: bool = False,
    skip_demo_video: bool = False,
    skip_remote_ci: bool = False,
    skip_pr_writer: bool = False,
    ci_timeout_s: int | None = None,
    max_issues: int | None = None,
    discover: str = "both",
    child_ids: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """End-to-end epic dispatcher. One invocation produces a merge-ready PR.

    Args:
        epic_id: bd epic id.
        rig, rig_path: standard PO rig args.
        merge_target_branch: base branch for pre-pr-review baseline diff.
        spec_path, smoke_cmd, walkthrough_script, skip_*, ci_timeout_s:
            forwarded to epic_finalize.
        max_issues, discover, child_ids: forwarded to epic_run.
        skip_pr_writer: skip the final PR-body write (e.g. when
            pr_writer is stubbed or you just want the gates).
        dry_run: forwarded to every sub-flow.

    Returns: aggregated dict with one key per sub-flow + a top-level
    `verdict` ∈ {passed, blocked, failed, partial}.
    """
    logger = get_run_logger()
    rig_path_p = Path(rig_path).expanduser().resolve()
    out: dict[str, Any] = {"epic_id": epic_id, "verdict": "unknown"}

    # 1. Fan-out.
    logger.info("epic_wts: dispatching children via epic_run")
    epic_out = epic_run(
        epic_id=epic_id,
        rig=rig,
        rig_path=str(rig_path_p),
        dry_run=dry_run,
        max_issues=max_issues,
        discover=discover,
        child_ids=child_ids,
    )
    out["epic_run"] = epic_out
    epic_failed = bool(epic_out.get("failed_ids") or epic_out.get("errors"))
    if epic_failed:
        logger.warning("epic_wts: epic_run reported failures; aborting chain")
        out["verdict"] = "failed"
        return out

    # 2. Pre-PR review.
    logger.info("epic_wts: pre-pr-review")
    review_out = pre_pr_review(
        epic_id=epic_id,
        rig_path=str(rig_path_p),
        merge_target_branch=merge_target_branch,
        dry_run=dry_run,
    )
    out["pre_pr_review"] = review_out
    if review_out.get("validation") == "blocked":
        logger.warning(
            "epic_wts: pre-pr-review blocked (%d finding-beads); aborting chain",
            len(review_out.get("bead_ids") or []),
        )
        out["verdict"] = "blocked"
        return out

    # 3. Epic finalize (lint/tests/smoke/demo/CI/docs/close).
    logger.info("epic_wts: epic-finalize")
    finalize_kwargs: dict[str, Any] = {
        "epic_id": epic_id,
        "rig": rig,
        "rig_path": str(rig_path_p),
        "spec_path": spec_path,
        "smoke_cmd": smoke_cmd,
        "walkthrough_script": walkthrough_script,
        "skip_walkthrough": skip_walkthrough,
        "skip_demo_video": skip_demo_video,
        "skip_remote_ci": skip_remote_ci,
        "dry_run": dry_run,
    }
    if ci_timeout_s is not None:
        finalize_kwargs["ci_timeout_s"] = ci_timeout_s
    finalize_out = epic_finalize(**finalize_kwargs)
    out["epic_finalize"] = finalize_out
    if finalize_out.get("status") != "PASSED":
        logger.warning(
            "epic_wts: epic_finalize FAILED (%d failure(s)); skipping pr_writer",
            len(finalize_out.get("failures") or []),
        )
        out["verdict"] = "failed"
        return out

    # 4. PR writer.
    if skip_pr_writer:
        logger.info("epic_wts: pr_writer skipped (skip_pr_writer=True)")
        out["pr_writer"] = {"skipped": True}
        out["verdict"] = "passed"
        return out
    try:
        pr_out = pr_writer(
            epic_id=epic_id,
            rig=rig,
            rig_path=str(rig_path_p),
            dry_run=dry_run,
        )
        out["pr_writer"] = pr_out
        # pr_writer returns verdict in {"PASS", "HALT"}. HALT = agent
        # stopped cleanly (rebase conflict, non-FF push reject, etc.) —
        # gates green but no PR opened. Report `partial` so the operator
        # sees both the green gates and the halt reason.
        if pr_out.get("verdict") == "PASS":
            out["verdict"] = "passed"
        else:
            out["verdict"] = "partial"
            logger.warning(
                "epic_wts: pr_writer HALT (%s); gates green but PR not opened",
                pr_out.get("reason", "no reason given"),
            )
    except NotImplementedError as exc:
        # Defensive — pr_writer was a stub until prefect-orchestration-3pt
        # landed. Kept so an accidental revert of pr_writer.py surfaces
        # a clear "partial" verdict instead of crashing the chain.
        logger.warning("epic_wts: pr_writer stub unexpectedly active: %s", exc)
        out["pr_writer"] = {"skipped": True, "reason": str(exc)}
        out["verdict"] = "partial"
    return out
