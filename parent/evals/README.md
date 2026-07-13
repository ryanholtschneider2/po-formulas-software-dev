# Agentic sizing evals

`agentic-sizing-cases.json` is the regression corpus for the model-authored
pre-dispatch sizing judgment. It includes the CourtPro and Storybook scale
failures that motivated decomposition refusal, plus a trivial control case.

An evaluator should give each `goal` to the `agentic-sizer` role, validate the
returned `sizing.json` with `po_formulas.agentic_sizing.read_sizing`, and compare
the judgment to the expected decision, accepted size values, and accepted
iteration budgets. The expectations intentionally avoid semantic Python
heuristics: the model judges each goal, while the harness checks only structured
output and declared policy bounds.

## Integrated epic acceptance cases

`agentic-epic-acceptance-cases.json` is the behavioral regression corpus for
the final assembled-epic judge. It contains the three deceptive delivery shapes
that must fail even when individual child work looks healthy: foundation-only
plumbing for an end-user criterion, a working API with the required UI absent,
and a completed child whose commit never reached the integration SHA.

An evaluator should place each case beside a representative PRD and acceptance
manifest, run the `agentic-epic-acceptance-critic` role, and require the declared
verdict. Python validates manifest structure and git ancestry; the model decides
whether the delivered surfaces and live whole-product proof satisfy the PRD.

Run the executable corpus and decorated-formula smoke without installing the
worktree pack into the shared `po` environment:

```bash
uv run --project parent python \
  parent/evals/run_agentic_epic_acceptance_evals.py /tmp/agentic-epic-acceptance
```

The harness creates disposable git workspaces, renders the shipped acceptance
role, and drives it with Codex. It requires the complete control to pass; the
foundation-only, missing-UI, and unintegrated-child cases to fail; and the real
`agentic_epic` flow to request a ready PR only for the complete case and a draft
for the failed case. `report.json`, each model verdict, and the generated
`epic-live-verification.md` / `critique-epic-acceptance.md` files are retained
under the chosen output directory.

## Verified-delivery dogfood

`run_verified_delivery_dogfood.py` is the publish gate for the complete
verified-delivery contract. It executes scratch backend and UI tasks through
`software_dev_agentic`, including a forced verifier rejection and retry. It
also drives a shared child through the formula's real branch-truth and
merge-back path in a disposable git epic, then feeds the emitted child contract
to assembled acceptance. Formula-boundary injections prove that explicit red
smoke and success-shaped packaging with no artifact fail closed. A separate run
is stopped during verification and resumed with `PO_RESUME=1`, proving preserved
role/session state and a final terminal contract. The remaining scenarios start
a real localhost server for stale-preview rejection and inject a wrong PR base.

```bash
uv run --project parent python \
  parent/evals/run_verified_delivery_dogfood.py /tmp/verified-delivery-dogfood
```

The command exits non-zero on any failed scenario and retains `report.json`,
`report.md`, the UI run directory, and disposable git fixtures for review.
