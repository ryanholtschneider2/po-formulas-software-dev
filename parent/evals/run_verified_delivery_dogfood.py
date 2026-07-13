"""Dogfood verified delivery across backend, UI, and shared-epic scenarios."""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import shutil
import socket
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from prefect_orchestration.agent_step import AgentStepResult

from po_formulas import agentic, agentic_epic, delivery_truth

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


@contextmanager
def _patch_attributes(
    patches: list[tuple[object, str, object]],
) -> Iterator[None]:
    originals = [(target, name, getattr(target, name)) for target, name, _ in patches]
    try:
        for target, name, replacement in patches:
            setattr(target, name, replacement)
        yield
    finally:
        for target, name, original in reversed(originals):
            setattr(target, name, original)


@contextmanager
def _environment(**values: str | None) -> Iterator[None]:
    previous = {name: os.environ.get(name) for name in values}
    try:
        for name, value in values.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _write_sizing(
    rig: Path,
    issue_id: str,
    *,
    surface_types: list[str],
    iteration_budget: int = 1,
) -> Path:
    run_dir = rig / ".planning" / "software-dev-agentic" / issue_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "sizing.json").write_text(
        json.dumps(
            {
                "decision": "proceed",
                "size": "small",
                "risk": "low",
                "surfaces": [f"scratch {surface_types[0]}"],
                "surface_types": surface_types,
                "iteration_budget": iteration_budget,
                "rationale": "Executable verified-delivery dogfood.",
                "decomposition_reason": "",
            },
            indent=2,
        )
        + "\n"
    )
    return run_dir


def _formula_patches(
    fake_step: Callable[..., AgentStepResult], closed: list[str]
) -> list[tuple[object, str, object]]:
    return [
        (agentic, "get_run_logger", lambda: logging.getLogger("dogfood.agentic")),
        (agentic, "agent_step", fake_step),
        (agentic, "claim_issue", lambda *args, **kwargs: None),
        (agentic, "close_issue", lambda issue, **kwargs: closed.append(issue)),
        (agentic, "_record_sizing_labels", lambda *args, **kwargs: None),
        (agentic, "_dispatch_pr_sheriff", lambda *args, **kwargs: None),
        (agentic, "_tag_flow_run_with_issue_id", lambda *args, **kwargs: None),
        (
            agentic.delivery_truth,
            "branch_truth",
            lambda *args, **kwargs: {
                "base_branch": kwargs["base_branch"],
                "base_sha": "base-sha",
                "head_branch": kwargs["branch"],
                "head_sha": "head-sha",
            },
        ),
        (agentic.delivery_truth, "pull_request_truth", lambda *args, **kwargs: None),
        (agentic, "_git_revision", lambda *args, **kwargs: "head-sha"),
    ]


def _run_formula_case(
    rig: Path,
    issue_id: str,
    fake_step: Callable[..., AgentStepResult],
    *,
    proof_mode: str,
    iteration_budget: int = 1,
) -> tuple[dict[str, Any], list[str]]:
    closed: list[str] = []
    with (
        _patch_attributes(_formula_patches(fake_step, closed)),
        _environment(
            PO_AGENTIC_PROOF_MODE=proof_mode,
            PO_DEMO_VIDEO="0",
            PO_PREVIEW="off",
            PO_RESUME=None,
        ),
    ):
        result = agentic.software_dev_agentic.fn(
            issue_id=issue_id,
            rig="scratch",
            rig_path=str(rig),
            pack_path=str(rig),
            iter_cap=iteration_budget,
            claim=True,
        )
    return result, closed


