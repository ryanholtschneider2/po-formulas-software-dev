"""Prefect flow: ``software-dev-agentic``.

A prompt-driven, minimal pipeline: **one actor agent** owns the whole
implementation loop and is told — in its prompt, not in orchestrator-wired
Python — to open a worktree off ``main``, implement the feature there, run
the repo's own tests / CI, and **open a PR** when it's done. Then **exactly
one critic agent** verifies *goal accomplishment*: did the actor implement
the requested feature faithfully per the request? If not, the critic returns
a concrete fix list and the actor iterates (the actor-critic goal loop).

There is no mechanical gate layer — running tests and opening the PR are the
actor's job (prompt-driven), and the critic is the only gate that matters.
The flow does **not** auto-merge: the actor leaves a PR for human review.
The *flow* (machine) performs the seed close on a critic pass; the actor
never closes its own seed.

Pipeline::

    claim seed
      → loop iter in 1..iter_cap:
            agent_step(agentic-worker)   (worktree off main → build → test → PR)
            agent_step(agentic-reviewer) (goal-accomplishment critic: pass | fail)
            if critic == pass: success
            else: feed the fix list back to the worker and iterate
      → close_issue(seed)  on a critic pass, else raise (forensics)

All the convergence machinery (bead-stamping, session affinity, nudge
ladder, verdict parsing, cache fast-path, run_dir, ``_record_flow_outcome``)
is reused wholesale from ``agent_step`` and ``software_dev``.
"""

from __future__ import annotations

import os
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


def _revision_note(fix_list: str) -> str:
    """Compose the retry guidance fed to the worker as ``revision_note``.

    ``fix_list`` is the critic's concrete fix list from the prior iteration
    (read off ``critique-iter-<n>.md``). Empty on the first iteration.
    """
    if not fix_list.strip():
        return ""
    return (
        "## Prior critic verdict: FAIL\n\n"
        "The critic found the change does not yet accomplish the goal. Address "
        "every item below, commit on your worktree branch, update your PR, and "
        "exit the turn:\n\n" + fix_list.strip()
    )


def _read_text(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


# ─────────────────────── flow ───────────────────────────────────────


@flow(name="software_dev_agentic", flow_run_name="{issue_id}", log_prints=True)
def software_dev_agentic(
    issue_id: str,
    rig: str,
    rig_path: str,
    pack_path: str | None = None,
    iter_cap: int = 2,
    parent_bead: str | None = None,
    dry_run: bool = False,
    claim: bool = True,
) -> dict[str, Any]:
    """One prompt-driven actor looped against one goal-verifying critic.

    The worker agent is prompted to work in a worktree off ``main``, run the
    repo's own tests / CI, and open a PR (none of which is orchestrator-wired
    code). The critic then verifies that the change faithfully accomplishes
    the request and returns ``pass`` / ``fail`` (with a concrete fix list on
    fail). The seed closes iff the critic passes — and the flow, never the
    worker, performs the close. The flow never merges to ``main``.

    Parameters mirror the ``software_dev_full`` subset that fanout
    dispatchers care about (``issue_id`` / ``rig`` / ``rig_path`` plus
    optional ``parent_bead`` / ``dry_run``).
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
        critic_verdict = ""
        fix_list = ""
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
                    "revision_note": _revision_note(fix_list),
                },
                verdict_keywords=("complete", "failed"),
                dry_run=dry_run,
            )
            logger.info(
                "agentic: worker iter %s closed_by=%s", iter_n, worker.closed_by
            )

            review = agent_step(
                agent_dir=_AGENTS_DIR / "agentic-reviewer",
                task=_AGENTS_DIR / "agentic-reviewer" / "task.md",
                seed_id=issue_id,
                rig_path=str(rig_path_p),
                run_dir=run_dir,
                step="review",
                iter_n=iter_n,
                ctx={"iter": iter_n, "pack_path": str(pack_path_p)},
                verdict_keywords=("pass", "fail"),
                dry_run=dry_run,
            )
            critic_verdict = review.verdict
            if dry_run:
                # StubBackend never closes the bead with a real verdict (the
                # convergence ladder force-closes it as "failed"). Treat the
                # `--dry-run` smoke as a pass so the worker→critic→close
                # wiring runs end to end.
                critic_verdict = "pass"
            logger.info("agentic: iter %s critic=%s", iter_n, critic_verdict)

            if critic_verdict == "pass":
                success = True
                break
            # Critic failed → read its fix list for the next worker turn.
            fix_list = _read_text(run_dir / f"critique-iter-{iter_n}.md")

        if not success:
            # Leave the seed open and raise for forensics — run_dir artifacts
            # (critiques, diffs, sessions) stay for `po retry` / inspection.
            raise RuntimeError(
                f"software-dev-agentic: did not converge after {iter_cap} iter(s) — "
                f"critic={critic_verdict or '(no verdict)'}"
            )

        if claim and not dry_run:
            close_issue(
                issue_id,
                notes=f"po software-dev-agentic complete: critic={critic_verdict}",
                rig_path=rig_path_p,
            )

        return {
            "status": "completed",
            "critic_verdict": critic_verdict,
        }
    except Exception as exc:
        _record_flow_outcome(run_dir, exc, issue_id, str(rig_path_p))
        raise


__all__ = ["software_dev_agentic"]
