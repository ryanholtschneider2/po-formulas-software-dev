"""Shared-integration-branch transport for ``agentic-epic`` (po-formulas-software-dev-18m).

Pure git/gh mechanics — **no Prefect imports** — so every function is callable
from a flow, a dry-run script, or a unit test with ``subprocess.run``
monkeypatched. This is the *transport* leg of ZFC: branch / worktree / merge
plumbing lives here as deterministic code; *which* children couple (and thus
stack) stays the operator's `blocks`-edge wiring, read elsewhere.

Shared-branch mode lands a whole coupled epic as **one** integration branch
``epic/<epic-id>`` and **one** draft PR, instead of N per-child PRs:

- ``create_integration_branch`` cuts ``epic/<id>`` off ``main`` once (idempotent)
  and pushes it so the draft PR has a head.
- ``open_draft_pr`` opens a single draft PR for the branch (graceful no-op when
  there is no remote or ``gh`` is absent; idempotent if one already exists).
- Each child runs in its own worktree branched off the **current** epic tip
  (``child_branch_name``); independent children fan out in parallel, ``blocks``-
  chained children stack because a dependent starts only after its prerequisite
  has been integrated and so branches off the advanced tip.
- ``integrate_child`` merges a passed child's branch into ``epic/<id>`` inside a
  dedicated integration worktree, **serialized by a file lock** so parallel lanes
  never race the shared ref. A conflict aborts cleanly and is reported (rare —
  coupled children are ``blocks``-ordered and never run concurrently).
- ``mark_pr_ready`` flips the draft PR to ready at finalize for human review.

The module never merges to ``main``; the single epic PR is the deliverable.
"""

from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

logger = logging.getLogger(__name__)

# The per-child worktree branch base directory mirrors the wts pack convention
# (`<rig>/.worktrees/`) so nested worktrees stay out of the main rig's status.
_WORKTREE_DIR = ".worktrees"


# ─── identifiers ───


def epic_branch_name(epic_id: str) -> str:
    """The single integration branch for a shared-branch epic: ``epic/<epic-id>``."""
    return f"epic/{epic_id}"


def child_branch_name(child_id: str) -> str:
    """Per-child branch cut off the epic tip. Sanitized (dots → ``_``) so it is a
    valid refname / tmux session segment, matching the wts worktree convention.
    """
    return f"agentic-{_sanitize(child_id)}"


def _sanitize(issue_id: str) -> str:
    """Replace anything outside ``[A-Za-z0-9_-]`` with ``_`` (refname-safe).

    Reuses the wts worktree sanitizer when that pack is installed so the slug is
    identical across packs; falls back to an inline copy otherwise (the function
    is leaf — stdlib only — so the import is cheap and cycle-free).
    """
    try:
        from po_formulas_wts.worktree import sanitize

        return sanitize(issue_id)
    except Exception:  # noqa: BLE001 — wts pack may not be installed
        import re

        return re.sub(r"[^A-Za-z0-9_-]", "_", issue_id)


# ─── git plumbing ───


def _git(
    args: list[str], *, cwd: Path | str, check: bool = True
) -> subprocess.CompletedProcess:
    """Run a ``git`` command with output captured. Raises with full context on
    ``check`` so a flow failure names the failing command, not a bare rc."""
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (rc={proc.returncode}) in {cwd}\n"
            f"stdout: {proc.stdout[:1000]}\nstderr: {proc.stderr[:1000]}"
        )
    return proc


def _has_remote(repo: Path | str) -> bool:
    return bool(_git(["remote"], cwd=repo, check=False).stdout.strip())


def _local_branch_exists(repo: Path | str, branch: str) -> bool:
    return (
        _git(
            ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=repo,
            check=False,
        ).returncode
        == 0
    )


def _gh_available() -> bool:
    from shutil import which

    return which("gh") is not None


# ─── integration branch + draft PR ───


