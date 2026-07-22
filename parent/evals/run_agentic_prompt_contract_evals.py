"""Live behavioral regressions for the shipped agentic worker and critic prompts.

The scenarios intentionally use a disposable directory and a subscription-backed
Codex invocation. They exercise the rendered prompt/task pair rather than
matching phrases in the source Markdown: one worker must reject unrelated
skill-driven work, and one critic must stop at exhausted evidence and return a
severity-ranked verdict.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from po_formulas import agentic

HERE = Path(__file__).parent
CASES_PATH = HERE / "agentic-prompt-contract-cases.json"
AGENTS_DIR = Path(agentic.__file__).parent / "agents"


def _replace_template(text: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    if "{{" in text:
        raise ValueError("agentic prompt eval left an unresolved template variable")
    return text


def _render_role(case: dict[str, Any], case_dir: Path) -> str:
    role = str(case["role"])
    role_dir = AGENTS_DIR / role
    values = {
        "seed_id": f"eval-{case['id']}",
        "role_step_bead_id": "eval-only-no-bead",
        "run_dir": str(case_dir / "run"),
        "rig_path": str(case_dir / "scratch-rig"),
        "pack_path": str(case_dir / "scratch-rig"),
        "base_branch": "main",
        "epic_branch": "",
        "branch_truth": "scratch branch truth is intentionally irrelevant",
        "verified_delivery_path": str(case_dir / "verified-delivery.json"),
        "iter": "1",
        "branch_directive": "",
        "preview_note": "",
        "revision_note": "",
        "learning_receipt_path": str(case_dir / "learning-receipt.md"),
        "role_step_close_block": (
            "EVAL MODE: do not call tools, create or close beads, or modify files. "
            "Return only the JSON object requested by the evaluation scenario."
        ),
    }
    prompt = _replace_template((role_dir / "prompt.md").read_text(), values)
    task = _replace_template((role_dir / "task.md").read_text(), values)
    return f"{prompt}\n\n{task}\n\n# Evaluation scenario\n\n{case['scenario']}\n"


def _output_schema(role: str) -> dict[str, Any]:
    if role == "agentic-worker":
        return {
            "type": "object",
            "properties": {
                "outcome": {
                    "type": "string",
                    "enum": ["scope_held", "scope_expanded"],
                },
                "summary": {"type": "string"},
            },
            "required": ["outcome", "summary"],
            "additionalProperties": False,
        }
    if role == "agentic-reviewer":
        return {
            "type": "object",
            "properties": {
                "verdict": {"type": "string", "enum": ["pass", "fail"]},
                "findings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "severity": {
                                "type": "string",
                                "enum": ["blocker", "major", "minor"],
                            },
                            "summary": {"type": "string"},
                        },
                        "required": ["severity", "summary"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["verdict", "findings"],
            "additionalProperties": False,
        }
    raise ValueError(f"unsupported prompt-eval role: {role}")


def _run_codex(
    *, prompt: str, schema_path: Path, output_path: Path, cwd: Path, model: str
) -> dict[str, Any]:
    result = subprocess.run(
        [
            "codex",
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--model",
            model,
            "--sandbox",
            "read-only",
            "--cd",
            str(cwd),
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
            "-",
        ],
        cwd=cwd,
        input=prompt,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Codex prompt evaluation failed ({result.returncode}):\n"
            f"{result.stderr[-2000:]}"
        )
    return json.loads(output_path.read_text())


def _matches_expected(case: dict[str, Any], output: dict[str, Any]) -> bool:
    expected = dict(case["expected"])
    if case["role"] == "agentic-worker":
        return output.get("outcome") == expected["outcome"]
    findings = output.get("findings", [])
    severities = [item.get("severity") for item in findings]
    return (
        output.get("verdict") == expected["verdict"]
        and severities == expected["severity_order"]
    )


def run(output_dir: Path, *, model: str) -> dict[str, Any]:
    root = output_dir.resolve()
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    cases = json.loads(CASES_PATH.read_text())
    results: list[dict[str, Any]] = []
    for case in cases:
        case_dir = root / str(case["id"])
        case_dir.mkdir()
        rendered = _render_role(case, case_dir)
        (case_dir / "rendered-role.md").write_text(rendered)
        schema_path = case_dir / "output-schema.json"
        output_path = case_dir / "response.json"
        schema_path.write_text(json.dumps(_output_schema(str(case["role"])), indent=2))
        output = _run_codex(
            prompt=rendered,
            schema_path=schema_path,
            output_path=output_path,
            cwd=case_dir,
            model=model,
        )
        passed = _matches_expected(case, output)
        result = {
            "case": case["id"],
            "role": case["role"],
            "expected": case["expected"],
            "actual": output,
            "passed": passed,
        }
        results.append(result)
        print(f"{case['id']}: {'PASS' if passed else 'FAIL'}")
    report = {
        "model": model,
        "cases": results,
        "overall_pass": all(item["passed"] for item in results),
    }
    (root / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    if not report["overall_pass"]:
        raise SystemExit(1)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output_dir", type=Path)
    parser.add_argument(
        "--model", default=os.environ.get("PO_MODEL_CLI", "gpt-5.6-terra")
    )
    args = parser.parse_args()
    run(args.output_dir, model=args.model)


if __name__ == "__main__":
    main()
