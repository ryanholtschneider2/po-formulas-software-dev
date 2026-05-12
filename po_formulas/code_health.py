"""Prefect flow: ``code-health-review``.

Periodically scans a rig's codebase for tech debt — dead code, oversized
modules, duplicated subsystems, stale docs, YAGNI smells — and files
each finding as a child bead under a new epic. Once filed, dispatches
the epic so PO fans the cleanup tasks out via ``software-dev-{edit,fast,full}``
per the reviewer's per-finding formula choice.

Pipeline::

    [synthesize seed bead]
        → agent_step(code-health-reviewer)         (writes proposals.json)
        → file beads + create epic + stamp formula  (deterministic Python)
        → run_deployment(epic-manual)               (async fan-out)
        → close seed bead

Key design choices:

- **Reviewer is read-only.** The agent_step turn never edits code or
  commits — every finding becomes a bead, not a patch.
- **Each child cleanup runs on its own branch.** The bead description
  the orchestrator stamps tells the builder to ``git checkout -b
  code-health/<bead-id>`` in the affected repo before editing. Polyrepo-
  friendly: each finding declares ``affected_repo`` and the builder
  ``cd``s there first.
- **Auto-dispatch is async.** We schedule the ``epic`` flow via
  ``run_deployment`` so the code-health flow itself returns quickly
  and the cleanup epic runs on a worker.
- **Cron-friendly.** No triggering bead required — the flow synthesizes
  one per run (engdocs/agent-step-adoption.md). Ship a paused weekly
  cron deployment so users opt in by unpausing.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from prefect import flow, get_run_logger
from prefect_orchestration.agent_step import agent_step
from prefect_orchestration.beads_meta import (
    claim_issue,
    close_issue,
    create_child_bead,
)


_AGENTS_DIR = Path(__file__).parent / "agents"
_VALID_FORMULAS = {"software-dev-edit", "software-dev-fast", "software-dev-full"}
_BRANCH_PREFIX = "code-health"


def _utc_slug() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _bd_create_seed(
    rig_path: Path,
    requested_id: str,
    title: str,
    description: str,
) -> str:
    """Create a synthesized seed bead, falling back to bd auto-id on prefix mismatch.

    Per engdocs/agent-step-adoption.md: some bd configs reject custom
    ``--id=`` and require a fixed prefix. Handle both. Idempotent on
    "already exists".
    """
    cmd = [
        "bd", "create",
        f"--id={requested_id}",
        "--title", title,
        "--description", description,
        "--type", "epic",
        "-p", "3",
    ]
    proc = subprocess.run(cmd, cwd=str(rig_path), capture_output=True, text=True, check=False)
    stderr = (proc.stderr or "") + (proc.stdout or "")
    if proc.returncode == 0:
        return requested_id
    if "already exists" in stderr.lower():
        return requested_id
    if "prefix" in stderr.lower() or "invalid" in stderr.lower():
        # Retry without --id; parse assigned id from stdout.
        retry = subprocess.run(
            ["bd", "create", "--title", title, "--description", description,
             "--type", "epic", "-p", "3"],
            cwd=str(rig_path), capture_output=True, text=True, check=False,
        )
        if retry.returncode != 0:
            raise RuntimeError(f"bd create (auto-id retry) failed: {retry.stderr}")
        for line in (retry.stdout or "").splitlines():
            if line.startswith("Created "):
                return line.split()[1].rstrip(":")
        raise RuntimeError(f"bd create succeeded but stdout unparseable: {retry.stdout!r}")
    raise RuntimeError(f"bd create {requested_id} failed: {stderr.strip()}")


def _bd_set_metadata(bead_id: str, key: str, value: str, rig_path: Path) -> None:
    subprocess.run(
        ["bd", "update", bead_id, "--set-metadata", f"{key}={value}"],
        cwd=str(rig_path), check=False, capture_output=True, text=True,
    )


def _build_child_description(finding: dict[str, Any], parent_id: str) -> str:
    """Compose the bead description the downstream builder reads.

    Bakes in the per-finding 'first action: branch' instruction so each
    cleanup commits on its own branch.
    """
    affected_repo = finding.get("affected_repo") or "."
    affected_paths = finding.get("affected_paths") or []
    branch_name = f"{_BRANCH_PREFIX}/{finding.get('_bead_id', 'unknown')}"
    description = finding.get("description", "").strip() or "(no description provided)"
    evidence_lines = finding.get("evidence") or []

    lines = [
        f"# {finding.get('title', '(untitled)')}",
        "",
        f"**Parent epic:** `{parent_id}`",
        f"**Severity:** {finding.get('severity', 'p3')}",
        f"**Kind:** {finding.get('kind', 'other')}",
        "",
        "## First action — work on a fresh branch",
        "",
        f"Before editing, `cd` into the affected repo and create the branch:",
        "",
        "```bash",
        f"cd <rig_path>/{affected_repo}",
        f"git checkout -b {branch_name}",
        "```",
        "",
        "All commits for this task must land on that branch. The plan / build agents will pick this up automatically.",
        "",
        "## Affected files",
        "",
    ]
    if affected_paths:
        for path in affected_paths:
            lines.append(f"- `{path}`")
    else:
        lines.append("(none specified — discover during planning)")
    lines += [
        "",
        "## Description",
        "",
        description,
        "",
    ]
    if evidence_lines:
        lines.append("## Evidence (from code-health review)")
        lines.append("")
        for ev in evidence_lines:
            lines.append(f"- {ev}")
        lines.append("")
    return "\n".join(lines)


def _file_finding_beads(
    rig_path: Path,
    epic_id: str,
    findings: list[dict[str, Any]],
    logger: Any,
) -> list[dict[str, Any]]:
    """Create one child bead per finding under ``epic_id``. Mutates findings to record bead_id.

    Returns the list of findings actually filed (skips ones with an
    ``existing_bead`` reference).
    """
    filed: list[dict[str, Any]] = []
    for idx, finding in enumerate(findings, start=1):
        if finding.get("existing_bead"):
            logger.info(
                "code-health: skipping finding %d — existing bead %s",
                idx, finding["existing_bead"],
            )
            continue

        formula = finding.get("formula", "software-dev-fast")
        if formula not in _VALID_FORMULAS:
            logger.warning(
                "code-health: finding %d had invalid formula %r; falling back to software-dev-fast",
                idx, formula,
            )
            formula = "software-dev-fast"

        # bd auto-IDs the child; we ask for a deterministic-ish slug so
        # branch names stay readable, but tolerate prefix-mismatch.
        requested_id = f"{epic_id}.{idx}"
        finding["_bead_id"] = requested_id
        description = _build_child_description(finding, epic_id)
        try:
            actual_id = create_child_bead(
                parent_id=epic_id,
                child_id=requested_id,
                title=finding.get("title", f"code-health finding {idx}")[:120],
                description=description,
                issue_type="task",
                priority={"p1": 1, "p2": 2, "p3": 3}.get(finding.get("severity"), 3),
                rig_path=rig_path,
            )
        except Exception as exc:
            logger.exception("code-health: failed to create child bead for finding %d: %s", idx, exc)
            continue

        finding["_bead_id"] = actual_id
        if actual_id != requested_id:
            # Prefix-mismatch fallback: bd reassigned. Re-stamp description so the
            # branch name in the body matches the actual id.
            description = _build_child_description(finding, epic_id)
            subprocess.run(
                ["bd", "update", actual_id, "--description", description],
                cwd=str(rig_path), check=False, capture_output=True, text=True,
            )

        _bd_set_metadata(actual_id, "formula", formula, rig_path)
        _bd_set_metadata(actual_id, "code_health.parent_epic", epic_id, rig_path)
        filed.append(finding)
        logger.info("code-health: filed %s (%s) → %s", actual_id, formula, finding.get("title", "")[:60])

    return filed


def _dispatch_epic(
    epic_id: str,
    rig: str,
    rig_path: Path,
    logger: Any,
    auto_dispatch: bool,
) -> str | None:
    """Schedule the ``epic`` flow against ``epic_id``. Returns flow_run_id or None."""
    if not auto_dispatch:
        logger.info("code-health: auto_dispatch=False; skipping epic dispatch (epic %s ready)", epic_id)
        return None

    try:
        from prefect.deployments import run_deployment  # type: ignore[import-not-found]
    except ImportError:
        logger.warning("code-health: prefect.deployments.run_deployment unavailable; skipping dispatch")
        return None

    deployment_name = "epic_run/epic-manual"
    try:
        flow_run = run_deployment(
            name=deployment_name,
            parameters={"epic_id": epic_id, "rig": rig, "rig_path": str(rig_path)},
            timeout=0,  # don't block this flow on the epic finishing
            as_subflow=False,
        )
        run_id = getattr(flow_run, "id", None) or str(flow_run)
        logger.info("code-health: dispatched epic %s via %s (flow_run=%s)", epic_id, deployment_name, run_id)
        return str(run_id) if run_id else None
    except Exception as exc:
        logger.exception(
            "code-health: failed to dispatch %s — apply the deployment first "
            "(`po deploy --apply`) and ensure a worker is running. Error: %s",
            deployment_name, exc,
        )
        return None


@flow(name="code_health_review", flow_run_name="{rig}-{report_slug}", log_prints=True)
def code_health_review(
    rig: str,
    rig_path: str,
    pack_path: str | None = None,
    auto_dispatch: bool = True,
    max_findings: int = 25,
    report_slug: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Scan a rig for tech debt, file beads, and dispatch the cleanup epic.

    Parameters
    ----------
    rig
        Rig name (display only).
    rig_path
        Absolute path to the rig where ``bd`` lives. Code edits land
        wherever each finding's ``affected_repo`` points (polyrepo
        friendly).
    pack_path
        Optional explicit path the reviewer should treat as the code
        root if different from ``rig_path``.
    auto_dispatch
        When True (default), schedule the ``epic`` deployment for the
        filed cleanup epic so PO fans children out automatically. When
        False, file beads + return; user dispatches manually with
        ``po run epic --epic-id <id>``.
    max_findings
        Cap on findings filed per run. Reviewer guidance is 5–25;
        anything beyond is noise. Excess findings are dropped (logged).
    report_slug
        UTC-timestamp slug used in flow_run_name + seed bead id. Auto-
        generated when omitted.
    dry_run
        Skip the agent turn (StubBackend) and bead-filing. Returns an
        empty payload — useful for wiring smoke tests.
    """
    logger = get_run_logger()
    rig_path_p = Path(rig_path).expanduser().resolve()
    slug = report_slug or _utc_slug()

    if dry_run:
        logger.info("code-health: dry_run=True; short-circuiting before agent_step")
        return {"status": "dry-run", "epic_id": None, "findings": [], "dispatched_run_id": None}

    # 1. Synthesize seed bead. bd needs a real parent for create_child_bead later.
    requested_seed = f"code-health-{slug}"
    seed_title = f"Code-health review: {rig} ({slug})"
    seed_description = (
        f"Auto-created by `code-health-review` flow for rig `{rig}` "
        f"({rig_path_p}). Children are individual cleanup tasks filed "
        f"by the code-health reviewer agent. Each child has "
        f"`metadata.formula` set so `po run epic` dispatches it through "
        f"the right pipeline."
    )
    seed_id = _bd_create_seed(rig_path_p, requested_seed, seed_title, seed_description)
    logger.info("code-health: seed bead = %s", seed_id)
    claim_issue(seed_id, assignee=f"po-{os.getpid()}", rig_path=rig_path_p)

    # 2. Run the reviewer agent. Writes proposals.json + verdicts/code-health-review.json.
    result = agent_step(
        agent_dir=_AGENTS_DIR / "code-health-reviewer",
        task=_AGENTS_DIR / "code-health-reviewer" / "task.md",
        seed_id=seed_id,
        rig_path=str(rig_path_p),
        step="code-health-review",
        iter_n=1,
        ctx={
            "rig": rig,
            "pack_path": pack_path or str(rig_path_p),
        },
        verdict_keywords=("complete", "failed"),
        dry_run=False,
    )
    if result.verdict != "complete":
        close_issue(seed_id, notes=f"code-health review failed: {result.summary}", rig_path=rig_path_p)
        raise RuntimeError(f"code-health-reviewer did not converge: {result.summary or '(no summary)'}")

    # 3. Read proposals.
    run_dir = Path(result.run_dir) if getattr(result, "run_dir", None) else (
        rig_path_p / ".planning" / "agent-step" / seed_id
    )
    proposals_path = run_dir / "proposals.json"
    if not proposals_path.exists():
        close_issue(seed_id, notes="code-health: no proposals.json", rig_path=rig_path_p)
        raise RuntimeError(f"code-health: reviewer closed without writing {proposals_path}")
    proposals = json.loads(proposals_path.read_text())
    findings: list[dict[str, Any]] = list(proposals.get("findings", []))
    if max_findings and len(findings) > max_findings:
        logger.warning(
            "code-health: reviewer returned %d findings; capping at %d",
            len(findings), max_findings,
        )
        findings = findings[:max_findings]

    if not findings:
        logger.info("code-health: zero filable findings; closing seed cleanly")
        close_issue(seed_id, notes="code-health: no findings", rig_path=rig_path_p)
        return {"status": "no-findings", "epic_id": seed_id, "findings": [], "dispatched_run_id": None}

    # 4. Use the seed bead itself as the cleanup epic — children attach via parent-child.
    filed_findings = _file_finding_beads(rig_path_p, seed_id, findings, logger)

    # 5. Dispatch the epic. epic_run walks parent-child deps regardless of the
    # epic's own status, so leaving the seed open while children are pending
    # is safe and avoids confusion if the user wants to inspect with `bd show`.
    dispatched_run_id = _dispatch_epic(seed_id, rig, rig_path_p, logger, auto_dispatch)

    # 6. If auto-dispatched, close the seed so it doesn't loiter in `bd ready`.
    # Otherwise leave it open with a one-line dispatch hint in the description.
    if dispatched_run_id is not None:
        close_issue(
            seed_id,
            notes=(
                f"code-health review filed {len(filed_findings)} cleanup beads "
                f"(of {len(findings)} findings); dispatched={dispatched_run_id}"
            ),
            rig_path=rig_path_p,
        )
    else:
        hint = (
            f"\n\n## Dispatch (manual)\n\n"
            f"Filed {len(filed_findings)} cleanup beads. To fan out:\n\n"
            f"```bash\npo run epic --epic-id {seed_id} --rig {rig} --rig-path {rig_path_p}\n```\n"
        )
        subprocess.run(
            ["bd", "update", seed_id, "--description-append", hint],
            cwd=str(rig_path_p), check=False, capture_output=True, text=True,
        )

    return {
        "status": "ok",
        "epic_id": seed_id,
        "report_slug": slug,
        "report_dir": str(run_dir),
        "findings_count": len(findings),
        "filed_count": len(filed_findings),
        "filed_beads": [f.get("_bead_id") for f in filed_findings if f.get("_bead_id")],
        "dispatched_run_id": dispatched_run_id,
    }
