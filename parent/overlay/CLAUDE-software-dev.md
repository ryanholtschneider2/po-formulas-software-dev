# po-formulas-software-dev

**What it provides:** Actor-critic multi-agent pipelines for autonomous software development — full and fast variants, epic fan-out, graph dispatch, and skill evals.

**When to use:**
- Dispatching a beads issue for autonomous implementation (new features, bug fixes, refactors)
- Running a DAG-ordered epic of child issues in parallel
- Evaluating a pack's skills with LLM-judged rubrics

**Key verbs:** `software-dev-full`, `software-dev-fast`, `software-dev-agentic`, `software-dev-edit`, `epic`, `agentic-epic`, `graph`, `skill-evals`, `epic-finalize`
- `software-dev-edit`: ultra-thin plan → build → lint → close; for trivial single-file edits and doc tweaks; pair with `epic-finalize` as the last epic child.
- `software-dev-agentic`: one prompt-driven actor opens a worktree off `main`, builds, runs the repo's own tests/CI, and opens a PR — looped against one critic that verifies goal accomplishment (`pass`/`fail`). No machine gate layer; never auto-merges. See README §`software-dev-agentic`.
- `agentic-epic`: turns one epic goal into ONE integration branch `epic/<epic-id>` + ONE PR via PRD → decomposition → plan-critic → shared-branch dispatch → integrated acceptance. Final acceptance pins the assembled SHA, consumes every child's `verified-delivery.json`, and runs relevant live verification once on the whole; missing/dropped children or broken seams fail. Bounded acceptance-fix children may repair gaps. The PR opens ready only on PASS (draft on exhausted failure). **The flow wires `blocks` edges ONLY between coupled children** (shared `touches` → auto-serialized; disjoint → left parallel); a child may carry `"formula": "minimal-task"` to run a lighter pipeline. Shared-branch is the **default**; pass `--shared-branch=false` for the legacy N-per-child-PR path. See README §`agentic-epic`.

**Key paths:** `po_formulas/agents/<role>/prompt.md`, `po_formulas/software_dev.py`, `po_formulas/epic.py`

**Skip if:** The task doesn't involve code changes, or you only need scheduling / orchestration utilities without an actor-critic loop.

**Read more:** `po show software-dev-full`, `po show epic`, `engdocs/formula-modes.md`
