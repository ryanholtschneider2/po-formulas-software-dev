# po-formulas-software-dev

**What it provides:** Actor-critic multi-agent pipelines for autonomous software development — full and fast variants, epic fan-out, graph dispatch, and skill evals.

**When to use:**
- Dispatching a beads issue for autonomous implementation (new features, bug fixes, refactors)
- Running a DAG-ordered epic of child issues in parallel
- Evaluating a pack's skills with LLM-judged rubrics

**Key verbs:** `software-dev-full`, `software-dev-fast`, `software-dev-agentic`, `software-dev-edit`, `epic`, `agentic-epic`, `graph`, `skill-evals`, `epic-finalize`
- `software-dev-edit`: ultra-thin plan → build → lint → close; for trivial single-file edits and doc tweaks; pair with `epic-finalize` as the last epic child.
- `software-dev-agentic`: one prompt-driven actor opens a worktree off `main`, builds, runs the repo's own tests/CI, and opens a PR — looped against one critic that verifies goal accomplishment (`pass`/`fail`). No machine gate layer; never auto-merges. See README §`software-dev-agentic`.
- `agentic-epic`: turns one epic goal into ONE integration branch `epic/<epic-id>` + ONE PR via gated phases — **brainstorm** (optional two-role Product-Visionary/Technical-Architect debate for vague/complex goals; self-skips when overkill; `--brainstorm auto|always|never`) → **PRD** (scope the goal: problem / acceptance-criteria outcomes / surfaces) → **decomposition** (children **by logical separable chunk — NO target/max child count**, each declaring the files it `touches` as coupling evidence + any real `depends_on`) → **deep code-grounded plan-critic loop** (walks PRD ACs one by one, opens the cited files to verify same-file pairs are ordered, sizing/deps/buildability/ordering) → **shared-branch dispatch** (independent children parallel off the epic tip, coupled children stack, each merged on critic-pass) → **finalize** (ONE `software-dev-agentic` builder runs the full rig suite + cross-child integration/smoke + docs/roadmap, skipped for 1-child epics) → **acceptance-critic** judges the assembled diff vs the PRD; PR opened ready on PASS / draft on FAIL. **Ordering is the planner's judgment**: it declares `depends_on` and the flow records exactly those as `blocks` edges (nothing inferred from `touches`); a child may carry `"formula": "minimal-task"` for a lighter pipeline. Shared-branch is the **default**; pass `--shared-branch=false` for the legacy N-per-child-PR path. See README §`agentic-epic`.

**Key paths:** `po_formulas/agents/<role>/prompt.md`, `po_formulas/software_dev.py`, `po_formulas/epic.py`

**Skip if:** The task doesn't involve code changes, or you only need scheduling / orchestration utilities without an actor-critic loop.

**Read more:** `po show software-dev-full`, `po show epic`, `engdocs/formula-modes.md`
