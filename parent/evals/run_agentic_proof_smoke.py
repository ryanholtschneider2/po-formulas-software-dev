"""Exercise the decorated agentic flow's live-proof reject/retry topology."""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
from typing import Any

from prefect_orchestration.agent_step import AgentStepResult

from po_formulas import agentic


def run(output_dir: Path) -> dict[str, Any]:
    rig = output_dir.resolve()
    issue_id = "scratch-agentic-live-proof"
    run_dir = rig / ".planning" / "software-dev-agentic" / issue_id
    review_dir = run_dir / "review-artifacts"
    review_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "sizing.json").write_text(
        json.dumps(
            {
                "decision": "proceed",
                "size": "medium",
                "risk": "medium",
                "surfaces": ["scratch UI", "runtime"],
                "surface_types": ["ui"],
                "iteration_budget": 2,
                "rationale": "Exercise every adaptive proof phase.",
                "decomposition_reason": "",
            },
            indent=2,
        )
        + "\n"
    )

    trace: list[dict[str, Any]] = []
    verification_count = 0
    closed: list[str] = []

    def fake_step(**kwargs: Any) -> AgentStepResult:
        nonlocal verification_count
        step = str(kwargs["step"])
        iteration = int(kwargs.get("iter_n", 1))
        trace.append(
            {
                "step": step,
                "iteration": iteration,
                "revision_note": kwargs.get("ctx", {}).get("revision_note", ""),
            }
        )
        verdict = "complete"
        if step == "review":
            verdict = "pass"
        elif step == "deploy-smoke":
            (run_dir / "smoke-test-output.txt").write_text(
                f"iteration {iteration}: scratch service healthy\n"
            )
        elif step == "demo-video":
            verdict = "recorded"
            demo = review_dir / "demo.mp4"
            demo.write_bytes(f"scratch demo iteration {iteration}".encode())
            (run_dir / "demo.mp4").write_bytes(demo.read_bytes())
        elif step == "review-artifacts":
            (review_dir / "summary.md").write_text(
                f"# Scratch proof package\n\nIteration {iteration}.\n"
            )
        elif step == "verify":
            verification_count += 1
            verdict = "rejected" if verification_count == 1 else "approved"
            (run_dir / f"verification-report-iter-{iteration}.md").write_text(
                "forced live rejection: retry the complete proof chain\n"
                if verdict == "rejected"
                else "live proof approved after rerun\n"
            )
        return AgentStepResult(
            bead_id=f"{issue_id}-{step}-iter{iteration}",
            verdict=verdict,
            closed_by="agent",
        )

    agentic.agent_step = fake_step
    agentic.claim_issue = lambda *args, **kwargs: None
    agentic.close_issue = lambda issue, **kwargs: closed.append(issue)
    agentic._record_sizing_labels = lambda *args, **kwargs: None
    agentic._dispatch_pr_sheriff = lambda *args, **kwargs: None
    agentic._tag_flow_run_with_issue_id = lambda *args, **kwargs: None
    agentic.delivery_truth.branch_truth = lambda *args, **kwargs: {
        "base_branch": "main",
        "base_sha": "base-sha",
        "head_branch": "agentic-scratch-agentic-live-proof",
        "head_sha": "head-sha",
    }
    agentic.delivery_truth.pull_request_truth = lambda *args, **kwargs: None
    agentic._git_revision = lambda *args, **kwargs: "head-sha"
    os.environ["PO_DEMO_VIDEO"] = "1"
    os.environ["PO_PREVIEW"] = "off"

    result = agentic.software_dev_agentic(
        issue_id=issue_id,
        rig="scratch",
        rig_path=str(rig),
        pack_path=str(Path.cwd()),
        iter_cap=2,
        claim=True,
    )
    evidence = {
        "result": result,
        "closed": closed,
        "trace": trace,
        "verified_delivery": json.loads(
            (run_dir / "verified-delivery.json").read_text()
        ),
    }
    (review_dir / "proof-smoke.json").write_text(json.dumps(evidence, indent=2) + "\n")
    return evidence


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    logging.getLogger("prefect").setLevel(logging.WARNING)
    evidence = run(args.output_dir)
    print(" -> ".join(item["step"] for item in evidence["trace"]))
    print(evidence["result"]["status"])


if __name__ == "__main__":
    main()
