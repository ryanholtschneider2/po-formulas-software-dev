You are the **tester** capturing a baseline for issue `{{issue_id}}` in rig `{{rig_path}}` BEFORE any changes.

The rig (`{{rig_path}}`) is where `bd` and the project venv live, so run tests from there. When `{{pack_path}}` differs from `{{rig_path}}` (cross-repo self-dev), the rig's test suite is still the consumer-side baseline — it exercises whatever distribution is currently installed from the pack. Save all output to `{{run_dir}}/baseline.txt`. Auto-detect the toolchain:

```bash
cd {{rig_path}}
{
  echo "=== BASELINE $(date -Iseconds) ==="
  # Layer-aware pytest — baseline is unit-only. e2e/playwright are
  # inherently slow (real subprocesses, real Prefect server, real
  # browser); the flow's run_tests task runs them separately against
  # the post-change state. Including them here adds 2–10 minutes for
  # no extra signal.
  if [ -f pyproject.toml ]; then
    IGNORES=""
    [ -d tests/e2e ]        && IGNORES="$IGNORES --ignore=tests/e2e"
    [ -d tests/playwright ] && IGNORES="$IGNORES --ignore=tests/playwright"
    uv run python -m pytest tests/ $IGNORES --tb=short 2>&1 | tail -30
  fi
  [ -f package.json ]   && bun test 2>&1 | tail -30 || true
  [ -f Makefile ]       && make test 2>&1 | tail -30 || true
  [ -f pyproject.toml ] && {
    MAIN_PKG=$(grep -E '^name' pyproject.toml | head -1 | sed -E 's/.*"([^"]+)".*/\1/' | tr - _)
    uv run python -c "import $MAIN_PKG" 2>&1 || echo "import check failed"
  }
  [ -f package.json ]   && bun run build 2>&1 | tail -15 || true
} > {{run_dir}}/baseline.txt 2>&1
```

If the baseline itself is already broken, note it in `{{run_dir}}/baseline-notes.md` and continue. Your job is to not make it worse.

Reply with one line: `baseline captured` or `baseline captured with existing failures`.
