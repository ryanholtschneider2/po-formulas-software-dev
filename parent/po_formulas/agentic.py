"""Prefect flow: ``software-dev-agentic``.

A prompt-driven, minimal pipeline: **one actor agent** owns the whole
implementation loop and is told — in its prompt, not in orchestrator-wired
Python — to open a worktree off ``main``, implement the feature there, run
the repo's own tests / CI, and **open a PR** when it's done. A goal critic
checks the diff. For changes whose model-authored sizing declares live or
deployable surfaces, the flow then runs the applicable smoke, demo,
review-artifact, and live-verifier roles. Any failed semantic verification
returns a concrete fix list to the same actor.

Running tests and opening the PR remain the actor's job (prompt-driven).
Python only transports the model-authored proof plan between semantic roles;
the reviewers judge quality. The flow does **not** auto-merge: the actor leaves
a PR for human review. The *flow* performs the seed close after all required
reviewers pass; the actor never closes its own seed.

Pipeline::

    claim seed
      → loop iter in 1..iter_cap:
            agent_step(agentic-worker)   (worktree off main → build → test → PR)
            agent_step(agentic-reviewer) (goal-accomplishment critic: pass | fail)
            if critic == pass: run sizing-selected proof phases
            if verifier == approved (or not required): success
            else: feed the fix list back to the worker and iterate
      → close_issue(seed)  after all required reviewers pass, else raise

All the convergence machinery (bead-stamping, session affinity, nudge
ladder, verdict parsing, cache fast-path, run_dir, ``_record_flow_outcome``)
is reused wholesale from ``agent_step`` and ``software_dev``.

Per-rig proof/preview/demo knobs (read from ``<rig>/.po-env`` via
``_load_rig_env``)::

    PO_AGENTIC_PROOF_MODE=adaptive|strict  # default adaptive
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
import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

from prefect import flow, get_run_logger
from prefect_orchestration.agent_step import agent_step
from prefect_orchestration.beads_meta import claim_issue, close_issue

from po_formulas import shared_branch
from po_formulas import delivery_truth
from po_formulas import verified_delivery
from po_formulas import agentic_sizing
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


def _git_revision(repo: Path, revision: str = "HEAD") -> str | None:
    """Resolve *revision* in *repo*, returning no opinion outside a git tree."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", revision],
            cwd=repo,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    stdout = getattr(result, "stdout", "")
    return stdout.strip() if result.returncode == 0 and stdout.strip() else None


def _dispatch_provenance(
    run_dir: Path, rig: str, rig_path: Path, pack_path: Path, parent_epic: str | None
) -> dict[str, Any]:
    """Collect the exact runtime tuple, preferring the dispatch artifact."""
    dispatch: dict[str, Any] = {}
    try:
        loaded = json.loads((run_dir / ".po-dispatch.json").read_text())
        if isinstance(loaded, dict):
            dispatch = loaded
    except (OSError, json.JSONDecodeError):
        pass
    runtime = dispatch.get("runtime_env")
    runtime = runtime if isinstance(runtime, dict) else {}
    argv = dispatch.get("argv")
    command = (
        shlex.join(str(part) for part in argv)
        if isinstance(argv, list)
        else os.environ.get("PO_DISPATCH_COMMAND")
    )
    return {
        "formula": dispatch.get("formula", "software-dev-agentic"),
        "backend": runtime.get("PO_BACKEND", os.environ.get("PO_BACKEND")),
        "provider": runtime.get("PO_PROVIDER", os.environ.get("PO_PROVIDER", "codex")),
        "account": runtime.get("PO_ACCOUNT", os.environ.get("PO_ACCOUNT")),
        "account_class": runtime.get(
            "PO_ACCOUNT_CLASS", os.environ.get("PO_ACCOUNT_CLASS")
        ),
        "model": runtime.get("PO_MODEL_CLI", os.environ.get("PO_MODEL_CLI")),
        "effort": runtime.get("PO_EFFORT_CLI", os.environ.get("PO_EFFORT_CLI")),
        "rig": rig,
        "rig_path": str(rig_path),
        "pack_path": str(pack_path),
        "parent_epic": parent_epic,
        "flow_run_id": os.environ.get("PREFECT__FLOW_RUN_ID"),
        "dispatch_command": command,
    }


# ───────────────────── preview / demo knobs ─────────────────────────

