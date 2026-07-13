# Learning receipts and promotion

`software-dev-agentic` writes one `learning-receipt.md` in its run directory on
every critic turn. The receipt is deliberately an evidence artifact, not an
automatic root-prompt edit.

## Receipt contract

- Missing file: critic step incomplete.
- Existing empty file: explicit “no reusable lesson.”
- Non-empty file: concise lesson, evidence, recommended scope, and suggested
  enforcement or destination.

The critic may recommend one of three scopes:

- `project`: guidance that depends on one product, repository, or directory.
- `user`: a durable operator preference that applies across projects.
- `engine`: reusable behavior owned by PO, a formula, an agent role, or an eval.

## Promotion contract

Dream/Improve or another learning job judges whether evidence is durable and
where it belongs. Prefer extending an existing skill, reference, prompt, or eval
over creating a duplicate. Detailed guidance lives in a standards document,
reference, or skill. `AGENTS.md` and `CLAUDE.md` receive only a short
discoverability pointer that says when to read or invoke that material.

Recommended durable locations:

- Project: `<project>/standards/learnings/`
- User: `~/.agents/standards/learnings/`
- Engine: `<engine-or-pack>/standards/learnings/`

Every promoted rule should retain a pointer to its receipt/run evidence so a
later audit can revise or remove guidance that did not generalize.
