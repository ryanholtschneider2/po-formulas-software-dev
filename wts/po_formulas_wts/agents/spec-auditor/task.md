Audit the implementation in `{{rig_path}}` against the spec at `{{spec_path}}`.

Process:

1. Read the full spec at `{{spec_path}}`. Enumerate every concrete capability, subcommand, flag, endpoint, behavior, or output the spec mandates.

2. For each enumerated capability, find the corresponding code in `{{rig_path}}` and exercise it behaviorally (run the binary, curl the endpoint, inspect the produced files). Do not rely on source grep alone — confirm by running.

3. For each capability, decide: `OK` (works as spec describes), `GAP` (spec mandates it but it's missing entirely), or `BROKEN` (it exists but doesn't behave as the spec describes).

4. Write your findings to `{{run_dir}}/spec-audit.md` with one bullet per capability:

   ```markdown
   # Spec-audit for epic {{seed_id}}

   - **OK** — `todo add <text>` per spec § "Core item ops": invoked `todo add "buy milk" --priority h`, got id back.
   - **GAP** — `todo undo`: spec § "Import/export/sync" mandates a 10-entry ring buffer of write ops. No undo subcommand in CLI. Code reference: src/todo_cli/cli.py — `undo` never registered.
   - **BROKEN** — `todo edit --add-tag`: spec § "Core item ops" lists `--add-tag`. Invoking `todo edit 1 --add-tag foo` errors with `Unknown option`. Code reference: src/todo_cli/cli.py:107.
   ```

5. Write a structured verdict to `{{run_dir}}/verdicts/spec-audit.json`:

   ```json
   {"verdict": "PASSED", "gaps": []}
   ```
   or
   ```json
   {"verdict": "FAILED", "gaps": ["one-line summary per gap, exactly matching the GAP/BROKEN bullets above"]}
   ```

6. Close your bead with `bd close {{seed_id}} --reason "spec-audit complete: <PASSED|FAILED>, <N> gaps"`.

Done.