def create_integration_branch(
    rig_path: Path | str,
    epic_id: str,
    *,
    base_branch: str = "main",
) -> dict[str, object]:
    """Create (or reuse) the ``epic/<epic-id>`` integration branch off ``base_branch``.

    Idempotent: if the branch already exists locally it is reused untouched (so a
    re-run / retry never discards children already integrated). When a remote is
    present the base is fetched first and the branch is cut off ``origin/<base>``
    (falling back to the local base), then pushed so the draft PR has a head.

    Returns ``{branch, created, pushed, remote}``.
    """
    repo = Path(rig_path).expanduser().resolve()
    branch = epic_branch_name(epic_id)
    remote = _has_remote(repo)

    if _local_branch_exists(repo, branch):
        logger.info("shared-branch: reusing existing %s", branch)
        pushed = False
        if remote:
            push = _git(["push", "-u", "origin", branch], cwd=repo, check=False)
            pushed = push.returncode == 0
        return {"branch": branch, "created": False, "pushed": pushed, "remote": remote}

    start_point = base_branch
    if remote:
        _git(["fetch", "origin", base_branch], cwd=repo, check=False)
        # Prefer the freshly-fetched remote tip; fall back to the local base.
        if (
            _git(
                [
                    "rev-parse",
                    "--verify",
                    "--quiet",
                    f"refs/remotes/origin/{base_branch}",
                ],
                cwd=repo,
                check=False,
            ).returncode
            == 0
        ):
            start_point = f"origin/{base_branch}"

    # Cut the branch at the base tip (no seed commit). The epic PR is opened at
    # FINALIZE, only once children have integrated real commits — so the branch
    # legitimately has a diff by then and no empty-commit seed is needed. Opening
    # the PR upfront risked the PR-sheriff merging a still-incomplete epic.
    _git(["branch", branch, start_point], cwd=repo)
    logger.info("shared-branch: created %s off %s", branch, start_point)

    pushed = False
    if remote:
        push = _git(["push", "-u", "origin", branch], cwd=repo, check=False)
        pushed = push.returncode == 0
        if not pushed:
            logger.warning(
                "shared-branch: push of %s failed; branch is local only", branch
            )
    return {"branch": branch, "created": True, "pushed": pushed, "remote": remote}


def commits_ahead(
    rig_path: Path | str, base_branch: str, branch: str
) -> int:
    """How many commits ``branch`` is ahead of ``base_branch`` (``base..branch``).

    Used at finalize to decide whether to open the epic PR at all: zero means no
    child integrated, so there is nothing to review and no PR is opened. Returns
    0 on any git error (treat "can't tell" as "nothing to PR").
    """
    repo = Path(rig_path).expanduser().resolve()
    out = _git(
        ["rev-list", "--count", f"{base_branch}..{branch}"], cwd=repo, check=False
    )
    try:
        return int(out.stdout.strip())
    except (ValueError, AttributeError):
        return 0


