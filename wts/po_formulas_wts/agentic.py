"""Prefect flow: ``software-dev-agentic-wts``.

Worktree-isolated variant of ``software-dev-agentic``.  The worker commits
on branch ``wts-<id>`` inside ``<rig>/.worktrees/wts-<id>/`` — never on the
main rig's checked-out branch.  On success the worktree merges back into
main (or hands off to the PR Sheriff in ADE mode).  On failure the worktree
is left intact for forensic inspection.

Gate layer and reviewer are identical to ``software_dev_agentic``; only the
worktree wiring and merge-back finalization differ.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from prefect import flow, get_run_logger
from prefect_orchestration.agent_step import agent_step
from prefect_orchestration.beads_meta import claim_issue, close_issue

# Reuse gate layer from parent pack (pure Python, path-agnostic).
from po_formulas.agentic import (
    GateReport,
    _AGENTS_DIR,
    _failed_checks,
    _git_head,
    _mechanical_gates,
    _read_text,
    _revision_note,
    _write_gate_report,
)
from po_formulas.software_dev import (
    _load_rig_env,
    _record_flow_outcome,
    _tag_flow_run_with_issue_id,
)
from po_formulas_wts.software_dev import (
    _handoff_to_sheriff,
    _resolve_epic_worktree_context,
    _sheriff_handoff_enabled,
)


@flow(name="software_dev_agentic_wts", flow_run_name="{issue_id}", log_prints=True)
def software_dev_agentic_wts(
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
    use_worktree: bool = True,
    parent_epic_worktree: str | None = None,
    parent_epic_branch: str | None = None,
    parent_epic_id: str | None = None,
    parent_epic_merge_target: str | None = None,
) -> dict[str, Any]:
    """Worktree-isolated agentic pipeline.

    Identical to software-dev-agentic but the worker commits on branch
    wts-<id> inside <rig>/.worktrees/wts-<id>/, not on the main rig's
    current branch.  On success, merges back into main (or hands off to the
    PR Sheriff in ADE mode).
    """
    logger = get_run_logger()
    main_rig_path_p = Path(rig_path).expanduser().resolve()

    # Worktree resolution — mirrors software_dev_full_wts pattern exactly.
    epic_worktree_context = _resolve_epic_worktree_context(
        issue_id,
        main_rig_path_p,
        parent_epic_worktree=parent_epic_worktree,
        parent_epic_branch=parent_epic_branch,
        parent_epic_id=parent_epic_id,
        parent_epic_merge_target=parent_epic_merge_target,
    )

    rig_path_p = main_rig_path_p
    worktree_enabled = False
    epic_managed_worktree = False

    if epic_worktree_context is not None:
        rig_path_p = Path(epic_worktree_context["work_dir"]).expanduser().resolve()
        epic_managed_worktree = True
        logger.info(
            "worktree: using parent epic worktree %s on branch %s",
            rig_path_p,
            epic_worktree_context.get("branch") or "(unknown)",
        )
    elif use_worktree and not os.environ.get("PO_WTS_NO_WORKTREE"):
        try:
            from po_formulas_wts.worktree import _is_git_repo, setup_worktree

            if _is_git_repo(main_rig_path_p):
                rig_path_p = setup_worktree(main_rig_path_p, issue_id)
                worktree_enabled = True
                logger.info(
                    "worktree: enabled — agent cwd=%s, bd+planning via symlink to %s",
                    rig_path_p,
                    main_rig_path_p,
                )
            else:
                logger.info(
                    "worktree: skipped — %s is not a git repo", main_rig_path_p
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("worktree: setup failed (%s); falling back to main rig", exc)
            rig_path_p = main_rig_path_p
            worktree_enabled = False

    # pack_path: when not explicitly provided, use rig_path_p (= worktree in
    # wts mode) so the gate layer diffs against the worktree branch.
    pack_path_p = Path(pack_path).expanduser().resolve() if pack_path else rig_path_p

    # run_dir always lives under the main rig's .planning/ (the worktree's
    # .planning is symlinked back here, so both paths resolve identically).
    run_dir = main_rig_path_p / ".planning" / "software-dev-agentic" / issue_id
    run_dir.mkdir(parents=True, exist_ok=True)

    try:
        _load_rig_env(rig_path_p)
        _tag_flow_run_with_issue_id(issue_id, logger)

        if claim and not dry_run:
            claim_issue(issue_id, assignee=f"po-{os.getpid()}", rig_path=rig_path_p)

        # Capture HEAD before any worker commit.  Every gate diffs
        # baseline_ref..HEAD so a multi-commit worker is fully covered.
        baseline_ref = _git_head(pack_path_p)

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
                "agentic-wts: worker iter %s closed_by=%s", iter_n, worker.closed_by
            )

            if dry_run:
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
                "agentic-wts: iter %s gates passed=%s failing=%s",
                iter_n,
                gates.passed,
                _failed_checks(gates),
            )

            if not gates.passed:
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
            logger.info("agentic-wts: iter %s reviewer=%s", iter_n, reviewer_verdict)
            if gates.passed and reviewer_verdict in {"high", "medium"}:
                success = True
                break

        if not success:
            failing = _failed_checks(gates) if gates else ["(no gate report)"]
            raise RuntimeError(
                f"software-dev-agentic-wts: did not converge after {iter_cap} iter(s) — "
                f"gates_passed={getattr(gates, 'passed', None)} "
                f"failing={failing} reviewer={reviewer_verdict or '(skipped)'}"
            )

        # Finalize — mirrors software_dev_full_wts logic exactly.
        # ADE mode: push branch + hand off to PR Sheriff (no direct merge).
        # Standalone: merge worktree back into main, then close seed.
        # Epic-managed worktrees: finalize at the epic level.
        merged_into = None
        sheriff = None
        handoff = (
            worktree_enabled
            and not epic_managed_worktree
            and not dry_run
            and _sheriff_handoff_enabled(main_rig_path_p)
        )
        logger.info(
            "agentic-wts: finalize worktree_enabled=%s handoff=%s epic_managed=%s",
            worktree_enabled,
            handoff,
            epic_managed_worktree,
        )
        if handoff:
            sheriff = _handoff_to_sheriff(main_rig_path_p, issue_id, logger)
        else:
            if worktree_enabled:
                from po_formulas_wts.worktree import merge_worktree

                try:
                    merged_into = merge_worktree(main_rig_path_p, issue_id, cleanup=True)
                    logger.info("worktree: merged into %s", merged_into)
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "worktree: merge failed (%s); leaving worktree at "
                        "<rig>/.worktrees/wts-<id> for resolution. Re-run after fixing.",
                        exc,
                    )
                    raise

            if claim and not dry_run:
                close_issue(
                    issue_id,
                    notes=(
                        f"po software-dev-agentic-wts complete: gates green, "
                        f"reviewer={reviewer_verdict}"
                    ),
                    rig_path=main_rig_path_p,
                )

        return {
            "status": "completed",
            "gates_passed": gates.passed if gates else False,
            "gate_checks": gates.checks if gates else {},
            "reviewer_verdict": reviewer_verdict,
            "worktree_merged_into": merged_into,
            "sheriff_handoff": sheriff,
            "epic_managed_worktree": epic_managed_worktree,
        }

    except BaseException as exc:
        _record_flow_outcome(run_dir, exc, issue_id, str(main_rig_path_p))
        # Leave worktree intact on failure for forensic inspection.
        raise


__all__ = ["software_dev_agentic_wts"]
