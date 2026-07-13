"""Mechanical branch, pull-request, integration, and preview identity checks."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse


class DeliveryTruthError(RuntimeError):
    """A claimed delivery fact disagrees with git, GitHub, or the live process."""


def _run(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)


def revision(repo: Path, ref: str) -> str:
    proc = _run(["git", "rev-parse", "--verify", ref], cwd=repo)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise DeliveryTruthError(
            f"cannot resolve git ref {ref!r} in {repo}: {proc.stderr.strip()}"
        )
    return proc.stdout.strip()


def require_ancestor(repo: Path, ancestor: str, descendant: str, *, label: str) -> None:
    proc = _run(["git", "merge-base", "--is-ancestor", ancestor, descendant], cwd=repo)
    if proc.returncode != 0:
        raise DeliveryTruthError(
            f"{label}: {ancestor} is not an ancestor of {descendant}"
        )


def branch_truth(repo: Path, *, branch: str, base_branch: str) -> dict[str, str]:
    """Resolve a worker branch and prove it descends from the requested base."""
    base_sha = revision(repo, base_branch)
    head_sha = revision(repo, branch)
    require_ancestor(repo, base_sha, head_sha, label="worker branch ancestry mismatch")
    return {
        "base_branch": base_branch,
        "base_sha": base_sha,
        "head_branch": branch,
        "head_sha": head_sha,
    }


def worktree_for_branch(repo: Path, branch: str) -> Path:
    """Return the registered worktree path for a local branch."""
    proc = _run(["git", "worktree", "list", "--porcelain"], cwd=repo)
    wanted = f"refs/heads/{branch}"
    current_path: Path | None = None
    for line in proc.stdout.splitlines():
        if line.startswith("worktree "):
            current_path = Path(line.removeprefix("worktree "))
        elif line == f"branch {wanted}" and current_path is not None:
            return current_path.resolve()
    raise DeliveryTruthError(f"no registered worktree found for branch {branch}")


def integration_truth(
    repo: Path, *, child_branch: str, integration_branch: str, base_branch: str
) -> dict[str, str]:
    """Prove the integration branch has the right base and contains the child."""
    base_sha = revision(repo, base_branch)
    child_sha = revision(repo, child_branch)
    integration_sha = revision(repo, integration_branch)
    require_ancestor(
        repo, base_sha, integration_sha, label="integration branch base mismatch"
    )
    require_ancestor(repo, child_sha, integration_sha, label="child is not integrated")
    return {
        "base_sha": base_sha,
        "child_sha": child_sha,
        "integration_sha": integration_sha,
    }


def pull_request_truth(
    repo: Path, *, head_branch: str, target_branch: str
) -> dict[str, object] | None:
    """Return PR identity when present and reject a PR aimed at another base."""
    if shutil.which("gh") is None:
        return None
    proc = _run(
        [
            "gh",
            "pr",
            "view",
            head_branch,
            "--json",
            "number,url,headRefName,baseRefName,state",
        ],
        cwd=repo,
    )
    if proc.returncode != 0:
        return None
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise DeliveryTruthError("gh returned invalid PR identity JSON") from exc
    actual_head = str(payload.get("headRefName") or "")
    actual_base = str(payload.get("baseRefName") or "")
    if actual_head != head_branch:
        raise DeliveryTruthError(
            f"PR head mismatch: expected {head_branch}, found {actual_head or '(empty)'}"
        )
    if actual_base != target_branch:
        raise DeliveryTruthError(
            f"PR target mismatch: expected {target_branch}, found {actual_base or '(empty)'}"
        )
    return {
        "number": payload.get("number"),
        "url": payload.get("url"),
        "target": actual_base,
        "head": actual_head,
        "state": payload.get("state"),
    }


def _listening_pid(port: int) -> int:
    if shutil.which("lsof") is None:
        raise DeliveryTruthError("cannot verify localhost preview: lsof is unavailable")
    proc = subprocess.run(
        ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
        capture_output=True,
        text=True,
        check=False,
    )
    pids = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if proc.returncode != 0 or len(pids) != 1:
        raise DeliveryTruthError(
            f"cannot identify one listening process for localhost preview port {port}"
        )
    return int(pids[0])


def localhost_preview_truth(
    url: str, *, expected_repo: Path, expected_revision: str
) -> dict[str, object]:
    """Prove a localhost URL is served by the expected checkout and revision."""
    parsed = urlparse(url)
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"} or not parsed.port:
        raise DeliveryTruthError(
            "preview URL is not a localhost URL with an explicit port"
        )
    pid = _listening_pid(parsed.port)
    try:
        cwd = Path(f"/proc/{pid}/cwd").resolve(strict=True)
    except OSError as exc:
        raise DeliveryTruthError(f"cannot inspect preview process {pid} cwd") from exc
    expected_root = expected_repo.resolve()
    try:
        cwd.relative_to(expected_root)
    except ValueError as exc:
        raise DeliveryTruthError(
            f"preview app mismatch: process cwd {cwd} is outside {expected_root}"
        ) from exc
    served_revision = revision(cwd, "HEAD")
    if served_revision != expected_revision:
        raise DeliveryTruthError(
            f"stale preview revision: expected {expected_revision}, served {served_revision}"
        )
    return {
        "url": url,
        "revision": served_revision,
        "process_id": pid,
        "app_root": str(expected_root),
        "process_cwd": str(cwd),
    }
