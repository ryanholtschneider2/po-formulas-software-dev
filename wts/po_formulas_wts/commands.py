"""Pack-shipped `po.commands` callables — non-orchestrated utility ops.

Each callable here is registered in this pack's `pyproject.toml` under
`[project.entry-points."po.commands"]` and dispatched via
`po <command> [--key=value ...]` (NOT `po run`). They skip Prefect
overhead per principle §4.
"""

from __future__ import annotations

import json

from prefect_orchestration.run_lookup import resolve_run_dir, RunDirNotFound


def summarize_verdicts(issue_id: str) -> None:
    """Print a one-line summary per `verdicts/*.json` for an issue's run dir.

    Resolves the run dir via bd metadata (po.rig_path / po.run_dir) and
    walks `<run_dir>/verdicts/*.json` in name order, printing
    `<step> <verdict> <reason-first-line>` for each. Non-fatal if the
    `verdicts/` directory is absent or empty — prints a clear hint.
    """
    try:
        loc = resolve_run_dir(issue_id)
    except RunDirNotFound as exc:
        print(f"error: {exc}")
        raise SystemExit(2) from exc

    vdir = loc.run_dir / "verdicts"
    if not vdir.is_dir():
        print(f"no verdicts/ under {loc.run_dir}")
        return

    files = sorted(vdir.glob("*.json"))
    if not files:
        print(f"no verdict files under {vdir}")
        return

    for path in files:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            print(f"  {path.stem:24s}  (unreadable: {exc})")
            continue
        verdict = str(data.get("verdict", "?"))
        reason_raw = data.get("reason") or data.get("summary") or ""
        first = reason_raw.splitlines()[0] if reason_raw else ""
        print(f"  {path.stem:24s}  {verdict:12s}  {first}")
