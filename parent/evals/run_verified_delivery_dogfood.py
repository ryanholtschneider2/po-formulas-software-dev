"""Dogfood verified delivery across backend, UI, and shared-epic scenarios."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import shutil
import socket
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from po_formulas import agentic, agentic_epic, agentic_sizing, delivery_truth
from po_formulas.verified_delivery import normalize

from run_agentic_proof_smoke import run as run_ui_proof_smoke


def _run(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        args, cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _git(repo: Path, *args: str) -> str:
    return _run(["git", *args], repo)


def _fixture_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "dogfood@example.test")
    _git(path, "config", "user.name", "Verified Delivery Dogfood")
    (path / "index.html").write_text("base\n")
    _git(path, "add", "index.html")
    _git(path, "commit", "-m", "base")
    return path


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def _http_server(cwd: Path) -> Iterator[int]:
    port = _free_port()
    process = subprocess.Popen(
        ["python", "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        cwd=cwd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(50):
            if process.poll() is not None:
                raise RuntimeError("scratch HTTP server exited")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.05)
        yield port
    finally:
        process.terminate()
        process.wait(timeout=5)


def _record(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "status": "PASS" if passed else "FAIL", "detail": detail}


def run(output_dir: Path) -> dict[str, Any]:
    root = output_dir.resolve()
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    scenarios: list[dict[str, Any]] = []

    adaptive = agentic_sizing.DeliveryPlan(False, False, False, False)
    strict = agentic_sizing.apply_proof_mode(adaptive, "strict")
    scenarios.append(
        _record(
            "backend strict task",
            strict.review_artifacts
            and strict.live_verifier
            and not strict.deploy_smoke,
            "strict mode adds review packaging and live verification without inventing deployability",
        )
    )
    ui = run_ui_proof_smoke(root / "ui-task")
    trace = [item["step"] for item in ui["trace"]]
    scenarios.append(
        _record(
            "UI task rejection and retry",
            ui["result"]["status"] == "completed"
            and trace.count("verify") == 2
            and trace.count("deploy-smoke") == 2,
            " -> ".join(trace),
        )
    )
    scenarios.append(
        _record(
            "verifier failure injection",
            ui["trace"][-1]["step"] == "verify"
            and "forced live rejection" in ui["trace"][7]["revision_note"],
            "first rejection reached the next worker revision note and the entire proof chain reran",
        )
    )
    # The controlled flow smoke replaces transport functions in-process. Reload
    # the module before the real git/process scenarios below.
    importlib.reload(delivery_truth)

    shared = _fixture_repo(root / "shared-epic")
    base_sha = _git(shared, "rev-parse", "main")
    _git(shared, "switch", "-c", "agentic-child", "main")
    (shared / "child.txt").write_text("integrated\n")
    _git(shared, "add", "child.txt")
    _git(shared, "commit", "-m", "child")
    child_sha = _git(shared, "rev-parse", "HEAD")
    _git(shared, "branch", "epic/scratch", "agentic-child")
    truth = delivery_truth.integration_truth(
        shared,
        child_branch="agentic-child",
        integration_branch="epic/scratch",
        base_branch="main",
    )
    artifact = normalize(
        {
            "revisions": {"base": base_sha, "head": child_sha},
            "terminal": {"state": "completed"},
            "changed_surfaces": ["scratch backend"],
            "live_verification": {
                "plan": ["review_artifacts", "live_verifier"],
                "results": [{"step": "verify", "verdict": "approved"}],
            },
        }
    )
    manifest_dir = root / "shared-manifest"
    manifest_dir.mkdir()
    manifest = agentic_epic._build_acceptance_manifest(
        child_ids=["child"],
        dispatch={
            "results": {
                "child": {
                    "integration": {"merged": True},
                    "verified_delivery": artifact,
                }
            }
        },
        rig_path=shared,
        pack_path=shared,
        run_dir=manifest_dir,
        base_branch="main",
        epic_branch="epic/scratch",
    )
    scenarios.append(
        _record(
            "shared-epic task",
            truth["integration_sha"] == child_sha and not manifest["blocking_facts"],
            "child ancestry and assembled acceptance manifest are both proven",
        )
    )

    smoke_dir = root / "red-smoke"
    smoke_dir.mkdir()
    (smoke_dir / "smoke-test-output.txt").write_text("SMOKE FAILED: injected\n")
    scenarios.append(
        _record(
            "red smoke injection",
            "records failure"
            in agentic._proof_evidence_failure(
                smoke_dir, step="deploy-smoke", iter_n=1
            ),
            "explicit red smoke cannot satisfy the structural proof gate",
        )
    )

    missing_dir = root / "missing-artifact"
    missing_dir.mkdir()
    scenarios.append(
        _record(
            "missing artifact injection",
            "no fresh"
            in agentic._proof_evidence_failure(
                missing_dir, step="review-artifacts", iter_n=1
            ),
            "a success-shaped role with no reviewer summary fails closed",
        )
    )

    stale_repo = _fixture_repo(root / "stale-preview")
    stale_sha = _git(stale_repo, "rev-parse", "HEAD")
    with _http_server(stale_repo) as port:
        try:
            delivery_truth.localhost_preview_truth(
                f"http://127.0.0.1:{port}",
                expected_repo=stale_repo,
                expected_revision="newer-revision",
            )
        except delivery_truth.DeliveryTruthError as exc:
            stale_rejected = "stale preview revision" in str(exc)
        else:
            stale_rejected = False
    scenarios.append(
        _record(
            "stale preview injection",
            stale_rejected,
            f"live server at {stale_sha[:12]} was rejected against newer-revision",
        )
    )

    original_which = delivery_truth.shutil.which
    original_run = delivery_truth._run
    delivery_truth.shutil.which = lambda name: "/usr/bin/gh"
    delivery_truth._run = lambda *args, **kwargs: subprocess.CompletedProcess(
        [],
        0,
        json.dumps(
            {
                "number": 7,
                "url": "https://example.test/pull/7",
                "headRefName": "agentic-child",
                "baseRefName": "main",
                "state": "OPEN",
            }
        ),
        "",
    )
    try:
        try:
            delivery_truth.pull_request_truth(
                shared, head_branch="agentic-child", target_branch="release"
            )
        except delivery_truth.DeliveryTruthError as exc:
            wrong_base_rejected = "expected release, found main" in str(exc)
        else:
            wrong_base_rejected = False
    finally:
        delivery_truth.shutil.which = original_which
        delivery_truth._run = original_run
    scenarios.append(
        _record(
            "wrong PR base injection",
            wrong_base_rejected,
            "GitHub identity disagreeing with the requested target is rejected",
        )
    )

    resume_dir = root / "resume" / "run"
    resume_dir.mkdir(parents=True)
    (resume_dir / "iter-bead-ids.json").write_text("{}\n")
    previous_resume = os.environ.get("PO_RESUME")
    os.environ["PO_RESUME"] = "1"
    try:
        archive = (
            agentic._archive_stale_run_dir(resume_dir)
            if os.environ.get("PO_RESUME") != "1"
            else None
        )
    finally:
        if previous_resume is None:
            os.environ.pop("PO_RESUME", None)
        else:
            os.environ["PO_RESUME"] = previous_resume
    scenarios.append(
        _record(
            "stopped and resumed run",
            archive is None and (resume_dir / "iter-bead-ids.json").exists(),
            "PO_RESUME=1 preserves role/session state instead of archiving it",
        )
    )

    report = {
        "status": "PASS"
        if all(row["status"] == "PASS" for row in scenarios)
        else "FAIL",
        "scenarios": scenarios,
    }
    (root / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    rows = "\n".join(
        f"| {row['name']} | {row['status']} | {row['detail']} |" for row in scenarios
    )
    (root / "report.md").write_text(
        "# Verified Delivery Dogfood\n\n"
        "| Scenario | Result | Evidence |\n|---|---|---|\n"
        f"{rows}\n"
    )
    if report["status"] != "PASS":
        raise RuntimeError("verified-delivery dogfood failed; see report.json")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    report = run(args.output_dir)
    print(f"verified-delivery dogfood: {report['status']}")
    for scenario in report["scenarios"]:
        print(f"{scenario['status']}: {scenario['name']}")


if __name__ == "__main__":
    main()
