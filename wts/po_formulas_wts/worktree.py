"""Git-worktree isolation for software_dev_full_wts and friends.

Each bead gets its own git worktree at `<rig_path>.wt-<sanitized-id>/`
on a per-bead branch `wts-<sanitized-id>`. The agent's cwd is the
worktree; concurrent siblings can't conflict on shared source files.

Shared resources are symlinked back to the main rig so they stay
authoritative across worktrees:

- `.beads/` — bd's dolt-server data lives in main only; symlink lets
  bd queries from a worktree see the same truth.
- `.planning/` — per-bead run dirs accumulate in main's `.planning/`,
  so `po watch` / `po artifacts` / `epic-finalize` find everything in
  one place.

Other tracked files (source, pyproject, .po-env, CLAUDE.md, …) are
copied into the worktree by `git worktree add` automatically.

Lessons baked in from the prior nanocorps-tbw attempt:
- Sanitize bead IDs with dots ('x.1' → 'x_1') so they're valid as
  branch names and tmux session names. Same convention as
  `prefect_orchestration.backend_select`.
- Use `-B` not `-b` when creating the branch so a stale leftover
  branch from a prior aborted run is reset, not error'd on.
- Always pass `--force` when removing — partial state from a crashed
  prior worktree must be reclaimable.

This module is intentionally a thin shell-out layer; no Prefect
imports. Callable from any flow or from a dry-run script.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


# ─── Identifier sanitization ───


def sanitize(issue_id: str) -> str:
    """Make a bead id safe as a git branch name / dir name segment.

    Branch names can't have dots in segments per refname rules; tmux
    treats dots as pane separators. Replace anything outside
    [A-Za-z0-9_-] with '_'.
    """
    return re.sub(r"[^A-Za-z0-9_-]", "_", issue_id)


# ─── Paths ───


@dataclass(frozen=True)
class WorktreePaths:
    """Computed paths for a (rig, issue) tuple."""

    main_rig: Path
    worktree: Path
    branch: str

    @classmethod
    def for_issue(cls, rig_path: Path | str, issue_id: str) -> "WorktreePaths":
        main = Path(rig_path).expanduser().resolve()
        slug = sanitize(issue_id)
        return cls(
            main_rig=main,
            worktree=main.parent / f"{main.name}.wt-{slug}",
            branch=f"wts-{slug}",
        )


# ─── Operations ───


def _run(cmd: list[str], *, cwd: Path | str, check: bool = True) -> subprocess.CompletedProcess:
    """Shell out with stdout/stderr captured. Raises with full context on `check`."""
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"command failed (rc={proc.returncode}) in {cwd}: {' '.join(cmd)}\n"
            f"stdout: {proc.stdout[:1000]}\nstderr: {proc.stderr[:1000]}"
        )
    return proc


def _is_git_repo(path: Path) -> bool:
    """True iff `path` is itself a git toplevel (not just a subpath of one).

    Strict check matters when rigs sit inside a mono-repo — the naive
    `git rev-parse --git-dir` walks up, so a target dir inside nanocorps
    would report True even though it isn't its own repo. That false
    positive made `git worktree add` attach the worktree to the OUTER
    repo's history, with confusing results."""
    proc = _run(
        ["git", "rev-parse", "--show-toplevel"], cwd=path, check=False
    )
    if proc.returncode != 0:
        return False
    return Path(proc.stdout.strip()).resolve() == path.resolve()


def _ensure_symlink(src: Path, dst: Path) -> None:
    """Create `dst` as a symlink to `src`. Idempotent."""
    if dst.exists() or dst.is_symlink():
        if dst.is_symlink() and dst.resolve() == src.resolve():
            return  # already correct
        if dst.is_dir() and not dst.is_symlink():
            # The git worktree add may have created an empty dir for a
            # path that's gitignored upstream. Replace with a symlink.
            try:
                dst.rmdir()
            except OSError:
                raise RuntimeError(
                    f"worktree: {dst} exists as a real directory (git likely tracked it). "
                    f"Fix: cd {dst.parent} && git rm -rf {dst.name} && git commit -m 'stop tracking {dst.name}'"
                )
        else:
            return
    dst.symlink_to(src.resolve())


def _worktree_exclude_file(main_rig: Path, branch: str) -> Path:
    """Resolve the per-worktree `info/exclude` file. Worktrees get their
    own at `<main>/.git/worktrees/<wt-name>/info/exclude`; falls back to
    the main repo's `.git/info/exclude` if the per-worktree dir is
    missing for some reason (older git, weird setups)."""
    wt_name = f"{main_rig.name}.wt-{branch.removeprefix('wts-')}"
    per_wt = main_rig / ".git" / "worktrees" / wt_name / "info" / "exclude"
    if per_wt.parent.is_dir():
        return per_wt
    return main_rig / ".git" / "info" / "exclude"


def _add_exclude_rules(main_rig: Path, branch: str, patterns: list[str]) -> None:
    """Append patterns to the per-worktree exclude file if not already
    present. Idempotent."""
    excl = _worktree_exclude_file(main_rig, branch)
    excl.parent.mkdir(parents=True, exist_ok=True)
    existing = excl.read_text() if excl.exists() else ""
    lines = set(line.strip() for line in existing.splitlines() if line.strip())
    new = [p for p in patterns if p not in lines]
    if not new:
        return
    with excl.open("a", encoding="utf-8") as fh:
        if existing and not existing.endswith("\n"):
            fh.write("\n")
        fh.write("# Added by po_formulas_wts.worktree — local-only.\n")
        for p in new:
            fh.write(f"{p}\n")


