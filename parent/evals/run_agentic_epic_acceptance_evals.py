"""Behavioral and formula smoke evals for assembled epic acceptance.

The harness uses scratch git repositories and the shipped acceptance role. It
never installs the pack into the operator's shared ``po`` environment and never
creates a real PR or persistent bead.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from po_formulas import agentic_epic as ae

HERE = Path(__file__).parent
CASES_PATH = HERE / "agentic-epic-acceptance-cases.json"
AGENT_DIR = Path(ae.__file__).parent / "agents" / "agentic-epic-acceptance-critic"


def _run(args: list[str], cwd: Path, *, input_text: str | None = None) -> str:
    result = subprocess.run(
        args,
        cwd=cwd,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{' '.join(args)} failed ({result.returncode}):\n{result.stderr[-2000:]}"
        )
    return result.stdout.strip()


def _git(repo: Path, *args: str) -> str:
    return _run(["git", *args], repo)


def _write_case_surface(repo: Path, case_id: str) -> None:
    if case_id == "complete":
        smoke = repo / "smoke.sh"
        smoke.write_text("#!/bin/sh\necho 'CUSTOMER FLOW PASS'\n")
        smoke.chmod(smoke.stat().st_mode | stat.S_IXUSR)
    elif case_id == "foundation-only":
        (repo / "database.sql").write_text("CREATE TABLE onboarding (id INTEGER);\n")
        (repo / "api-schema.json").write_text('{"onboarding": true}\n')
    elif case_id == "missing-ui":
        (repo / "api.py").write_text("def create_record():\n    return {'id': 1}\n")
    elif case_id == "unintegrated-child":
        (repo / "api.py").write_text("def api_ready():\n    return True\n")
    else:
        raise ValueError(f"unknown acceptance case {case_id}")


def _prepare_case(root: Path, case: dict[str, Any]) -> dict[str, Any]:
    case_dir = root / case["id"]
    repo = case_dir / "repo"
    run_dir = repo / ".planning" / "agentic-epic" / f"eval-{case['id']}"
    repo.mkdir(parents=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "acceptance-eval@example.invalid")
    _git(repo, "config", "user.name", "Acceptance Eval")
    (repo / "README.md").write_text("# acceptance eval\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "base")
    branch = f"epic/eval-{case['id']}"
    _git(repo, "switch", "-c", branch)
    _write_case_surface(repo, case["id"])
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", f"case {case['id']}")
    assembled_sha = _git(repo, "rev-parse", "HEAD")
    base_sha = _git(repo, "rev-parse", "main")
    run_dir.mkdir(parents=True)
    (run_dir / "goal.md").write_text("\n".join(case["prd"]) + "\n")
    (run_dir / ae._PRD_FILE).write_text(
        "# PRD\n\n## Acceptance criteria\n\n"
        + "\n".join(f"- {criterion}" for criterion in case["prd"])
        + "\n"
    )
    integrated = case["id"] != "unintegrated-child"
    blocking = [] if integrated else ["ui-child: child not integrated"]
    manifest = {
        "schema": "po.epic-acceptance-manifest",
        "version": 1,
        "base_branch": "main",
        "base_sha": base_sha,
        "epic_branch": branch,
        "assembled_sha": assembled_sha,
        "children": [
            {
                "id": "child-1",
                "dispatch_present": True,
                "integrated": integrated,
                "ancestry_proven": integrated,
                "artifact_path": "fixture",
                "artifact": {
                    "revisions": {"head": assembled_sha},
                    "terminal": {"state": "completed"},
                    "changed_surfaces": case["child_evidence"].get(
                        "changed_surfaces", []
                    ),
                    "live_verification": {
                        "plan": case["child_evidence"].get("live_verification", []),
                        "results": [],
                    },
                },
                "blocking_facts": blocking,
            }
        ],
        "blocking_facts": blocking,
        "fixture_observation": case["assembled_observation"],
    }
    (run_dir / ae._ACCEPTANCE_MANIFEST_FILE).write_text(
        json.dumps(manifest, indent=2) + "\n"
    )
    return {
        "case": case,
        "repo": repo,
        "run_dir": run_dir,
        "branch": branch,
        "assembled_sha": assembled_sha,
        "manifest": manifest,
    }


def _render_role(prepared: dict[str, Any]) -> str:
    values = {
        "seed_id": f"eval-{prepared['case']['id']}",
        "role_step_bead_id": "eval-only-no-bead",
        "run_dir": str(prepared["run_dir"]),
        "rig_path": str(prepared["repo"]),
        "pack_path": str(prepared["repo"]),
        "prd_file": ae._PRD_FILE,
        "epic_branch": prepared["branch"],
        "base_branch": "main",
        "integration_summary": (
            "- `child-1`: LANDED"
            if not prepared["manifest"]["blocking_facts"]
            else "- `ui-child`: DROPPED — not integrated"
        ),
        "acceptance_manifest": str(prepared["run_dir"] / ae._ACCEPTANCE_MANIFEST_FILE),
        "assembled_sha": prepared["assembled_sha"],
        "integration_path": str(prepared["repo"]),
    }
    prompt = (AGENT_DIR / "prompt.md").read_text()
    task = (AGENT_DIR / "task.md").read_text()
    values["role_step_close_block"] = (
        "EVAL MODE: do not create, update, or close beads. Return only the "
        "structured verdict requested below."
    )
    for key, value in values.items():
        prompt = prompt.replace("{{" + key + "}}", str(value))
        task = task.replace("{{" + key + "}}", str(value))
    return (
        prompt
        + "\n\n"
        + task
        + "\n\n# Eval output\nReturn JSON with `verdict` (`pass` or `fail`) and "
        "`rationale`. You must still write the required critique and live-verification "
        "artifacts before returning."
    )


def _run_role(prepared: dict[str, Any], model: str) -> dict[str, str]:
    schema = prepared["run_dir"] / "verdict-schema.json"
    output = prepared["run_dir"] / "last-message.json"
    schema.write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {
                    "verdict": {"type": "string", "enum": ["pass", "fail"]},
                    "rationale": {"type": "string"},
                },
                "required": ["verdict", "rationale"],
                "additionalProperties": False,
            }
        )
    )
    _run(
        [
            "codex",
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--dangerously-bypass-hook-trust",
            "--model",
            model,
            "--sandbox",
            "danger-full-access",
            "--cd",
            str(prepared["repo"]),
            "--output-schema",
            str(schema),
            "--output-last-message",
            str(output),
            "-",
        ],
        prepared["repo"],
        input_text=_render_role(prepared),
    )
    return json.loads(output.read_text())


@dataclass
class _StepResult:
    verdict: str
    closed_by: str = "acceptance-eval"


def _formula_smoke(prepared: dict[str, Any], model: str) -> dict[str, Any]:
    case = prepared["case"]
    repo = prepared["repo"]
    child_id = "child-1"
    artifact = prepared["manifest"]["children"][0]["artifact"]
    integrated = not prepared["manifest"]["blocking_facts"]
    pr_calls: list[dict[str, Any]] = []

    ae.get_run_logger = lambda: __import__("logging").getLogger("acceptance-eval")
    ae.claim_issue = lambda *args, **kwargs: None
    ae.close_issue = lambda *args, **kwargs: None
    ae._tag_flow_run_with_issue_id = lambda *args, **kwargs: None
    ae._load_rig_env = lambda *args, **kwargs: None
    ae._bd_show_description = lambda *args, **kwargs: "\n".join(case["prd"])
    ae._existing_planned_children = lambda *args, **kwargs: [child_id]
    ae.graph_run = lambda **kwargs: {
        "status": "completed",
        "results": {
            child_id: {
                "integration": {"merged": integrated},
                "verified_delivery": artifact,
            }
        },
    }
    ae.sb.create_integration_branch = lambda *args, **kwargs: {
        "branch": prepared["branch"],
        "created": False,
        "pushed": False,
        "remote": False,
    }
    ae.sb.open_draft_pr = lambda *args, **kwargs: (
        pr_calls.append(dict(kwargs))
        or {"opened": True, "url": "scratch://acceptance-pr", "reason": ""}
    )
    ae.sb.cleanup_integration_worktree = lambda *args, **kwargs: None

    def acceptance_step(*, step: str, **kwargs: Any) -> _StepResult:
        if step != "epic-acceptance-critic":
            raise AssertionError(f"unexpected agent step {step}")
        return _StepResult(_run_role(prepared, model)["verdict"])

    ae.agent_step = acceptance_step
    result = ae.agentic_epic.fn(
        epic_id=f"eval-{case['id']}",
        rig="acceptance-eval",
        rig_path=str(repo),
        pack_path=str(repo),
        shared_branch=True,
        acceptance_fix_cap=0,
    )
    return {"result": result, "pr_calls": pr_calls}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--model", default=os.environ.get("PO_MODEL_CLI", "gpt-5.4"))
    args = parser.parse_args()
    root = args.output_dir.resolve()
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    cases = json.loads(CASES_PATH.read_text())
    results: list[dict[str, Any]] = []
    prepared_cases: dict[str, dict[str, Any]] = {}
    for case in cases:
        prepared = _prepare_case(root, case)
        prepared_cases[case["id"]] = prepared
        verdict = _run_role(prepared, args.model)
        passed = verdict["verdict"] == case["expected"]
        results.append(
            {
                "case": case["id"],
                "expected": case["expected"],
                "actual": verdict["verdict"],
                "passed": passed,
                "rationale": verdict["rationale"],
            }
        )
        print(f"{case['id']}: {verdict['verdict']} ({'PASS' if passed else 'FAIL'})")

    complete_smoke = _formula_smoke(prepared_cases["complete"], args.model)
    missing_ui_smoke = _formula_smoke(prepared_cases["missing-ui"], args.model)
    formula_checks = {
        "complete_ready": complete_smoke["pr_calls"][0]["draft"] is False,
        "missing_ui_draft": missing_ui_smoke["pr_calls"][0]["draft"] is True,
        "complete_manifest_read": (
            prepared_cases["complete"]["run_dir"] / "critique-epic-acceptance.md"
        ).is_file(),
        "complete_live_verification": (
            prepared_cases["complete"]["run_dir"] / "epic-live-verification.md"
        ).is_file(),
    }
    report = {"behavioral_cases": results, "formula_checks": formula_checks}
    (root / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    if not all(item["passed"] for item in results) or not all(formula_checks.values()):
        raise SystemExit(1)
    print("formula smoke: PASS")


if __name__ == "__main__":
    main()
