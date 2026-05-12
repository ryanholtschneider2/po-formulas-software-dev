"""Prefect flow: `minimal-task`.

A trimmed actor-critic pipeline for trivial fanout children:

    triage → plan → build → lint → close

No baseline, no plan-critic, no regression-gate, no review, no
deploy-smoke, no review-artifacts, no verification, no ralph, no docs,
no demo, no learn. Lint failure twice fails the flow loudly so 100-way
fanout epics don't burn tokens recovering from broken trivia.

Rewritten 2026-04-29 to use `agent_step` (the simplified primitive)
+ plain Python, replacing the legacy RoleRegistry + nested-loop body.
~50 LOC vs 140 in the prior shape; all the convergence machinery
(bead-stamping, session affinity, nudge ladder, verdict parsing)
lives in core's `agent_step`.

The bead graph produced by one run::

    <seed>                       (the user's issue)
    ├── <seed>.triage.iter1      (closed by triager agent)
    ├── <seed>.plan.iter1        (closed by builder agent in plan mode)
    ├── <seed>.build.iter1       (closed by builder)
    ├── <seed>.lint.iter1        "clean" or "failed"
    ├── <seed>.build.iter2       (only if iter1 failed lint)
    └── <seed>.lint.iter2        "clean" → success, "failed" → raise

The agents reuse `agents/<role>/{prompt,task}.md` from the same pack —
identity prompt + task spec stay shared with `software_dev_full`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from prefect import flow
from prefect_orchestration.agent_step import agent_step
from prefect_orchestration.beads_meta import claim_issue, close_issue


_AGENTS_DIR = Path(__file__).parent / "agents"


@flow(name="minimal_task_wts", flow_run_name="{issue_id}", log_prints=True)
def minimal_task(
    issue_id: str,
    rig: str,
    rig_path: str,
    pack_path: str | None = None,
    parent_bead: str | None = None,
    dry_run: bool = False,
    claim: bool = True,
) -> dict[str, Any]:
    """Lightweight `triage → plan → build → lint → close` pipeline.

    Used for high-fanout epics (e.g. snake-bead demos) where the full
    actor-critic loop would burn tokens on trivial children. On lint
    failure the builder gets one more attempt; if lint fails twice
    in a row the flow raises and the bead stays `in_progress`.

    Kwargs match the subset of `software_dev_full` that fanout
    dispatchers care about, so swapping formulas is a one-token change.
    """
    rig_path_p = Path(rig_path).expanduser().resolve()

    if claim and not dry_run:
        claim_issue(issue_id, assignee=f"po-{os.getpid()}", rig_path=rig_path_p)

    # 1. Triage — same agent + task as software_dev_full so verdicts
    #    written to verdicts/triage.json stay compatible with `po`
    #    artifacts/watch/logs.
    agent_step(
        agent_dir=_AGENTS_DIR / "triager",
        task=_AGENTS_DIR / "triager" / "task.md",
        seed_id=issue_id,
        rig_path=str(rig_path_p),
        step="triage",
        iter_n=1,
        ctx={"pack_path": pack_path or str(rig_path_p)},
        dry_run=dry_run,
    )

    # 2. Plan — single pass; no plan-critic in this minimal flow.
    agent_step(
        agent_dir=_AGENTS_DIR / "planner",
        task=_AGENTS_DIR / "planner" / "task.md",
        seed_id=issue_id,
        rig_path=str(rig_path_p),
        step="plan",
        iter_n=1,
        dry_run=dry_run,
    )

    # 3. Build + lint loop — at most 2 iterations. iter2 sees prior
    #    lint summary as revision_note context.
    last_verdict: str = ""
    last_summary: str = ""
    iter_cap = 2
    for iter_n in range(1, iter_cap + 1):
        revision_note = (
            ""
            if iter_n == 1
            else f"## Prior lint failure\n\nLint iter {iter_n - 1} failed: "
            f"{last_summary}\n\nFix the lint errors, commit, and exit the turn."
        )
        agent_step(
            agent_dir=_AGENTS_DIR / "builder",
            task=_AGENTS_DIR / "builder" / "task.md",
            seed_id=issue_id,
            rig_path=str(rig_path_p),
            step="build",
            iter_n=iter_n,
            ctx={
                "iter": iter_n,
                "revision_note": revision_note,
                "pack_path": pack_path or str(rig_path_p),
            },
            dry_run=dry_run,
        )
        lint_result = agent_step(
            agent_dir=_AGENTS_DIR / "linter",
            task=_AGENTS_DIR / "linter" / "task.md",
            seed_id=issue_id,
            rig_path=str(rig_path_p),
            step="lint",
            iter_n=iter_n,
            verdict_keywords=("clean", "failed"),
            dry_run=dry_run,
        )
        last_verdict = lint_result.verdict
        last_summary = lint_result.summary
        if last_verdict == "clean":
            break

    if last_verdict != "clean":
        # No ralph fallback — fail loudly so the bead stays in_progress
        # and run-dir artifacts remain for forensics. Don't close the
        # seed.
        raise RuntimeError(
            f"minimal-task: lint failed after {iter_cap} build iterations: "
            f"{last_summary or '(no summary)'}"
        )

    if claim and not dry_run:
        close_issue(issue_id, notes="po minimal-task complete", rig_path=rig_path_p)

    return {
        "status": "completed",
        "lint_verdict": last_verdict,
        "lint_summary": last_summary,
    }