def setup_worktree(
    rig_path: Path | str,
    issue_id: str,
    *,
    shared_dirs: tuple[str, ...] = (".beads", ".planning"),
) -> Path:
    """Create (or refresh) a worktree for this bead. Returns the worktree path.

    Idempotent: if the worktree already exists at the expected path,
    refreshes the symlinks and returns it.
    """
    paths = WorktreePaths.for_issue(rig_path, issue_id)

    if not _is_git_repo(paths.main_rig):
        raise RuntimeError(
            f"rig at {paths.main_rig} is not a git repo; worktree isolation requires git. "
            "Pass --no-worktree (or set PO_WTS_NO_WORKTREE=1) to skip."
        )

    if paths.worktree.exists():
        logger.info("worktree: reusing existing %s", paths.worktree)
    else:
        # -B resets the branch if it already exists from a prior aborted run.
        _run(
            ["git", "worktree", "add", "-B", paths.branch, str(paths.worktree)],
            cwd=paths.main_rig,
        )
        logger.info("worktree: created %s on branch %s", paths.worktree, paths.branch)

    # Mark shared dirs as locally-ignored BEFORE we materialize them as
    # symlinks. Without this, `git add -A` (run later by build agents
    # or merge_worktree's commit_pending) would stage the symlink as a
    # tracked file, then `git checkout <target>` in the main rig refuses
    # the merge because the real directory contents would be lost.
    _add_exclude_rules(paths.main_rig, paths.branch, list(shared_dirs))

    for name in shared_dirs:
        src = paths.main_rig / name
        src.mkdir(parents=True, exist_ok=True)
        _ensure_symlink(src, paths.worktree / name)

    return paths.worktree


def _has_pending_changes(worktree: Path) -> bool:
    proc = _run(["git", "status", "--porcelain"], cwd=worktree, check=False)
    return bool(proc.stdout.strip())


def commit_pending(
    rig_path: Path | str,
    issue_id: str,
    *,
    message: str | None = None,
) -> bool:
    """Stage + commit anything dirty in the worktree. Returns True if a
    commit was created. Safe to call when the tree is already clean
    (returns False)."""
    paths = WorktreePaths.for_issue(rig_path, issue_id)
    if not paths.worktree.exists():
        return False
    if not _has_pending_changes(paths.worktree):
        return False
    _run(["git", "add", "-A"], cwd=paths.worktree)
    _run(
        ["git", "commit", "-m", message or f"[{issue_id}] wts worktree snapshot"],
        cwd=paths.worktree,
    )
    return True


def _current_branch(repo: Path) -> str:
    return _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo).stdout.strip()


def merge_worktree(
    rig_path: Path | str,
    issue_id: str,
    *,
    target_branch: str | None = None,
    cleanup: bool = True,
) -> str:
    """Merge the bead's worktree branch back into the main rig's current
    branch (or `target_branch` if explicitly named), then optionally
    remove the worktree.

    Returns the branch name merged into. Raises on merge conflict —
    caller decides whether to leave the worktree for resolution (set
    `cleanup=False`).
    """
    paths = WorktreePaths.for_issue(rig_path, issue_id)
    if not paths.worktree.exists():
        logger.warning("worktree: %s missing; nothing to merge", paths.worktree)
        return target_branch or _current_branch(paths.main_rig)

    commit_pending(rig_path, issue_id, message=f"[{issue_id}] pre-merge snapshot")

    target = target_branch or _current_branch(paths.main_rig)
    # Main rig may already be on `target` — `checkout` is still safe and
    # a no-op in that case.
    _run(["git", "checkout", target], cwd=paths.main_rig)
    _run(["git", "merge", "--no-edit", paths.branch], cwd=paths.main_rig)
    logger.info("worktree: merged %s into %s", paths.branch, target)

    if cleanup:
        cleanup_worktree(rig_path, issue_id, force=True, delete_branch=False)
    return target


def cleanup_worktree(
    rig_path: Path | str,
    issue_id: str,
    *,
    force: bool = False,
    delete_branch: bool = False,
) -> None:
    """Remove the worktree directory + (optionally) its branch.

    `force=True` reclaims a worktree that was left in a dirty state
    by a crash. `delete_branch=True` also drops the per-bead branch
    (useful after a successful merge; not after a failure)."""
    paths = WorktreePaths.for_issue(rig_path, issue_id)

    if paths.worktree.exists():
        args = ["git", "worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(paths.worktree))
        proc = _run(args, cwd=paths.main_rig, check=False)
        if proc.returncode != 0:
            logger.warning("worktree: remove failed (%s); leaving as-is", proc.stderr.strip())

    if delete_branch:
        proc = _run(
            ["git", "branch", "-D", paths.branch], cwd=paths.main_rig, check=False
        )
        if proc.returncode != 0:
            logger.warning(
                "worktree: branch -D %s failed (%s); leaving",
                paths.branch,
                proc.stderr.strip(),
            )
