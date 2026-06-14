# po-formulas-software-dev

**What it provides:** Actor-critic multi-agent pipelines for autonomous software development — full and fast variants, epic fan-out, graph dispatch, and skill evals.

**When to use:**
- Dispatching a beads issue for autonomous implementation (new features, bug fixes, refactors)
- Running a DAG-ordered epic of child issues in parallel
- Evaluating a pack's skills with LLM-judged rubrics

**Key verbs:** `software-dev-full`, `software-dev-fast`, `software-dev-agentic`, `software-dev-edit`, `epic`, `agentic-epic`, `graph`, `skill-evals`, `epic-finalize`
- `software-dev-edit`: ultra-thin plan → build → lint → close; for trivial single-file edits and doc tweaks; pair with `epic-finalize` as the last epic child.
- `software-dev-agentic`: one prompt-driven actor opens a worktree off `main`, builds, runs the repo's own tests/CI, and opens a PR — looped against one critic that verifies goal accomplishment (`pass`/`fail`). No machine gate layer; never auto-merges. See README §`software-dev-agentic`.
- `agentic-epic`: plans an epic from its goal, creates stamped children, fans them out as `software-dev-agentic` runs (each its own PR). Add `--shared-branch` for a **coupled** epic (children editing the same files): the whole epic lands as ONE integration branch `epic/<epic-id>` + ONE draft PR — independent children run in parallel off the epic tip, `blocks`-chained children stack, and each child is merged into the epic branch on critic-pass. Default OFF. **Wire `blocks` edges only between children that actually couple** (same files) — that wiring is what makes them stack instead of colliding. See README §`agentic-epic --shared-branch`.

**Key paths:** `po_formulas/agents/<role>/prompt.md`, `po_formulas/software_dev.py`, `po_formulas/epic.py`

**Skip if:** The task doesn't involve code changes, or you only need scheduling / orchestration utilities without an actor-critic loop.

**Read more:** `po show software-dev-full`, `po show epic`, `engdocs/formula-modes.md`