def open_draft_pr(
    rig_path: Path | str,
    *,
    branch: str,
    base_branch: str,
    title: str,
    body: str,
    draft: bool = True,
) -> dict[str, object]:
    """Open a single PR for ``branch`` → ``base_branch`` via ``gh``.

    ``draft=True`` (default) opens it as a draft; the epic flow opens it
    ``draft=False`` (ready-for-review) at FINALIZE, once every child has
    integrated — so the PR-sheriff never sees an incomplete epic.

    Graceful no-op (never raises) when there is no remote or ``gh`` is absent —
    the branch + commits are left for a human to PR. Idempotent: if a PR already
    exists for the branch its URL is returned instead of opening a second one.

    Returns ``{opened, url, reason}``.
    """
    repo = Path(rig_path).expanduser().resolve()
    if not _has_remote(repo):
        return {"opened": False, "url": "", "reason": "no remote (local-only repo)"}
    if not _gh_available():
        return {"opened": False, "url": "", "reason": "gh CLI not on PATH"}

    existing = _existing_pr_url(repo, branch)
    if existing:
        logger.info("shared-branch: PR already exists for %s: %s", branch, existing)
        return {"opened": False, "url": existing, "reason": "PR already exists"}

    proc = subprocess.run(
        [
            "gh",
            "pr",
            "create",
            *(["--draft"] if draft else []),
            "--base",
            base_branch,
            "--head",
            branch,
            "--title",
            title,
            "--body",
            body,
        ],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        reason = (proc.stderr or proc.stdout).strip()[:300]
        logger.warning("shared-branch: gh pr create failed: %s", reason)
        return {"opened": False, "url": "", "reason": f"gh pr create failed: {reason}"}
    url = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    logger.info("shared-branch: opened draft PR %s", url)
    return {"opened": True, "url": url, "reason": ""}


def _existing_pr_url(repo: Path, branch: str) -> str:
    """Return the URL of an open PR whose head is ``branch``, or ``""``."""
    proc = subprocess.run(
        ["gh", "pr", "view", branch, "--json", "url", "-q", ".url"],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip()
    return ""


def mark_pr_ready(rig_path: Path | str, *, branch: str) -> dict[str, object]:
    """Flip the draft PR for ``branch`` to ready-for-review (finalize step).

    Graceful no-op when no remote / no ``gh`` / no PR. Returns ``{ready, reason}``.
    """
    repo = Path(rig_path).expanduser().resolve()
    if not _has_remote(repo):
        return {"ready": False, "reason": "no remote (local-only repo)"}
    if not _gh_available():
        return {"ready": False, "reason": "gh CLI not on PATH"}
    proc = subprocess.run(
        ["gh", "pr", "ready", branch],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        reason = (proc.stderr or proc.stdout).strip()[:300]
        logger.warning("shared-branch: gh pr ready failed: %s", reason)
        return {"ready": False, "reason": f"gh pr ready failed: {reason}"}
    logger.info("shared-branch: marked PR for %s ready", branch)
    return {"ready": True, "reason": ""}


# ─── integration worktree lifecycle ───


def integration_worktree_path(rig_path: Path | str, epic_id: str) -> Path:
    """Deterministic path of the epic's integration worktree (checked out on the
    epic branch). Derived from ``epic_id`` so every child run resolves the same
    location without threading a path through kwargs."""
    return (
        Path(rig_path).expanduser().resolve()
        / _WORKTREE_DIR
        / f"epic-integrate-{_sanitize(epic_id)}"
    )


def ensure_integration_worktree(rig_path: Path | str, epic_id: str) -> Path:
    """Create (idempotently) the integration worktree checked out on ``epic/<id>``.

    Merges into the epic branch happen here so the *local* epic ref advances and
    the next dependent child branches off the integrated tip. Reuses an existing
    worktree untouched. Race-safe: the existence check + ``git worktree add`` run
    under the per-epic integration lock, so two parallel children that both pass
    their critic at the same instant don't both try to create it.
    """
    repo = Path(rig_path).expanduser().resolve()
    epic_branch = epic_branch_name(epic_id)
    wt = integration_worktree_path(repo, epic_id)
    with _integration_lock(repo, epic_id):
        if wt.exists():
            return wt
        wt.parent.mkdir(parents=True, exist_ok=True)
        # Keep the nested worktree out of the main rig's `git status`.
        _git(["worktree", "add", str(wt), epic_branch], cwd=repo)
        logger.info(
            "shared-branch: created integration worktree %s on %s", wt, epic_branch
        )
    return wt


def cleanup_integration_worktree(rig_path: Path | str, epic_id: str) -> None:
    """Remove the integration worktree (finalize). Best-effort; never raises."""
    repo = Path(rig_path).expanduser().resolve()
    wt = integration_worktree_path(repo, epic_id)
    if wt.exists():
        _git(["worktree", "remove", "--force", str(wt)], cwd=repo, check=False)


# ─── integrate-on-pass ───


@contextlib.contextmanager
def _integration_lock(rig_path: Path | str, epic_id: str) -> Iterator[None]:
    """Advisory file lock serializing integration merges for one epic.

    Parallel child lanes can finish (critic-pass) at the same instant; the merge
    into the shared epic branch must be one-at-a-time. ``flock`` on a per-epic
    lock file under the rig's ``.worktrees/`` gives that without a Prefect
    concurrency-limit dependency.
    """
    lock_dir = Path(rig_path).expanduser().resolve() / _WORKTREE_DIR
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_file = lock_dir / f".integrate-{_sanitize(epic_id)}.lock"
    fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def integrate_child(
    rig_path: Path | str,
    epic_id: str,
    child_id: str,
    *,
    integration_worktree: Path | str | None = None,
    push: bool = True,
) -> dict[str, object]:
    """Merge a passed child's branch into ``epic/<epic-id>`` (serialized).

    Runs inside the epic's integration worktree — a worktree checked out on the
    epic branch — so the local epic ref advances and the next dependent child
    branches off the integrated tip (the stacking guarantee). When
    ``integration_worktree`` is ``None`` the worktree is ensured first (its own
    locked create runs and releases *before* this merge takes the lock, so the
    flock is never nested — a second fd on the same lock file would self-deadlock).
    Holds the per-epic integration lock for the whole merge so parallel lanes
    don't race the ref.

    On conflict the merge is aborted (``git merge --abort``) and the epic branch
    is left clean; the caller decides whether to fall back to a fix-merge.

    Returns ``{merged, conflict, child_branch, pushed, reason}``.
    """
    repo = Path(rig_path).expanduser().resolve()
    if integration_worktree is None:
        wt = ensure_integration_worktree(repo, epic_id)
    else:
        wt = Path(integration_worktree).expanduser().resolve()
    child_branch = child_branch_name(child_id)
    epic_branch = epic_branch_name(epic_id)

    with _integration_lock(repo, epic_id):
        if not _local_branch_exists(repo, child_branch):
            return {
                "merged": False,
                "conflict": False,
                "child_branch": child_branch,
                "pushed": False,
                "reason": f"child branch {child_branch} not found",
            }
        # Defensive: the worktree should already be on the epic branch (that's
        # how it was created), but a checkout makes integrate self-correcting if
        # a prior run left it elsewhere. No-op when already on the branch.
        _git(["checkout", epic_branch], cwd=wt, check=False)
        merge = _git(["merge", "--no-edit", child_branch], cwd=wt, check=False)
        if merge.returncode != 0:
            _git(["merge", "--abort"], cwd=wt, check=False)
            reason = (merge.stdout + merge.stderr).strip()[:300]
            logger.warning(
                "shared-branch: merge of %s into %s conflicted; aborted (%s)",
                child_branch,
                epic_branch,
                reason,
            )
            return {
                "merged": False,
                "conflict": True,
                "child_branch": child_branch,
                "pushed": False,
                "reason": f"merge conflict: {reason}",
            }
        logger.info("shared-branch: integrated %s into %s", child_branch, epic_branch)

        pushed = False
        if push and _has_remote(wt):
            p = _git(["push", "origin", epic_branch], cwd=wt, check=False)
            pushed = p.returncode == 0
        return {
            "merged": True,
            "conflict": False,
            "child_branch": child_branch,
            "pushed": pushed,
            "reason": "",
        }


# ─── per-child worker prompt override ───


def branch_directive(epic_branch: str, child_id: str) -> str:
    """The high-priority prompt block that switches the agentic worker from
    "worktree off ``main`` + open a PR" to shared-branch behavior.

    Returned text is injected at the top of the worker task as
    ``{{branch_directive}}``. It tells the worker to branch off the **current
    epic tip** on a flow-dictated branch name and to **push but not PR** — the
    orchestrator integrates that branch into the epic branch on critic-pass and
    opens the single epic PR. Empty string in normal (per-child-PR) mode.
    """
    child_branch = child_branch_name(child_id)
    return (
        "# SHARED-BRANCH EPIC MODE — OVERRIDES the worktree/PR steps below\n\n"
        "This child is part of a shared-integration-branch epic. The numbered "
        "worktree-off-`main` and `gh pr create` steps below are **superseded** by "
        "the following. Do everything else (plan, build, test, docs, close-loop, "
        "commit) exactly as instructed.\n\n"
        f"1. Branch off the **current tip** of the epic branch `{epic_branch}` "
        f"(NOT `main`), onto branch `{child_branch}`, in your own worktree:\n\n"
        "   ```bash\n"
        "   cd {{pack_path}}\n"
        f"   git fetch origin {epic_branch} 2>/dev/null || true\n"
        f"   git worktree add ../$(basename {{{{pack_path}}}}).{child_branch} "
        f"-B {child_branch} {epic_branch}\n"
        f"   cd ../$(basename {{{{pack_path}}}}).{child_branch}\n"
        "   ```\n\n"
        "   (Your prerequisites have already been integrated into "
        f"`{epic_branch}`, so branching off its tip stacks your change on theirs.)\n"
        f"2. Implement + test on `{child_branch}`. Commit early and often.\n"
        f"3. **Push your branch** (`git push -u origin {child_branch}` if a remote "
        "exists) but **NEVER run `gh pr create` and NEVER merge to `main`** — the "
        f"orchestrator merges your branch into `{epic_branch}` on critic-pass and "
        "opens the single epic PR at the very end. If a step below says 'open a "
        "PR', that step does NOT apply to you; just push and report the branch.\n"
        "4. When a step below says **Save the diff**, diff against the epic branch "
        f"you forked from, NOT `main` (else you capture prior children's work too):\n\n"
        "   ```bash\n"
        f"   git diff {epic_branch}...HEAD > {{{{run_dir}}}}/build-iter-{{{{iter}}}}.diff\n"
        "   ```\n"
        "5. Close your iter bead exactly as instructed below."
    )


__all__ = [
    "epic_branch_name",
    "child_branch_name",
    "create_integration_branch",
    "commits_ahead",
    "open_draft_pr",
    "mark_pr_ready",
    "integrate_child",
    "branch_directive",
]