# Where the worker writes its reachable preview URL; the flow reads it on
# a critic pass and stamps `po.preview_url`. Verdict-file pattern — never
# parse the agent's reply text.
_PREVIEW_URL_FILE = "preview_url.txt"
_LEARNING_RECEIPT_FILE = "learning-receipt.md"
_VALID_PREVIEW_MODES = ("local", "cloud", "off")
_VALID_PROOF_MODES = ("adaptive", "strict")
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


def _resolve_proof_mode() -> str:
    """Return the opt-in delivery proof policy, preserving adaptive defaults."""
    mode = os.environ.get("PO_AGENTIC_PROOF_MODE", "adaptive").strip().lower()
    return mode if mode in _VALID_PROOF_MODES else "adaptive"


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


def _bd_add_label(issue_id: str, label: str, rig_path: Path) -> None:
    """Add a portable audit label to a bead (best-effort transport)."""
    subprocess.run(
        ["bd", "update", issue_id, "--add-label", label],
        check=False,
        capture_output=True,
        text=True,
        cwd=str(rig_path),
    )


def _record_sizing_labels(
    issue_id: str,
    decision: agentic_sizing.SizingDecision,
    rig_path: Path,
) -> None:
    for label in (
        f"po_size:{decision.size}",
        f"po_risk:{decision.risk}",
        f"po_sizing_decision:{decision.decision}",
        f"po_iteration_budget:{decision.iteration_budget}",
    ):
        _bd_add_label(issue_id, label, rig_path)


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


def _record_proof_result(run_dir: Path, *, step: str, iter_n: int, result: Any) -> None:
    """Append one role result and discover its concrete review artifacts."""
    contract = verified_delivery.read(run_dir)
    results = list(contract["live_verification"]["results"])
    results.append(
        {
            "step": step,
            "iteration": iter_n,
            "verdict": result.verdict,
            "bead_id": result.bead_id,
        }
    )
    review_dir = run_dir / "review-artifacts"
    screenshots = [
        {"path": str(path), "iteration": iter_n}
        for path in sorted(review_dir.glob("*.png"))
    ]
    patch: dict[str, Any] = {
        "live_verification": {"results": results},
        "screenshots": screenshots,
    }
    demo_path = review_dir / "demo.mp4"
    if demo_path.is_file():
        patch["demo"] = {"path": str(demo_path)}
    verified_delivery.update(run_dir, patch)


def _clear_demo_evidence(run_dir: Path) -> None:
    """Remove prior demo bytes so a retry must prove the current iteration."""
    (run_dir / "demo.mp4").unlink(missing_ok=True)
    (run_dir / "review-artifacts" / "demo.mp4").unlink(missing_ok=True)


def _demo_evidence_failure(
    run_dir: Path, *, verdict: str = "", error: Exception | None = None
) -> str:
    """Return a concrete revision note when required demo proof is absent."""
    if error is not None:
        return (
            "Required UI demo proof failed to run: "
            f"{error}. Fix the demo/runtime failure and produce a non-empty "
            f"{run_dir / 'review-artifacts' / 'demo.mp4'}."
        )
    demo_path = run_dir / "review-artifacts" / "demo.mp4"
    if verdict != "recorded":
        return (
            f"Required UI demo proof returned {verdict or 'no verdict'} instead "
            f"of recorded. Produce a non-empty current demo at {demo_path}."
        )
    try:
        has_bytes = demo_path.stat().st_size > 0
    except OSError:
        has_bytes = False
    if has_bytes:
        return ""
    return (
        "Required UI demo proof was skipped or missing. Produce a non-empty "
        f"demo for the current iteration at {demo_path}; stale evidence from "
        "an earlier iteration does not count."
    )


def _clear_phase_evidence(run_dir: Path, plan: agentic_sizing.DeliveryPlan) -> None:
    """Remove phase-owned outputs so retries cannot reuse stale proof bytes."""
    if plan.deploy_smoke:
        (run_dir / "smoke-test-output.txt").unlink(missing_ok=True)
    if plan.review_artifacts:
        review_dir = run_dir / "review-artifacts"
        (review_dir / "summary.md").unlink(missing_ok=True)
        (review_dir / "overview.md").unlink(missing_ok=True)


