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

Per-rig preview/demo knobs (read from ``<rig>/.po-env`` via
``_load_rig_env``)::

    PO_PREVIEW=local|cloud|off   # default off
    PO_DEMO_VIDEO=0|1            # default 0

When ``PO_PREVIEW`` is ``local``/``cloud`` the worker is asked to leave a
reachable preview of the change and write its URL to
``<run_dir>/preview_url.txt``; on a critic pass the flow reads that file
and stamps it as ``po.preview_url`` on the seed bead (parallel to how core
stamps ``po.run_dir``) so dashboard cards can link the preview.
"""

from __future__ import annotations

import importlib
import os
import subprocess
from pathlib import Path
from typing import Any

from prefect import flow, get_run_logger
from prefect_orchestration.agent_step import agent_step
from prefect_orchestration.beads_meta import claim_issue, close_issue

from po_formulas import shared_branch
from po_formulas.software_dev import (
    _load_rig_env,
    _record_flow_outcome,
    _tag_flow_run_with_issue_id,
)

_AGENTS_DIR = Path(__file__).parent / "agents"

# Artifacts that only a prior run's ITERATIONS leave behind. metadata.json is
# deliberately excluded: `po retry` restores it into a fresh run_dir, and its
# presence alone must not trigger a (double) archive.
_PRIOR_ITER_ARTIFACTS = ("iter-bead-ids.json", "role-sessions.json")
_PRIOR_ITER_GLOBS = ("critique-iter-*.md", "build-iter-*.diff")


def _has_prior_iter_state(run_dir: Path) -> bool:
    """True when *run_dir* holds artifacts from a prior run's iterations.

    Distinguishes a stale re-dispatch (worker/critic already ran once) from a
    first dispatch (no run_dir / empty) and a `po retry` (fresh run_dir with at
    most metadata.json). See prefect-orchestration-17xa.
    """
    if any((run_dir / name).exists() for name in _PRIOR_ITER_ARTIFACTS):
        return True
    if (run_dir / "verdicts").is_dir():
        return True
    return any(next(run_dir.glob(pat), None) is not None for pat in _PRIOR_ITER_GLOBS)


def _archive_stale_run_dir(run_dir: Path) -> Path | None:
    """Rename a run_dir carrying prior-iteration state to ``<dir>.bak-<UTC>``.

    Returns the archive path, or None when there's nothing stale to archive
    (run_dir absent, or empty / retry-fresh). Matches `po retry`'s archive
    convention so `po artifacts` / forensics find both alike.
    """
    if not run_dir.exists() or not _has_prior_iter_state(run_dir):
        return None
    from datetime import datetime, timezone

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    archived = run_dir.with_name(f"{run_dir.name}.bak-{stamp}")
    run_dir.rename(archived)
    return archived


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


# ───────────────────── preview / demo knobs ─────────────────────────

# Where the worker writes its reachable preview URL; the flow reads it on
# a critic pass and stamps `po.preview_url`. Verdict-file pattern — never
# parse the agent's reply text.
_PREVIEW_URL_FILE = "preview_url.txt"
_VALID_PREVIEW_MODES = ("local", "cloud", "off")
_TRUTHY = ("1", "true", "yes", "on")


def _resolve_preview_mode() -> str:
    """Per-rig preview mode from ``PO_PREVIEW`` (loaded from ``.po-env``).

    Defaults to ``off`` (no preview work) so existing rigs are unchanged.
    Anything other than ``local``/``cloud``/``off`` falls back to ``off``.
    """
    mode = os.environ.get("PO_PREVIEW", "off").strip().lower()
    return mode if mode in _VALID_PREVIEW_MODES else "off"


def _demo_video_requested() -> bool:
    """Whether this rig requests a demo video (``PO_DEMO_VIDEO`` truthy)."""
    return os.environ.get("PO_DEMO_VIDEO", "0").strip().lower() in _TRUTHY


def _preview_note(mode: str, run_dir: Path, demo: bool) -> str:
    """Worker instruction block for the preview/demo knobs.

    Returns ``""`` when there's nothing to ask for (``off`` mode and no
    demo request) so the prompt is unchanged for rigs that haven't opted
    in. The worker writes the URL to ``<run_dir>/preview_url.txt``; the
    flow stamps it as ``po.preview_url``.
    """
    url_file = run_dir / _PREVIEW_URL_FILE
    parts: list[str] = []
    if mode == "local":
        parts.append(
            "# Leave a reachable preview (PO_PREVIEW=local)\n\n"
            "After opening the PR, start the rig's dev server in the background "
            "(e.g. `npm run dev &`, `uv run uvicorn ... &`) and leave it running. "
            "Confirm it answers (curl the port), then write the reachable URL on "
            f"its own line to `{url_file}` — e.g. `http://localhost:5173`. The "
            "orchestrator reads that file and stamps it as `po.preview_url` on "
            "the seed bead so the dashboard can link it. If the change has no "
            "runnable surface (backend-only / library), write nothing and say so "
            "in your build summary."
        )
    elif mode == "cloud":
        parts.append(
            "# Leave a reachable preview (PO_PREVIEW=cloud)\n\n"
            "This is a cloud/rclaude workspace. After opening the PR, start the "
            "dev server in the background, then run `rpreview <port>` to resolve a "
            "public preview URL (the rclaude shim maps it to a reachable link / "
            "k8s ingress; the workspace stays up per the backend's auto-stop "
            f"settings). Write that URL on its own line to `{url_file}`. The "
            "orchestrator stamps it as `po.preview_url`. If there's no runnable "
            "surface, write nothing and note it in your build summary."
        )
    if demo:
        parts.append(
            "# Record a demo video (PO_DEMO_VIDEO=1)\n\n"
            "This rig requests a short demo video for visual changes. If the "
            "change has a UI surface, record a brief screen capture of the new "
            "behavior (the rig's playwright / demo tooling) and note its path in "
            "your build summary. Skip for backend-only changes."
        )
    return "\n\n".join(parts)


def _bd_set_metadata(issue_id: str, key: str, value: str, rig_path: Path) -> None:
    """Stamp ``key=value`` on a bead (best-effort).

    Tries metadata (``--set-metadata``, dolt-bd). beads-rust has NO arbitrary
    metadata — only labels — so for ``po.formula`` we ALSO add a
    ``formula:<value>`` label, which ``_resolve_per_bead_formula`` reads. That
    keeps the per-child formula stamp working on beads-rust.
    """
    subprocess.run(
        ["bd", "update", issue_id, "--set-metadata", f"{key}={value}"],
        check=False,
        capture_output=True,
        text=True,
        cwd=str(rig_path),
    )
    if key == "po.formula":
        subprocess.run(
            ["bd", "update", issue_id, "--add-label", f"formula:{value}"],
            check=False,
            capture_output=True,
            text=True,
            cwd=str(rig_path),
        )


def _dispatch_pr_sheriff(rig_path: Path, issue_id: str, logger: Any) -> None:
    """Fire the workspace's pr-sheriff deployment for this PR (best-effort).

    The worker has opened a PR and the critic passed, so this is the natural
    "PR opened" moment. Prefers SoloCo's own ``soloco-sheriff`` (the independent
    SoloCo runtime) and falls back to po-director's ``pr-sheriff``. Each
    ``on_pr_opened`` gates on the workspace ``merge_mode`` (only ``auto`` /
    ``ai-approve-all`` dispatch) **and** on its own standing deployment existing
    for the rig, so a SoloCo workspace routes to ``soloco-sheriff`` while a
    Director workspace routes to ``pr-sheriff`` — whichever owns the rig fires,
    and the first that dispatches wins (never both). Optional + non-fatal: a
    pack may not be installed, the rig may not be a managed workspace, or
    Prefect may be unreachable — none of which should fail a completed run.

    This is the seam where an auto-merge pipeline most often appears to stall,
    so every outcome is logged loudly — entry, each candidate module
    (``unavailable`` / ``dispatched`` / ``declined`` / ``skipped``), and a
    terminal line when nothing dispatched. The earlier silent return on the
    "declined" path made "did the flow even try, and what happened?"
    unanswerable from the run log: an operator chasing a stuck PR could not tell
    a dispatch that fired (problem is downstream — worker/pool/merge) from one
    that never fired (problem is here) — see po-formulas-software-dev-2wp.
    """
    logger.info(
        "agentic: PR sheriff dispatch — start (issue=%s rig=%s)", issue_id, rig_path
    )
    tried: list[str] = []
    for module_name, label in (
        ("po_soloco.sheriff_dispatch", "soloco-sheriff"),
        ("po_director.sheriff_dispatch", "pr-sheriff"),
    ):
        tried.append(label)
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:  # noqa: BLE001 — pack may not be installed
            logger.info("agentic: %s unavailable (%s)", label, exc)
            continue
        try:
            if module.on_pr_opened(str(rig_path), issue_id):
                logger.info("agentic: dispatched %s for %s", label, issue_id)
                return
            logger.info(
                "agentic: %s declined %s (not this rig's sheriff, or merge_mode "
                "not auto, or its deployment is unapplied)",
                label,
                issue_id,
            )
        except Exception as exc:  # noqa: BLE001 — sheriff dispatch is best-effort
            logger.info("agentic: %s dispatch skipped (%s)", label, exc)
    logger.info(
        "agentic: no PR sheriff dispatched for %s (tried: %s) — PR left open for "
        "manual review (workspace is manual-merge, or no managed sheriff owns "
        "this rig)",
        issue_id,
        ", ".join(tried),
    )


def _stamp_preview_url(issue_id: str, rig_path: Path, run_dir: Path) -> str:
    """Read ``<run_dir>/preview_url.txt`` and stamp ``po.preview_url``.

    Returns the stamped URL, or ``""`` when the worker wrote no preview
    (missing/empty file). Takes the last non-empty line so a worker that
    appends notes above the URL still works.
    """
    raw = _read_text(run_dir / _PREVIEW_URL_FILE)
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not lines:
        return ""
    url = lines[-1]
    _bd_set_metadata(issue_id, "po.preview_url", url, rig_path)
    return url


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
    epic_branch: str | None = None,
    parent_epic_id: str | None = None,
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

    **Shared-branch epic mode** (``epic_branch`` set, threaded by
    ``agentic_epic(shared_branch=True)``): the worker is told — via the
    ``branch_directive`` prompt block — to branch off the *current epic tip*
    instead of ``main`` and to push but **not** open its own PR. On critic-pass
    the flow integrates the child's branch into ``epic_branch`` (serialized,
    locked) instead of dispatching the PR sheriff, and skips per-child preview
    stamping. ``parent_epic_id`` identifies the epic for the integration
    worktree + lock. When ``epic_branch`` is ``None`` behavior is unchanged
    (per-child worktree off ``main`` + its own PR).
    """
    logger = get_run_logger()
    rig_path_p = Path(rig_path).expanduser().resolve()
    pack_path_p = Path(pack_path).expanduser().resolve() if pack_path else rig_path_p
    run_dir = rig_path_p / ".planning" / "software-dev-agentic" / issue_id
    # Fresh-dispatch hygiene (prefect-orchestration-17xa): a plain re-dispatch
    # of an already-run issue_id reuses this deterministic run_dir, whose stale
    # iter-bead-ids.json maps the convention key to a PRIOR run's closed bead.
    # agent_step's fast-path then sees a closed bead and returns
    # `closed_by=cache` — the worker never runs, the critic fails on no work,
    # and we get a RUNNING zombie. Archive the stale run_dir so the dispatch
    # starts clean. A real RESUME (`po resume`, PO_RESUME=1) must keep the
    # run_dir; `po retry` already archived and leaves only metadata.json, so it
    # carries no prior-iteration state and is not re-archived here.
    if not dry_run and os.environ.get("PO_RESUME") != "1":
        archived = _archive_stale_run_dir(run_dir)
        if archived is not None:
            logger.warning(
                "agentic: fresh dispatch of %s found a prior run_dir — archived "
                "to %s so the worker runs clean (not from stale cache)",
                issue_id,
                archived.name,
            )
    run_dir.mkdir(parents=True, exist_ok=True)

    if claim and not dry_run:
        claim_issue(issue_id, assignee=f"po-{os.getpid()}", rig_path=rig_path_p)

    _load_rig_env(rig_path_p)
    _tag_flow_run_with_issue_id(issue_id, logger)

    preview_mode = _resolve_preview_mode()
    preview_note = _preview_note(preview_mode, run_dir, _demo_video_requested())

    # Shared-branch epic mode: the worker branches off the epic tip and pushes
    # without opening a PR; the flow integrates on pass. Empty directive in the
    # default per-child-PR mode keeps the worker prompt byte-for-byte unchanged.
    shared_mode = bool(epic_branch)
    worker_branch_directive = (
        shared_branch.branch_directive(epic_branch, issue_id) if shared_mode else ""
    )

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
                    "preview_note": preview_note,
                    "branch_directive": worker_branch_directive,
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

        # Shared-branch mode: the CHILD merges its own branch back into the epic
        # branch now that its critic passed — agent-owned integration, no
        # deterministic `git merge` in the flow. Serialized by the per-epic lock
        # (transport) so parallel lanes don't race the shared ref; the merge-back
        # agent resolves any conflict (which means the planner mis-ordered two
        # same-surface children). The advanced epic tip is what the next dependent
        # child stacks on.
        integration: dict[str, object] | None = None
        preview_url = ""
        if shared_mode:
            if not dry_run:
                epic_for_wt = parent_epic_id or parent_bead or issue_id
                wt = shared_branch.ensure_integration_worktree(rig_path_p, epic_for_wt)
                child_branch = shared_branch.child_branch_name(issue_id)
                with shared_branch.integration_lock(rig_path_p, epic_for_wt):
                    mb = agent_step(
                        agent_dir=_AGENTS_DIR / "agentic-merge-back",
                        task=_AGENTS_DIR / "agentic-merge-back" / "task.md",
                        seed_id=issue_id,
                        rig_path=str(rig_path_p),
                        run_dir=run_dir,
                        step="merge-back",
                        iter_n=1,
                        ctx={
                            "worktree": str(wt),
                            "epic_branch": epic_branch,
                            "child_branch": child_branch,
                        },
                        verdict_keywords=("merged", "failed"),
                    )
                merged = mb.verdict == "merged"
                integration = {
                    "merged": merged,
                    "child_branch": child_branch,
                    "reason": "" if merged else "merge-back agent could not integrate",
                }
                if merged:
                    logger.info("agentic: %s merged itself into %s", issue_id, epic_branch)
                else:
                    logger.warning(
                        "agentic: %s merge-back failed — acceptance critic will flag the gap",
                        issue_id,
                    )
        else:
            # End-of-run preview: read the worker's preview_url.txt and stamp
            # po.preview_url so dashboard cards can link it. Best-effort — a
            # backend-only change leaves no file and stamps nothing.
            if not dry_run:
                preview_url = _stamp_preview_url(issue_id, rig_path_p, run_dir)
                if preview_url:
                    logger.info("agentic: stamped po.preview_url=%s", preview_url)
                elif preview_mode != "off":
                    logger.info(
                        "agentic: PO_PREVIEW=%s but worker wrote no %s",
                        preview_mode,
                        _PREVIEW_URL_FILE,
                    )

        if claim and not dry_run:
            close_issue(
                issue_id,
                notes=f"po software-dev-agentic complete: critic={critic_verdict}",
                rig_path=rig_path_p,
            )

        # Per-child-PR mode only: the worker's PR is open and the critic passed,
        # so announce the PR to po-director, which fires the PR Sheriff iff the
        # workspace is in an auto merge mode. Shared-branch children open no PR
        # of their own (the epic owns the single PR), so this is skipped for them.
        if not dry_run and not shared_mode:
            _dispatch_pr_sheriff(rig_path_p, issue_id, logger)

        return {
            "status": "completed",
            "critic_verdict": critic_verdict,
            "preview_url": preview_url,
            "integration": integration,
        }
    except Exception as exc:
        _record_flow_outcome(run_dir, exc, issue_id, str(rig_path_p))
        raise


__all__ = ["software_dev_agentic"]