def run(output_dir: Path) -> dict[str, Any]:
    root = output_dir.resolve()
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    scenarios: list[dict[str, Any]] = []

    backend_rig = root / "backend-strict"
    backend_issue = "scratch-backend-strict"
    backend_run_dir = _write_sizing(backend_rig, backend_issue, surface_types=["code"])
    backend_trace: list[str] = []

    def backend_step(**kwargs: Any) -> AgentStepResult:
        step = str(kwargs["step"])
        backend_trace.append(step)
        if step == "review-artifacts":
            review_dir = backend_run_dir / "review-artifacts"
            review_dir.mkdir(parents=True, exist_ok=True)
            (review_dir / "summary.md").write_text("# Backend strict review\n")
        if step == "verify":
            (backend_run_dir / "verification-report-iter-1.md").write_text(
                "backend strict proof approved\n"
            )
        verdict = {"review": "pass", "verify": "approved"}.get(step, "complete")
        return AgentStepResult(
            bead_id=f"{backend_issue}-{step}", verdict=verdict, closed_by="agent"
        )

    backend, backend_closed = _run_formula_case(
        backend_rig, backend_issue, backend_step, proof_mode="strict"
    )
    scenarios.append(
        _record(
            "backend strict task",
            backend["status"] == "completed"
            and backend["delivery_plan"]
            == {
                "review_artifacts": True,
                "live_verifier": True,
                "deploy_smoke": False,
                "demo": False,
            }
            and "review-artifacts" in backend_trace
            and "verify" in backend_trace
            and "deploy-smoke" not in backend_trace
            and backend_closed == [backend_issue],
            "software_dev_agentic ran review-artifacts -> verify and emitted terminal evidence without deploy smoke",
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
    importlib.reload(agentic)
    importlib.reload(delivery_truth)

    shared = _fixture_repo(root / "shared-epic")
    _git(shared, "branch", "epic/scratch", "main")
    shared_issue = "child"
    shared_run_dir = _write_sizing(shared, shared_issue, surface_types=["code"])
    shared_trace: list[str] = []
    shared_closed: list[str] = []

    def shared_step(**kwargs: Any) -> AgentStepResult:
        step = str(kwargs["step"])
        shared_trace.append(step)
        if step == "agentic":
            _git(shared, "switch", "-c", "agentic-child", "epic/scratch")
            (shared / "child.txt").write_text("integrated through child flow\n")
            _git(shared, "add", "child.txt")
            _git(shared, "commit", "-m", "scratch child delivery")
        elif step == "review-artifacts":
            review_dir = shared_run_dir / "review-artifacts"
            review_dir.mkdir(parents=True, exist_ok=True)
            (review_dir / "summary.md").write_text("# Shared child review\n")
        elif step == "verify":
            (shared_run_dir / "verification-report-iter-1.md").write_text(
                "shared child approved\n"
            )
        elif step == "merge-back":
            _git(shared, "switch", "epic/scratch")
            _git(shared, "merge", "--ff-only", "agentic-child")
        verdict = {
            "review": "pass",
            "verify": "approved",
            "merge-back": "merged",
        }.get(step, "complete")
        return AgentStepResult(
            bead_id=f"{shared_issue}-{step}", verdict=verdict, closed_by="agent"
        )

    @contextmanager
    def shared_lock(*args: Any, **kwargs: Any) -> Iterator[None]:
        yield

    shared_patches = [
        (agentic, "get_run_logger", lambda: logging.getLogger("dogfood.shared")),
        (agentic, "agent_step", shared_step),
        (agentic, "claim_issue", lambda *args, **kwargs: None),
        (
            agentic,
            "close_issue",
            lambda issue, **kwargs: shared_closed.append(issue),
        ),
        (agentic, "_record_sizing_labels", lambda *args, **kwargs: None),
        (agentic, "_tag_flow_run_with_issue_id", lambda *args, **kwargs: None),
        (agentic.delivery_truth, "pull_request_truth", lambda *args, **kwargs: None),
        (agentic.shared_branch, "ensure_integration_worktree", lambda *args: shared),
        (agentic.shared_branch, "integration_lock", shared_lock),
    ]
    with (
        _patch_attributes(shared_patches),
        _environment(
            PO_AGENTIC_PROOF_MODE="strict",
            PO_DEMO_VIDEO="0",
            PO_PREVIEW="off",
            PO_RESUME=None,
        ),
    ):
        shared_result = agentic.software_dev_agentic.fn(
            issue_id=shared_issue,
            rig="scratch",
            rig_path=str(shared),
            pack_path=str(shared),
            iter_cap=1,
            claim=True,
            base_branch="main",
            epic_branch="epic/scratch",
            parent_epic_id="scratch",
        )
    child_sha = _git(shared, "rev-parse", "agentic-child")
    manifest_dir = root / "shared-manifest"
    manifest_dir.mkdir()
    manifest = agentic_epic._build_acceptance_manifest(
        child_ids=["child"],
        dispatch={
            "results": {
                "child": {
                    "integration": shared_result["integration"],
                    "verified_delivery": shared_result["verified_delivery"],
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
            shared_result["status"] == "completed"
            and shared_result["integration"]["integration_sha"] == child_sha
            and shared_result["verified_delivery"]["terminal"]["state"] == "completed"
            and "merge-back" in shared_trace
            and shared_closed == [shared_issue]
            and not manifest["blocking_facts"],
            "shared child formula emitted verified-delivery, merged the real git branch, and assembled acceptance consumed it",
        )
    )

    smoke_rig = root / "red-smoke"
    smoke_issue = "scratch-red-smoke"
    smoke_run_dir = _write_sizing(smoke_rig, smoke_issue, surface_types=["api"])
    smoke_trace: list[str] = []

    def red_smoke_step(**kwargs: Any) -> AgentStepResult:
        step = str(kwargs["step"])
        smoke_trace.append(step)
        if step == "deploy-smoke":
            (smoke_run_dir / "smoke-test-output.txt").write_text(
                "SMOKE FAILED: injected\n"
            )
        verdict = "pass" if step == "review" else "complete"
        return AgentStepResult(
            bead_id=f"{smoke_issue}-{step}", verdict=verdict, closed_by="agent"
        )

    try:
        _run_formula_case(smoke_rig, smoke_issue, red_smoke_step, proof_mode="adaptive")
    except RuntimeError as exc:
        red_smoke_failed_closed = "did not converge" in str(exc)
    else:
        red_smoke_failed_closed = False
    smoke_artifact = json.loads((smoke_run_dir / "verified-delivery.json").read_text())
    scenarios.append(
        _record(
            "red smoke injection",
            red_smoke_failed_closed
            and smoke_trace[-1] == "deploy-smoke"
            and smoke_artifact["terminal"]["state"] == "failed",
            "success-shaped deploy role wrote explicit red evidence and software_dev_agentic failed closed",
        )
    )

    missing_rig = root / "missing-artifact"
    missing_issue = "scratch-missing-artifact"
    missing_run_dir = _write_sizing(missing_rig, missing_issue, surface_types=["code"])
    missing_trace: list[str] = []

    def missing_step(**kwargs: Any) -> AgentStepResult:
        step = str(kwargs["step"])
        missing_trace.append(step)
        verdict = "pass" if step == "review" else "complete"
        return AgentStepResult(
            bead_id=f"{missing_issue}-{step}", verdict=verdict, closed_by="agent"
        )

    try:
        _run_formula_case(missing_rig, missing_issue, missing_step, proof_mode="strict")
    except RuntimeError as exc:
        missing_failed_closed = "did not converge" in str(exc)
    else:
        missing_failed_closed = False
    missing_artifact = json.loads(
        (missing_run_dir / "verified-delivery.json").read_text()
    )
    scenarios.append(
        _record(
            "missing artifact injection",
            missing_failed_closed
            and missing_trace[-1] == "review-artifacts"
            and missing_artifact["terminal"]["state"] == "failed",
            "success-shaped packaging role omitted summary and software_dev_agentic failed closed before verify",
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

    resume_rig = root / "resume"
    resume_issue = "scratch-resume"
    resume_run_dir = _write_sizing(resume_rig, resume_issue, surface_types=["api"])
    resume_trace: list[tuple[int, str]] = []
    resume_attempt = 1
    resume_closed: list[str] = []
    session_payload = '{"scratch-resume.verify.1":"session-kept"}\n'

    def resume_step(**kwargs: Any) -> AgentStepResult:
        nonlocal resume_attempt
        step = str(kwargs["step"])
        resume_trace.append((resume_attempt, step))
        if step == "sizing" and resume_attempt == 1:
            (resume_run_dir / "role-sessions.json").write_text(session_payload)
            (resume_run_dir / "iter-bead-ids.json").write_text("{}\n")
        elif step == "deploy-smoke":
            (resume_run_dir / "smoke-test-output.txt").write_text(
                f"attempt {resume_attempt}: healthy\n"
            )
        elif step == "review-artifacts":
            review_dir = resume_run_dir / "review-artifacts"
            review_dir.mkdir(parents=True, exist_ok=True)
            (review_dir / "summary.md").write_text(
                f"# Resume package attempt {resume_attempt}\n"
            )
        elif step == "verify" and resume_attempt == 1:
            raise RuntimeError("injected worker stop before verifier verdict")
        elif step == "verify":
            (resume_run_dir / "verification-report-iter-1.md").write_text(
                "resumed live proof approved\n"
            )
        verdict = {"review": "pass", "verify": "approved"}.get(step, "complete")
        return AgentStepResult(
            bead_id=f"{resume_issue}-{step}", verdict=verdict, closed_by="agent"
        )

    resume_patches = _formula_patches(resume_step, resume_closed)
    with (
        _patch_attributes(resume_patches),
        _environment(
            PO_AGENTIC_PROOF_MODE="strict",
            PO_DEMO_VIDEO="0",
            PO_PREVIEW="off",
            PO_RESUME=None,
        ),
    ):
        try:
            agentic.software_dev_agentic.fn(
                issue_id=resume_issue,
                rig="scratch",
                rig_path=str(resume_rig),
                pack_path=str(resume_rig),
                iter_cap=1,
                claim=True,
            )
        except RuntimeError as exc:
            stopped = "injected worker stop" in str(exc)
        else:
            stopped = False
        stopped_artifact = json.loads(
            (resume_run_dir / "verified-delivery.json").read_text()
        )
        resume_attempt = 2
        with _environment(PO_RESUME="1"):
            resumed_result = agentic.software_dev_agentic.fn(
                issue_id=resume_issue,
                rig="scratch",
                rig_path=str(resume_rig),
                pack_path=str(resume_rig),
                iter_cap=1,
                claim=True,
            )
    scenarios.append(
        _record(
            "stopped and resumed run",
            stopped
            and stopped_artifact["terminal"]["state"] == "failed"
            and (resume_run_dir / "role-sessions.json").read_text() == session_payload
            and not list(resume_run_dir.parent.glob(f"{resume_issue}.bak-*"))
            and (2, "verify") in resume_trace
            and resumed_result["status"] == "completed"
            and resumed_result["verified_delivery"]["terminal"]["state"] == "completed"
            and resume_closed == [resume_issue],
            "injected stop terminalized failed; PO_RESUME preserved session state and completed smoke -> package -> verify",
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
