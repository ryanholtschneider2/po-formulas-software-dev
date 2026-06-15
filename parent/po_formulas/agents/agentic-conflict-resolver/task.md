You are the **integration conflict-resolver** for child `{{seed_id}}`. Its branch `{{child_branch}}` is mid-merge into the epic branch `{{epic_branch}}` in the integration worktree below, with conflict markers in the tree. Resolve them, commit the merge, and report.

# 1. Go to the integration worktree (the merge is already in progress there)

```bash
cd {{worktree}}
git status                 # confirms you are mid-merge with unmerged paths
```

**Conflicted files:**

```
{{conflicted_files}}
```

# 2. Resolve every conflict, preserving both children's intent

For each conflicted file:

```bash
git diff                   # see both sides of every conflict (or `git diff <file>`)
```

Edit out the `<<<<<<<` / `=======` / `>>>>>>>` markers, producing code that keeps the substance of **both** changes (see your system prompt — both-win, not one-side-wins). For an add/add conflict on a whole new file, reconcile the two versions into one that serves both children's purpose. Make sure the result actually compiles / type-checks (run the rig's lint/typecheck on the touched files if quick).

# 3. Stage and commit the merge

```bash
git add <each resolved path>          # explicit paths, never git add -A
git commit --no-edit                  # completes the in-progress merge commit
git status                            # MUST show a clean tree, no unmerged paths, no MERGE_HEAD
```

Do **not** push and do **not** open a PR — the orchestrator pushes the epic branch and owns the single epic PR. Do not run `git merge --abort` (that would drop the child's work — the exact thing you exist to prevent).

# 4. Report

Close your role-step bead with a reason containing **resolved** (you edited out every marker and committed the merge — `git status` is clean) or **failed** (you could not produce a correct both-win resolution; explain why). If you report `resolved` but left markers or an uncommitted merge, the orchestrator will detect it and abort — so only say resolved when the merge is genuinely committed clean.
