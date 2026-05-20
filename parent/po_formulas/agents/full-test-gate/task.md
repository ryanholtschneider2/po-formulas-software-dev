You are the **tester** running the `full_test_gate` for issue `{{issue_id}}` — the final safety net between verifier approval and `bd close`.

The actor-critic iter loop ran **scoped** test selections (only test files reachable from each iteration's diff, plus a smoke set). This step runs the **full** suite for every enabled layer to catch anything the scoping missed. Layers running here: **{{layers}}**.

Run **exactly** the commands below — the orchestrator computed them so layers stay non-overlapping (sibling layer dirs are excluded via `--ignore`). Pipe each layer's output to its own log file under the run_dir so the verdict can cite specific failures.

```bash
cd {{rig_path}}
{{test_cmds}} 2>&1 | tee {{run_dir}}/full-test-gate.log
```

Treat any pre-existing failure recorded in `{{run_dir}}/baseline.txt` as **NOT a regression** (it was already broken before this issue's changes). Anything that was green at baseline and is now red is a regression and must fail the gate.

# REQUIRED FINAL STEP — DO NOT SKIP

Your turn is **not complete** until the verdict is stamped on your bead. The orchestrator reads bead metadata key `po.full_test_gate` to decide between (a) closing the seed and (b) routing failures back to ralph for a fix-up turn. Run the right bash command verbatim as the **last action** before returning, then verify with `bd show`:

On a clean pass:

```bash
bd update {{role_step_bead_id}} --metadata '{"po.full_test_gate": {"passed": true, "summary": "all enabled layers green vs baseline"}}'
```

On failure (one or more newly-failing tests):

```bash
bd update {{role_step_bead_id}} --metadata '{"po.full_test_gate": {"passed": false, "failures": ["tests/test_x.py::test_a", "tests/test_y.py::test_b"], "summary": "two newly-failing tests after iter loop"}}'
```

Verify it landed:
```bash
bd show {{role_step_bead_id}} --json | python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin)[0]['metadata'].get('po.full_test_gate'), indent=2))"
```

Always populate `failures` with the **newly** failing test node IDs (not the baseline-already-broken ones). The ralph fix-up turn reads this list verbatim.
