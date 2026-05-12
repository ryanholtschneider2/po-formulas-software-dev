# Pillar 2 — Cumulative-Diff Critic

**Verdict:** rejected

**Question (a) — did the cumulative diff fulfill the original plan?**
The plan required three pillars; only two were implemented.

**Question (b) — does the cumulative diff cohere?**
The diff is mostly coherent but missing the smoke-test pillar.

## Findings

### Finding 1: Pillar-3 smoke test not implemented
The spec explicitly requires a real-env smoke test via `make dev-up` + browser agent.
The diff shows no `_run_pillar_3` implementation.

### Finding 2: Missing mutation in metadata.validation
`bd update <epic> --set-metadata validation=blocked` is never called on failure.

### Finding 3: Report heading order not deterministic
The `_write_validation_report` function appends sections in arbitrary order instead
of the pinned H1 sequence: Pillar 1 → Pillar 2 → Pillar 3 → Summary.
