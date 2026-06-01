"""Git-worktree isolation for software_dev_full_wts and friends.

Each bead gets its own git worktree at
`<rig_path>/.worktrees/wts-<sanitized-id>/` on a per-bead branch
`wts-<sanitized-id>`. The agent's cwd is the worktree; concurrent
siblings can't conflict on shared source files.

Nesting worktrees *inside* the main rig keeps the parent directory
(often a multi-project workspace like `rocks_project/`) free of
`<rig>.wt-<id>` sibling clutter. `setup_worktree` auto-adds
`.worktrees/` to the main rig's `.git/info/exclude` so `git status`
in main stays clean.

Shared resources point back to the main rig so they stay authoritative
across worktrees. How depends on whether the dir is git-tracked:

- Untracked (common case) → symlinked back to main. Proven behavior.
- Tracked (e.g. a rig that committed `.beads/`/`.planning/`) → `git
  worktree add` materializes a real copy in the worktree, so we can't
  symlink. We mirror gas town instead:
  - `.beads/` — drop bd's native `redirect` file (a relative path bd
    follows to main's `.beads/`). Works for dolt-server and embedded;
    coexists with the checked-out `metadata.json`. No `git rm` needed.
  - `.planning/` — leave the checked-out dir in place. Run artifacts
    route to main's `.planning/` via the flow's absolute `run_dir`
    (rig_path is the main rig), so `po watch`/`artifacts`/`epic-finalize`
    still find everything in one place.

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
import os
import re
import shutil
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
        return cls._for_id(rig_path, issue_id)

    @classmethod
    def for_epic(cls, rig_path: Path | str, epic_id: str) -> "WorktreePaths":
        return cls._for_id(rig_path, epic_id)

    @classmethod
    def _for_id(cls, rig_path: Path | str, raw_id: str) -> "WorktreePaths":
        main = Path(rig_path).expanduser().resolve()
        slug = sanitize(raw_id)
        return cls(
            main_rig=main,
            worktree=main / ".worktrees" / f"wts-{slug}",
            branch=f"wts-{slug}",
        )


def _paths_for(
    rig_path: Path | str,
    issue_id: str,
    *,
    for_epic: bool = False,
) -> WorktreePaths:
    if for_epic:
        return WorktreePaths.for_epic(rig_path, issue_id)
    return WorktreePaths.for_issue(rig_path, issue_id)


# ─── Operations ───


def _run(
    cmd: list[str], *, cwd: Path | str, check: bool = True
) -> subprocess.CompletedProcess:
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
    proc = _run(["git", "rev-parse", "--show-toplevel"], cwd=path, check=False)
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


def _write_beads_redirect(beads_src: Path, worktree: Path) -> None:
    """gastown-style sharing for a git-TRACKED `.beads/`.

    When `.beads/` is tracked, `git worktree add` materializes a real,
    non-empty `.beads/` in the worktree, so we can't symlink over it.
    Instead we drop bd's native `redirect` file: a one-line relative path
    that bd's resolver follows to the main rig's `.beads/` (the shared
    dolt server in server mode, or the embedded data dir otherwise). The
    redirect coexists with the checked-out `metadata.json`/`config.yaml`
    (bd prefers the redirect), so no `git rm` is needed.

    Per bd's contract the path is resolved from the dir CONTAINING
    `.beads/` (the worktree), not from inside `.beads/`. `redirect` is
    gitignored by bd's `.beads/.gitignore`, so it never gets committed.
    """
    beads_dst = worktree / ".beads"
    beads_dst.mkdir(parents=True, exist_ok=True)
    rel = os.path.relpath(beads_src.resolve(), worktree.resolve())
    (beads_dst / "redirect").write_text(rel + "\n", encoding="utf-8")
    logger.info("worktree: wrote %s/.beads/redirect -> %s", worktree.name, rel)


def _wire_shared_dir(name: str, src: Path, worktree: Path) -> None:
    """Make a worktree see the main rig's shared `name` dir.

    Untracked rigs (the common case) get a symlink back to main — the
    original, proven behavior. When the dir is git-TRACKED (so the
    worktree materialized a real non-empty copy), we don't fight it:

    - `.beads/` → write a bd `redirect` file (see `_write_beads_redirect`).
    - anything else (`.planning/`) → leave the checked-out dir in place.
      Run artifacts route to main's `.planning/` via the flow's absolute
      `run_dir` (rig_path is the main rig), so the in-worktree copy is
      inert and merges back untouched.

    This keeps behavior identical for untracked rigs and turns the old
    hard-refusal on tracked dirs into a working path.
    """
    dst = worktree / name
    try:
        _ensure_symlink(src, dst)
        return
    except RuntimeError:
        # Tracked + materialized in the worktree — gastown-style fallback.
        pass
    if name == ".beads":
        # bd resolves the shared dolt server via the redirect file; no
        # filesystem artifact sharing needed.
        _write_beads_redirect(src, worktree)
    else:
        # `.planning/` holds run artifacts that two code paths read+write
        # (the flow creates run_dir; context_bundle/agents write into it).
        # They only coincide if `<wt>/.planning` IS `<main>/.planning`, so
        # restore the symlink even though git materialized a tracked copy.
        # `--skip-worktree` makes git ignore the swap so `git add -A`
        # (merge-back) never stages it or clobbers main's tracked copy.
        _skip_worktree_and_symlink(name, src, dst, worktree)


def _skip_worktree_and_symlink(name: str, src: Path, dst: Path, worktree: Path) -> None:
    """Replace a git-tracked, materialized `<wt>/<name>` dir with a symlink
    to `src`, marking its tracked entries skip-worktree so git ignores the
    swap. Restores the untracked-rig symlink semantics for tracked rigs."""
    proc = _run(["git", "ls-files", "-z", name], cwd=worktree, check=False)
    files = [p for p in proc.stdout.split("\0") if p]
    if files:
        # Chunk to stay well under ARG_MAX on huge run-dir trees.
        for i in range(0, len(files), 500):
            _run(
                ["git", "update-index", "--skip-worktree", "--", *files[i : i + 500]],
                cwd=worktree,
                check=False,
            )
    if dst.is_symlink():
        dst.unlink()
    elif dst.is_dir():
        shutil.rmtree(dst)
    dst.symlink_to(src.resolve())
    logger.info("worktree: %s tracked → skip-worktree + symlink to main", name)


def _worktree_exclude_file(main_rig: Path, branch: str) -> Path:
    """Resolve the per-worktree `info/exclude` file. Worktrees get their
    own at `<main>/.git/worktrees/<wt-name>/info/exclude`; falls back to
    the main repo's `.git/info/exclude` if the per-worktree dir is
    missing for some reason (older git, weird setups)."""
    # `git worktree add <path>` records the worktree under
    # `.git/worktrees/<basename(path)>/`; our worktree basename is the
    # branch name (e.g. `wts-<slug>`).
    gitdir = _resolve_gitdir(main_rig)
    # When main_rig is itself a worktree, `gitdir` already points to
    # `<real-main>/.git/worktrees/<wt-name>/`. Per-branch worktrees
    # we're creating live alongside it under `<real-main>/.git/worktrees/`.
    common_dir = gitdir.parent if gitdir.parent.name == "worktrees" else gitdir / "worktrees"
    per_wt = common_dir / branch / "info" / "exclude"
    if per_wt.parent.is_dir():
        return per_wt
    return gitdir / "info" / "exclude"


def _resolve_gitdir(main_rig: Path) -> Path:
    """Return the real git dir for `main_rig`. Handles the case where
    `main_rig` is itself a worktree (`.git` is a file pointing to the
    per-worktree gitdir under the main repo)."""
    dot_git = main_rig / ".git"
    if dot_git.is_dir():
        return dot_git
    if dot_git.is_file():
        # Worktree: `.git` is a text file `gitdir: <abspath>`.
        contents = dot_git.read_text(encoding="utf-8").strip()
        prefix = "gitdir:"
        if contents.startswith(prefix):
            gitdir = Path(contents[len(prefix):].strip())
            if not gitdir.is_absolute():
                gitdir = (main_rig / gitdir).resolve()
            return gitdir
    raise RuntimeError(
        f"{main_rig} has no resolvable .git (neither dir nor worktree pointer)"
    )


def _add_main_exclude(main_rig: Path, patterns: list[str]) -> None:
    """Append patterns to the main rig's `info/exclude`. Idempotent.
    Used to hide `.worktrees/` (which holds nested worktrees) from the
    main rig's `git status`. Handles worktree rigs by resolving `.git`
    to the real gitdir."""
    gitdir = _resolve_gitdir(main_rig)
    excl = gitdir / "info" / "exclude"
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
    for_epic: bool = False,
) -> Path:
    """Create (or refresh) a worktree for this bead. Returns the worktree path.

    Idempotent: if the worktree already exists at the expected path,
    refreshes the symlinks and returns it.
    """
    paths = _paths_for(rig_path, issue_id, for_epic=for_epic)

    if not _is_git_repo(paths.main_rig):
        raise RuntimeError(
            f"rig at {paths.main_rig} is not a git repo; worktree isolation requires git. "
            "Pass --no-worktree (or set PO_WTS_NO_WORKTREE=1) to skip."
        )

    # Worktree lives at `<main>/.worktrees/<wts-slug>/` — keep `.worktrees/`
    # out of the main rig's `git status` (its own .git/info/exclude is per-
    # working-tree, so this only affects the main rig).
    _add_main_exclude(paths.main_rig, [".worktrees/"])
    paths.worktree.parent.mkdir(parents=True, exist_ok=True)

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
        _wire_shared_dir(name, src, paths.worktree)

    return paths.worktree


def _has_pending_changes(worktree: Path) -> bool:
    proc = _run(["git", "status", "--porcelain"], cwd=worktree, check=False)
    return bool(proc.stdout.strip())


def commit_pending(
    rig_path: Path | str,
    issue_id: str,
    *,
    message: str | None = None,
    for_epic: bool = False,
) -> bool:
    """Stage + commit anything dirty in the worktree. Returns True if a
    commit was created. Safe to call when the tree is already clean
    (returns False)."""
    paths = _paths_for(rig_path, issue_id, for_epic=for_epic)
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


def _has_remote(repo: Path) -> bool:
    return bool(_run(["git", "remote"], cwd=repo, check=False).stdout.strip())


def push_worktree_branch(
    rig_path: Path | str,
    issue_id: str,
    *,
    for_epic: bool = False,
) -> dict[str, object]:
    """Commit anything pending in the bead's worktree and push its branch.

    Used by the PR-Sheriff handoff (ADE mode): instead of merging the worktree
    back into main, leave it intact and push the branch so the Sheriff (or a
    PR) can pick it up. No-ops gracefully when there is no `origin` remote
    (local-only repo) — the branch still exists locally for the Sheriff.

    Returns `{branch, pushed, remote}`. Raises only on a real push failure.
    """
    paths = _paths_for(rig_path, issue_id, for_epic=for_epic)
    if not paths.worktree.exists():
        logger.warning("worktree: %s missing; nothing to push", paths.worktree)
        return {"branch": paths.branch, "pushed": False, "remote": False}

    commit_pending(rig_path, issue_id, message=f"[{issue_id}] pre-handoff snapshot", for_epic=for_epic)

    remote = _has_remote(paths.worktree)
    pushed = False
    if remote:
        _run(["git", "push", "-u", "origin", paths.branch], cwd=paths.worktree)
        pushed = True
        logger.info("worktree: pushed %s to origin", paths.branch)
    else:
        logger.info("worktree: no remote; %s left local for the Sheriff", paths.branch)
    return {"branch": paths.branch, "pushed": pushed, "remote": remote}


def merge_worktree(
    rig_path: Path | str,
    issue_id: str,
    *,
    target_branch: str | None = None,
    cleanup: bool = True,
    for_epic: bool = False,
) -> str:
    """Merge the bead's worktree branch back into the main rig's current
    branch (or `target_branch` if explicitly named), then optionally
    remove the worktree.

    Returns the branch name merged into. Raises on merge conflict —
    caller decides whether to leave the worktree for resolution (set
    `cleanup=False`).
    """
    paths = _paths_for(rig_path, issue_id, for_epic=for_epic)
    if not paths.worktree.exists():
        logger.warning("worktree: %s missing; nothing to merge", paths.worktree)
        return target_branch or _current_branch(paths.main_rig)

    commit_pending(
        rig_path,
        issue_id,
        message=f"[{issue_id}] pre-merge snapshot",
        for_epic=for_epic,
    )

    target = target_branch or _current_branch(paths.main_rig)
    # Main rig may already be on `target` — `checkout` is still safe and
    # a no-op in that case.
    _run(["git", "checkout", target], cwd=paths.main_rig)
    _run(["git", "merge", "--no-edit", paths.branch], cwd=paths.main_rig)
    logger.info("worktree: merged %s into %s", paths.branch, target)

    if cleanup:
        cleanup_worktree(
            rig_path,
            issue_id,
            force=True,
            delete_branch=False,
            for_epic=for_epic,
        )
    return target


def cleanup_worktree(
    rig_path: Path | str,
    issue_id: str,
    *,
    force: bool = False,
    delete_branch: bool = False,
    for_epic: bool = False,
) -> None:
    """Remove the worktree directory + (optionally) its branch.

    `force=True` reclaims a worktree that was left in a dirty state
    by a crash. `delete_branch=True` also drops the per-bead branch
    (useful after a successful merge; not after a failure)."""
    paths = _paths_for(rig_path, issue_id, for_epic=for_epic)

    if paths.worktree.exists():
        args = ["git", "worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(paths.worktree))
        proc = _run(args, cwd=paths.main_rig, check=False)
        if proc.returncode != 0:
            logger.warning(
                "worktree: remove failed (%s); leaving as-is", proc.stderr.strip()
            )

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
