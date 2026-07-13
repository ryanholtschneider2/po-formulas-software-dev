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
