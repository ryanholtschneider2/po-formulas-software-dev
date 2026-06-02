---
description: Dispatch a one-line ask through the software-dev-agentic flow (one worker owns plan→build→lint→test; machine gates + one reviewer)
argument-hint: <one-line description of the work>
---

The user wants to run the **software-dev-agentic** flow on this one-line ask:

> $ARGUMENTS

`software-dev-agentic` is the thin, self-decomposing pipeline: a single
worker agent owns the whole plan → build → lint → test loop (it may spawn
subagents), then a pure-Python mechanical gate layer (tree clean, work
landed, no mocked production code, lint clean, tests pass, no regression)
plus exactly one reviewer (intent + right-sized step-adherence) decide
whether the seed closes. Rigor scales to the ask — small asks are done
directly, PR-level asks run the full workflow.

Do this:

1. **Turn the ask into a seed bead** (skip if `$ARGUMENTS` is already a bead
   id like `<prefix>-xxx`). File it in the rig's beads tracker and label it
   `feature` so it surfaces on the board:

   ```bash
   bd create --title="<concise title from the ask>" \
     --description="<the ask, plus any acceptance criteria you can infer>" \
     --type=task --priority=2 -l feature
   ```

   Capture the new id.

2. **Dispatch the flow** on that id. Use the worktree-isolated formula by
   default on Ryan's machine (`software-dev-agentic-wts` if it is
   registered; otherwise `software-dev-agentic`):

   ```bash
   po run software-dev-agentic \
     --issue-id <bead-id> \
     --rig <rig-name> \
     --rig-path <absolute path to the rig>
   ```

   Run it in the background (`run_in_background: true`) since it is a
   long-running agent loop, and report the run id.

3. **Confirm scope before dispatching** only if the ask is ambiguous about
   *which* repo/rig it targets or *what* "done" means — otherwise pick the
   obvious rig (the current working directory's repo) and proceed.

After dispatch, point the user at `po logs <bead-id> -f`, `po watch
<bead-id>`, and `po artifacts <bead-id>` to follow and inspect the run.
