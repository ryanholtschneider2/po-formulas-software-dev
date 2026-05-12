"""Pack-contributed checks for `po doctor`.

Registered via the `po.doctor_checks` entry-point group in
`pyproject.toml`. Each check is a zero-arg callable returning a
`prefect_orchestration.doctor.DoctorCheck`. Core wraps invocations in a
soft 5s timeout (yellow on timeout) and aggregates the results into the
unified `po doctor` table.
"""

from __future__ import annotations

import os
import shutil
import subprocess

from prefect_orchestration.doctor import DoctorCheck


def _backend_choice() -> str:
    return (os.environ.get("PO_BACKEND") or "").strip().lower()


def _selected_cli_spec() -> tuple[str, str, str]:
    choice = _backend_choice()
    if choice == "stub":
        return ("stub", "PO_BACKEND=stub", "")
    if choice.startswith("codex"):
        return ("codex", "codex CLI present", "install Codex CLI")
    return ("claude", "claude CLI present", "install Claude Code")


def selected_agent_cli_present() -> DoctorCheck:
    """Verify the CLI for the selected PO backend is on PATH and runnable."""
    binary, name, install_hint = _selected_cli_spec()
    if binary == "stub":
        return DoctorCheck(
            name=name,
            status="green",
            message="stub backend selected; no agent CLI required",
        )

    path = shutil.which(binary)
    if not path:
        return DoctorCheck(
            name=name,
            status="red",
            message=f"`{binary}` not on PATH",
            hint=install_hint,
        )
    try:
        proc = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return DoctorCheck(
            name=name,
            status="yellow",
            message=f"`{binary} --version` timed out",
            hint=f"check your {binary} install for a hang",
        )
    except OSError as exc:
        return DoctorCheck(
            name=name,
            status="red",
            message=f"failed to invoke {binary}: {exc}",
            hint=install_hint,
        )
    if proc.returncode != 0:
        return DoctorCheck(
            name=name,
            status="red",
            message=f"`{binary} --version` exited {proc.returncode}",
            hint=install_hint,
        )
    return DoctorCheck(
        name=name,
        status="green",
        message=(proc.stdout or "").strip() or path,
    )
