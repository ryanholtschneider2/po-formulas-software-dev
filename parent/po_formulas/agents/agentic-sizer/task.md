You are sizing seed issue `{{seed_id}}` before any implementation worker is dispatched.

1. Read the issue with `bd show {{seed_id}}` and inspect `{{pack_path}}` only as needed to understand the real delivery surfaces and existing implementation.
2. Judge whether the goal is one PR-sized unit or requires decomposition. Do not use keyword rules, numeric scoring, or deterministic heuristics.
3. Write `{{run_dir}}/sizing.json` as exactly one JSON object:

```json
{
  "decision": "proceed",
  "size": "medium",
  "risk": "medium",
  "surfaces": ["API", "tests"],
  "surface_types": ["api", "code"],
  "iteration_budget": 2,
  "rationale": "Why this is or is not one coherent PR-sized delivery.",
  "decomposition_reason": "Empty when proceeding; when decomposing, explain the independent product or code slices."
}
```

Allowed values:

- `decision`: `proceed` or `decompose`
- `size`: `trivial`, `small`, `medium`, `large`, or `oversized`
- `risk`: `low`, `medium`, or `high`
- `iteration_budget`: integer 1 through 4
- `surfaces`: non-empty strings naming the affected user/system surfaces
- `surface_types`: one or more of `api`, `cli`, `code`, `data`, `docs`,
  `infrastructure`, `service`, `ui`, or `workflow`. This semantic classification
  controls the proof phases after review: live surfaces get artifacts and a
  live verifier; deployable surfaces get deploy-smoke; UI can never pass on a
  diff review alone and gets a demo when the rig enables demo capture.

Budget guidance is contextual, not a formula: use 1 for work that should complete in one careful pass; 2 for ordinary scoped PR work; 3–4 only when a coherent PR has meaningful integration or risk. If the goal contains multiple independently shippable surfaces that need their own acceptance proof, choose `decompose` instead of inflating the budget.

After writing valid JSON, stamp the same object with `po write-verdict`, then close your iter bead with `proceed:` or `decompose:` as instructed by your role prompt.
