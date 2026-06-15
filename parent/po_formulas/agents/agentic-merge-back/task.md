You are the **merge-back** for child `{{seed_id}}`. Merge its branch `{{child_branch}}` into the epic branch `{{epic_branch}}` in the epic integration worktree below, then report. You hold the integration lock, so no other child is merging right now.

# 1. Go to the epic integration worktree

```bash
cd {{worktree}}
git fetch origin {{epic_branch}} 2>/dev/null || true
git checkout {{epic_branch}}
git fetch origin {{child_branch}} 2>/dev/null || true   # the child pushed its branch
```

# 2. Merge your child branch in

```bash
git merge --no-edit {{child_branch}}
```

- **Clean merge** (the common case): done — go to step 4.
- **Conflict:** resolve it. For each conflicted file, `git diff <file>` to see both sides, edit out the `<<<<<<<` / `=======` / `>>>>>>>` markers producing code that keeps **both** children's intent (see your system prompt — both-win, never one-side-wins, never `git merge --abort`). For an add/add conflict on a brand-new file, reconcile the two versions into one. Make the result compile / type-check (run the rig's quick lint/typecheck on the touched files if fast).

```bash
git add <each resolved path>     # explicit paths, never git add -A
git commit --no-edit             # completes the merge commit
```

# 3. Verify the merge is committed clean

```bash
git status                       # MUST be clean: no unmerged paths, no MERGE_HEAD
git rev-parse -q --verify MERGE_HEAD && echo "STILL MID-MERGE — not done" || echo "merge committed"
```

# 4. Report

Do **not** push and do **not** open a PR — the orchestrator pushes `{{epic_branch}}` and owns the single epic PR.

Close your role-step bead with a reason containing **merged** (the child branch is merged into the epic branch and `git status` is clean) or **failed** (you could not land it — explain why; the epic acceptance critic will flag the missing child as a PRD gap). Only say **merged** when the merge is genuinely committed clean — if you leave markers or an uncommitted merge, the run is left broken.