def _nonempty_file(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _proof_evidence_failure(
    run_dir: Path,
    *,
    step: str,
    iter_n: int,
    require_overview: bool = False,
) -> str:
    """Validate only the structural evidence contract for a proof phase."""
    if step == "deploy-smoke":
        path = run_dir / "smoke-test-output.txt"
        if not _nonempty_file(path):
            return f"Deploy smoke produced no fresh non-empty evidence at {path}."
        body = _read_text(path).lower()
        if "smoke failed" in body or "status: fail" in body or "result: fail" in body:
            return f"Deploy smoke evidence records failure at {path}."
        return ""
    if step == "review-artifacts":
        summary = run_dir / "review-artifacts" / "summary.md"
        if not _nonempty_file(summary):
            return f"Review packaging produced no fresh non-empty summary at {summary}."
        overview = run_dir / "review-artifacts" / "overview.md"
        if require_overview and not _nonempty_file(overview):
            return (
                "Workflow/infrastructure review packaging produced no fresh "
                f"non-empty overview at {overview}."
            )
        return ""
    if step == "verify":
        report = run_dir / f"verification-report-iter-{iter_n}.md"
        if not _nonempty_file(report):
            return f"Live verifier produced no fresh non-empty report at {report}."
        return ""
    raise ValueError(f"unknown proof step: {step}")


# ─────────────────────── flow ───────────────────────────────────────


@flow(name="software_dev_agentic", flow_run_name="{issue_id}", log_prints=True)
def software_dev_agentic(
    issue_id: str,
    rig: str,
    rig_path: str,
    pack_path: str | None = None,
    iter_cap: int | None = None,
    parent_bead: str | None = None,
    dry_run: bool = False,
    claim: bool = True,
    base_branch: str = "main",
    epic_branch: str | None = None,
    parent_epic_id: str | None = None,
) -> dict[str, Any]:
    """One prompt-driven actor looped against one goal-verifying critic.

    A sizing agent first judges whether the seed is one PR-sized unit, selects
    a bounded iteration budget, and classifies its delivery surfaces. The
    worker opens a worktree, implements, tests, and opens a PR. A diff critic
    checks goal accomplishment; live/deployable surfaces then run the proof
    phases selected from sizing. Any critic or verifier rejection returns to
    the worker. The flow closes the seed only after every required reviewer
    passes and never merges to ``main``.

    Parameters mirror the ``software_dev_full`` subset that fanout
    dispatchers care about (``issue_id`` / ``rig`` / ``rig_path`` plus
    optional ``parent_bead`` / ``dry_run``).

    ``base_branch`` (default ``"main"``, settable as ``--base-branch`` on
    ``po run``) is the branch the worker cuts its worktree off and opens its
    PR *against*. Set it to keep a feature's child PRs off ``main`` — e.g. an
    integration branch like ``redesign-2026-06-28`` — so nothing reaches the
    deploy branch until the operator merges the integration branch. It is
    ignored in shared-branch epic mode (``epic_branch`` set), where the
    ``branch_directive`` makes the worker branch off the epic tip instead.

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

    provenance = _dispatch_provenance(
        run_dir, rig, rig_path_p, pack_path_p, parent_epic_id or parent_bead
    )
    verified_delivery.initialize(
        run_dir,
        provenance=provenance,
        base=_git_revision(pack_path_p, base_branch) or base_branch,
    )
    verified_delivery.update(
        run_dir,
        {"pull_request": {"target": base_branch}},
    )

    preview_mode = _resolve_preview_mode()
    proof_mode = _resolve_proof_mode()
    preview_note = _preview_note(preview_mode, run_dir, _demo_video_requested())

    # Shared-branch epic mode: the worker branches off the epic tip and pushes
    # without opening a PR; the flow integrates on pass. Empty directive in the
    # default per-child-PR mode keeps the worker prompt byte-for-byte unchanged.
    shared_mode = bool(epic_branch)
    if shared_mode and not dry_run:
        ancestry_preflight = shared_branch.preflight_child_ancestry(
            pack_path_p,
            epic_branch=epic_branch,
            child_id=issue_id,
        )
        (run_dir / "shared-branch-preflight.json").write_text(
            json.dumps(ancestry_preflight, indent=2) + "\n",
            encoding="utf-8",
        )
        verified_delivery.update(
            run_dir, {"shared_branch_preflight": ancestry_preflight}
        )
    worker_branch_directive = (
        shared_branch.branch_directive(epic_branch, issue_id) if shared_mode else ""
    )

    try:
        sizing_step = agent_step(
            agent_dir=_AGENTS_DIR / "agentic-sizer",
            task=_AGENTS_DIR / "agentic-sizer" / "task.md",
            seed_id=issue_id,
            rig_path=str(rig_path_p),
            run_dir=run_dir,
            step="sizing",
            iter_n=1,
            ctx={"pack_path": str(pack_path_p)},
            verdict_keywords=("proceed", "decompose", "failed"),
            dry_run=dry_run,
        )
        if dry_run and not (run_dir / agentic_sizing.SIZING_FILE).exists():
            # StubBackend exercises the real flow topology but cannot author an
            # artifact. Supply a clearly synthetic, structurally valid record.
            (run_dir / agentic_sizing.SIZING_FILE).write_text(
                json.dumps(
                    {
                        "decision": "proceed",
                        "size": "small",
                        "risk": "low",
                        "surfaces": ["dry-run topology"],
                        "iteration_budget": 1,
                        "rationale": "Synthetic dry-run sizing judgment.",
                        "decomposition_reason": "",
                    },
                    indent=2,
                )
                + "\n"
            )
        sizing = agentic_sizing.apply_operator_cap(
            agentic_sizing.read_sizing(run_dir), iter_cap
        )
        delivery_plan = agentic_sizing.apply_proof_mode(
            agentic_sizing.delivery_plan(sizing, demo_enabled=_demo_video_requested()),
            proof_mode,
        )
        logger.info(
            "agentic: sizing decision=%s size=%s risk=%s budget=%s closed_by=%s",
            sizing.decision,
            sizing.size,
            sizing.risk,
            sizing.iteration_budget,
            sizing_step.closed_by,
        )
        if not dry_run:
            _record_sizing_labels(issue_id, sizing, rig_path_p)
        verified_delivery.update(
            run_dir,
            {
                "sizing": {
                    **sizing.as_dict(),
                    "operator_iter_cap": iter_cap,
                    "provenance": provenance,
                },
                "delivery_plan": delivery_plan.as_dict(),
                "proof_mode": proof_mode,
                "changed_surfaces": list(sizing.surfaces),
                "live_verification": {"plan": delivery_plan.steps()},
            },
        )
        if sizing.decision == "decompose":
            raise agentic_sizing.DecompositionRequiredError(
                agentic_sizing.decomposition_message(issue_id, sizing)
            )

        critic_verdict = ""
        verifier_verdict = "not-required"
        fix_list = ""
        success = False
        for iter_n in range(1, sizing.iteration_budget + 1):
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
                    "base_branch": base_branch,
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

            # Transport gate: prompts tell the worker where to branch and where
            # to aim its PR; this proves what actually happened before semantic
            # judgment sees the change. A wrong base/head/preview never reaches
            # the critic as if it were valid delivery evidence.
            branch_evidence: dict[str, Any] = {}
            pr_evidence: dict[str, Any] | None = None
            preview_evidence: dict[str, Any] | None = None
            worker_branch = shared_branch.child_branch_name(issue_id)
            if not dry_run:
                branch_base = epic_branch if shared_mode else base_branch
                if shared_mode:
                    delivery_truth.require_ancestor(
                        pack_path_p,
                        base_branch,
                        epic_branch,
                        label="epic branch base mismatch",
                    )
                branch_evidence = delivery_truth.branch_truth(
                    pack_path_p,
                    branch=worker_branch,
                    base_branch=branch_base,
                )
                pr_evidence = delivery_truth.pull_request_truth(
                    pack_path_p,
                    head_branch=worker_branch,
                    target_branch=base_branch,
                )
                if shared_mode and pr_evidence is not None:
                    raise delivery_truth.DeliveryTruthError(
                        f"shared child {worker_branch} opened a forbidden per-child PR"
                    )
                preview_candidate = _read_text(run_dir / _PREVIEW_URL_FILE).strip()
                if preview_candidate:
                    if preview_mode != "local":
                        raise delivery_truth.DeliveryTruthError(
                            "preview identity proof currently requires PO_PREVIEW=local"
                        )
                    worker_repo = delivery_truth.worktree_for_branch(
                        pack_path_p, worker_branch
                    )
                    preview_evidence = delivery_truth.localhost_preview_truth(
                        preview_candidate.splitlines()[-1].strip(),
                        expected_repo=worker_repo,
                        expected_revision=branch_evidence["head_sha"],
                    )
                patch: dict[str, Any] = {
                    "revisions": {
                        "base": branch_evidence["base_sha"],
                        "head": branch_evidence["head_sha"],
                    },
                    "branch_truth": branch_evidence,
                }
                if pr_evidence is not None:
                    patch["pull_request"] = pr_evidence
                if preview_evidence is not None:
                    patch["preview"] = preview_evidence
                verified_delivery.update(run_dir, patch)

            learning_receipt = run_dir / _LEARNING_RECEIPT_FILE
            learning_receipt.unlink(missing_ok=True)
            review = agent_step(
                agent_dir=_AGENTS_DIR / "agentic-reviewer",
                task=_AGENTS_DIR / "agentic-reviewer" / "task.md",
                seed_id=issue_id,
                rig_path=str(rig_path_p),
                run_dir=run_dir,
                step="review",
                iter_n=iter_n,
                ctx={
                    "iter": iter_n,
                    "pack_path": str(pack_path_p),
                    "base_branch": base_branch,
                    "epic_branch": epic_branch or "",
                    "verified_delivery_path": str(
                        verified_delivery.artifact_path(run_dir)
                    ),
                    "branch_truth": json.dumps(branch_evidence, sort_keys=True),
                    "learning_receipt_path": str(learning_receipt),
                },
                verdict_keywords=("pass", "fail"),
                required_artifacts=(_LEARNING_RECEIPT_FILE,),
                artifact_nudge=(
                    f"Complete the learning receipt at {learning_receipt}. "
                    "Create an empty file if there are no reusable lessons."
                ),
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

            if critic_verdict != "pass":
                fix_list = _read_text(run_dir / f"critique-iter-{iter_n}.md")
                continue

            # The sizing model owns semantic classification. Python only
            # executes the resulting proof plan and validates verdict shape.
            _clear_phase_evidence(run_dir, delivery_plan)
            (run_dir / f"verification-report-iter-{iter_n}.md").unlink(missing_ok=True)
            if delivery_plan.deploy_smoke:
                smoke = agent_step(
                    agent_dir=_AGENTS_DIR / "deploy-smoke",
                    task=_AGENTS_DIR / "deploy-smoke" / "task.md",
                    seed_id=issue_id,
                    rig_path=str(rig_path_p),
                    run_dir=run_dir,
                    step="deploy-smoke",
                    iter_n=iter_n,
                    ctx={"issue_id": issue_id, "pack_path": str(pack_path_p)},
                    dry_run=dry_run,
                )
                _record_proof_result(
                    run_dir, step="deploy-smoke", iter_n=iter_n, result=smoke
                )
                smoke_failure = _proof_evidence_failure(
                    run_dir, step="deploy-smoke", iter_n=iter_n
                )
                if smoke_failure:
                    fix_list = smoke_failure
                    logger.info("agentic: iter %s %s", iter_n, smoke_failure)
                    continue
            if delivery_plan.demo:
                _clear_demo_evidence(run_dir)
                demo_error: Exception | None = None
                try:
                    demo = agent_step(
                        agent_dir=_AGENTS_DIR / "demo-video",
                        task=_AGENTS_DIR / "demo-video" / "task.md",
                        seed_id=issue_id,
                        rig_path=str(rig_path_p),
                        run_dir=run_dir,
                        step="demo-video",
                        iter_n=iter_n,
                        ctx={"issue_id": issue_id},
                        verdict_keywords=("recorded", "skipped", "failed"),
                        dry_run=dry_run,
                    )
                    _record_proof_result(
                        run_dir, step="demo-video", iter_n=iter_n, result=demo
                    )
                except Exception as exc:  # noqa: BLE001 - proof failure retries actor
                    demo_error = exc
                demo_failure = _demo_evidence_failure(
                    run_dir,
                    verdict="" if demo_error is not None else demo.verdict,
                    error=demo_error,
                )
                if demo_failure:
                    fix_list = demo_failure
                    logger.info(
                        "agentic: iter %s required demo failed: %s",
                        iter_n,
                        demo_failure,
                    )
                    continue
            if delivery_plan.review_artifacts:
                artifacts = agent_step(
                    agent_dir=_AGENTS_DIR / "review-artifacts",
                    task=_AGENTS_DIR / "review-artifacts" / "task.md",
                    seed_id=issue_id,
                    rig_path=str(rig_path_p),
                    run_dir=run_dir,
                    step="review-artifacts",
                    iter_n=iter_n,
                    ctx={"issue_id": issue_id},
                    dry_run=dry_run,
                )
                _record_proof_result(
                    run_dir,
                    step="review-artifacts",
                    iter_n=iter_n,
                    result=artifacts,
                )
                artifact_failure = _proof_evidence_failure(
                    run_dir,
                    step="review-artifacts",
                    iter_n=iter_n,
                    require_overview=bool(
                        {"workflow", "infrastructure"} & set(sizing.surface_types)
                    ),
                )
                if artifact_failure:
                    fix_list = artifact_failure
                    logger.info("agentic: iter %s %s", iter_n, artifact_failure)
                    continue
            if delivery_plan.live_verifier:
                verification = agent_step(
                    agent_dir=_AGENTS_DIR / "verifier",
                    task=_AGENTS_DIR / "verifier" / "task.md",
                    seed_id=issue_id,
                    rig_path=str(rig_path_p),
                    run_dir=run_dir,
                    step="verify",
                    iter_n=iter_n,
                    ctx={
                        "verify_iter": iter_n,
                        "prior_critique": fix_list,
                        "pack_path": str(pack_path_p),
                    },
                    verdict_keywords=("approved", "rejected"),
                    dry_run=dry_run,
                )
                verifier_verdict = "approved" if dry_run else verification.verdict
                _record_proof_result(
                    run_dir, step="verify", iter_n=iter_n, result=verification
                )
                logger.info(
                    "agentic: iter %s live-verifier=%s", iter_n, verifier_verdict
                )
                verification_failure = _proof_evidence_failure(
                    run_dir, step="verify", iter_n=iter_n
                )
                if verifier_verdict != "approved" or verification_failure:
                    report = _read_text(
                        run_dir / f"verification-report-iter-{iter_n}.md"
                    )
                    fix_list = (
                        verification_failure
                        or report
                        or verification.summary
                        or (
                            "Live verification rejected without a report. Reproduce "
                            "the live failure and provide complete evidence."
                        )
                    )
                    continue

            success = True
            break

        if not success:
            # Leave the seed open and raise for forensics — run_dir artifacts
            # (critiques, diffs, sessions) stay for `po retry` / inspection.
            raise RuntimeError(
                "software-dev-agentic: did not converge after "
                f"{sizing.iteration_budget} iter(s) — "
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
                wt = shared_branch.ensure_integration_worktree(pack_path_p, epic_for_wt)
                child_branch = shared_branch.child_branch_name(issue_id)
                with shared_branch.integration_lock(pack_path_p, epic_for_wt):
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
                    proven = delivery_truth.integration_truth(
                        wt,
                        child_branch=child_branch,
                        integration_branch=epic_branch,
                        base_branch=base_branch,
                    )
                    integration.update(proven)
                    logger.info(
                        "agentic: %s merged itself into %s", issue_id, epic_branch
                    )
                else:
                    raise delivery_truth.DeliveryTruthError(
                        f"merge-back did not integrate {child_branch} into {epic_branch}"
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

        # The flow runs from the pack's main checkout while the worker commits
        # in its isolated branch. HEAD here would report the unchanged rig.
        head_revision = _git_revision(pack_path_p, worker_branch)
        delivery_patch: dict[str, Any] = {
            "revisions": {"head": head_revision},
            "terminal": {"state": "completed", "reason": None},
        }
        if integration and integration.get("merged"):
            delivery_patch["revisions"]["integration"] = integration.get(
                "integration_sha"
            )
        if preview_url and preview_evidence:
            delivery_patch["preview"] = preview_evidence
        contract = verified_delivery.update(run_dir, delivery_patch)

        # Per-child-PR mode only: the worker's PR is open and the critic passed,
        # so announce the PR to po-director, which fires the PR Sheriff iff the
        # workspace is in an auto merge mode. Shared-branch children open no PR
        # of their own (the epic owns the single PR), so this is skipped for them.
        if not dry_run and not shared_mode:
            _dispatch_pr_sheriff(rig_path_p, issue_id, logger)

        return {
            "status": "completed",
            "critic_verdict": critic_verdict,
            "verifier_verdict": verifier_verdict,
            "delivery_plan": delivery_plan.as_dict(),
            "preview_url": preview_url,
            "integration": integration,
            "verified_delivery": contract,
        }
    except Exception as exc:
        try:
            verified_delivery.update(
                run_dir,
                {
                    "revisions": {
                        "head": _git_revision(
                            pack_path_p, shared_branch.child_branch_name(issue_id)
                        )
                    },
                    "terminal": {
                        "state": (
                            "rejected"
                            if isinstance(
                                exc, agentic_sizing.DecompositionRequiredError
                            )
                            else "failed"
                        ),
                        "reason": str(exc),
                    },
                },
            )
        except Exception as artifact_exc:  # noqa: BLE001 — preserve flow failure
            logger.error(
                "agentic: could not terminalize verified-delivery artifact (%s)",
                artifact_exc,
            )
        _record_flow_outcome(run_dir, exc, issue_id, str(rig_path_p))
        raise


__all__ = ["software_dev_agentic"]
