# wts pack evals

Shape-level evals for `po-formulas-software-dev-wts`. Modeled after
`~/Desktop/Code/personal/directive/`'s **`dogfood-eval`** pattern: a
bash runner exercises each registered formula, emits `PASS: <case>` /
`FAIL: <case>` lines on stdout, and the `dogfood-eval` Prefect formula
counts those lines into a JSON history entry.

These are **stub-backend / dry-run** evals — they validate the
orchestration shape (formula registered, chain advances, return-dict
keys, verdict files appear) without spending model tokens or hitting
real `gh` / `make` / Playwright. End-to-end "real epic, real PR"
testing happens organically on natural work items.

## Run

```bash
# Prereqs: dolt-sql-server on 127.0.0.1:3307, po on PATH, bd on PATH,
# jq on PATH.

cd ~/Desktop/Code/personal/po-formulas-software-dev
bash wts/evals/run.sh                          # all cases
bash wts/evals/run.sh --case epic-wts-shape    # one case
bash wts/evals/run.sh --no-cleanup             # keep tmp rigs for forensics

# CI-integration via the directive formula:
po run dogfood-eval --rig-path /home/ryan-24/Desktop/Code/personal/po-formulas-software-dev/wts
```

`dogfood-eval` (formula in `agent_engine/formulas/dogfood_eval.py`)
expects `<rig-path>/eval/run.sh` — symlink or rename if you want to
plug into that runner verbatim. Direct `bash wts/evals/run.sh` is
identical functionally and easier to debug.

## What each case asserts

| Case | What it exercises | Verdicts |
|---|---|---|
| `po-list-shape` | Entry-point metadata for all 7 wts formulas + `epic-wts` is registered | grep `^formula <name>` for each |
| `epic-wts-shape` | Full chain run (`--stub-backend --dry_run true`) on a synthetic 2-child epic | Both children submit; chain advances past `pre_pr_review`; return dict has top-level `verdict` ∈ {passed, blocked, partial, failed} |
| `pr-writer-dry-run` | `pr_writer` dry-run writes `verdicts/pr-writer.json` with `verdict=PASS` + `dry_run=true` | jq assertions on the verdict file |
| `epic-finalize-post-flight` | `epic_finalize` dry-run produces `post-flight.md` with the `## Gates` section + smoke/demo/CI rows | grep on the artifact |

## Adding a case

1. Add a `case_<name>()` function in `run.sh`.
2. Use `scaffold_rig "<short-name>"` for an isolated bd-server rig + tmp dir.
3. Run the formula via `po run <name> --dry_run true --stub-backend`.
4. Assert on stdout + verdict files + planning artifacts.
5. Call `cleanup_rig "${rig}"` at the end.
6. Append `case_<name>` to the "run all" block at the bottom.
7. Add a row to the table above.

## Why not `po run skill-evals` + `evals.json`?

The core `skill-evals` formula uses `pydantic_evals.LLMJudge` against
per-case JSON definitions (the demo-video skill model). That's heavy
for pure shape validation — you need an OpenAI/Anthropic key and the
`[evals]` extra. The bash-PASS/FAIL pattern is what directive landed
on for the same reason: fast, CI-cheap, no judge model needed.

When we need **judge-graded** evals (e.g. "is the generated PR body
high quality?") layered on top of shape evals, we can add `evals.json`
per-formula alongside this `run.sh` — same dir, different pattern.

## State

History rows from `dogfood-eval` invocations land in `.state/eval-history.jsonl`.
Per-run reports at `.state/eval-<UTC>.md`. Both gitignored.
