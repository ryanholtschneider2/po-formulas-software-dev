You are the **tester** running the `full_test_gate` for issue `{{issue_id}}` — the final safety net between verifier approval and `bd close`.

The actor-critic iter loop ran **scoped** test selections (only test files reachable from each iteration's diff, plus a smoke set). This step runs the **full** suite for every enabled layer to catch anything the scoping missed. Layers running here: **{{layers}}**.

Run **exactly** the commands below — the orchestrator computed them so layers stay non-overlapping (sibling layer dirs are excluded via `--ignore`). Pipe each layer's output to its own log file under the run_dir so the verdict can cite specific failures.

```bash
cd {{rig_path}}
{{test_cmds}} 2>&1 | tee {{run_dir}}/full-test-gate.log
```

Treat any pre-existing failure recorded in `{{run_dir}}/baseline.txt` as **NOT a regression** (it was already broken before this issue's changes). Anything that was green at baseline and is now red is a regression and must fail the gate.

# REQUIRED FINAL STEP — DO NOT SKIP

Your turn is **not complete** until the verdict file exists on disk. The orchestrator reads it to decide between (a) closing the bead and (b) routing the failures back to ralph for a fix-up turn. Run this bash command verbatim as the **last action** before returning, then verify with `ls` + `cat`:

On a clean pass:

```bash
mkdir -p {{run_dir}}/verdicts
cat > {{run_dir}}/verdicts/full-test-gate.json <<'EOF'
{"passed": true, "summary": "all enabled layers green vs baseline"}
EOF
```

On failure (one or more newly-failing tests):

```bash
mkdir -p {{run_dir}}/verdicts
cat > {{run_dir}}/verdicts/full-test-gate.json <<'EOF'
{"passed": false, "failures": ["tests/test_x.py::test_a", "tests/test_y.py::test_b"], "summary": "two newly-failing tests after iter loop"}
EOF
```

Always populate `failures` with the **newly** failing test node IDs (not the baseline-already-broken ones). The ralph fix-up turn reads this list verbatim.
