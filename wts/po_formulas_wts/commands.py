"""Pack-shipped `po.commands` callables — non-orchestrated utility ops.

Each callable here is registered in this pack's `pyproject.toml` under
`[project.entry-points."po.commands"]` and dispatched via
`po <command> [--key=value ...]` (NOT `po run`). They skip Prefect
overhead per principle §4.
"""

from __future__ import annotations

import json
import re
import subprocess

from prefect_orchestration.run_lookup import resolve_run_dir, RunDirNotFound


def summarize_verdicts(issue_id: str) -> None:
    """Print a one-line summary per `po.*` metadata key across an issue's iter beads.

    Walks every iter bead under the seed (`<seed>.<step>.iter<N>`) and
    prints one line per `po.<role>` metadata key found, sorted by step
    + iter index. Resolves the seed's rig_path via bd metadata so the
    `bd` shellout runs in the right rig.
    """
    try:
        loc = resolve_run_dir(issue_id)
    except RunDirNotFound as exc:
        print(f"error: {exc}")
        raise SystemExit(2) from exc

    rig_path = loc.rig_path
    proc = subprocess.run(
        ["bd", "list", "--parent", issue_id, "--all", "--json"],
        cwd=str(rig_path),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        print(f"no iter beads found for {issue_id} under {rig_path}")
        return

    try:
        rows = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        print(f"bd list returned unparseable JSON: {exc}")
        return

    iter_pat = re.compile(rf"^{re.escape(issue_id)}\.(.+?)\.iter(\d+)$")
    items = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        m = iter_pat.match(str(row.get("id", "")))
        if not m:
            continue
        metadata = row.get("metadata") or {}
        for key, value in metadata.items():
            if not str(key).startswith("po."):
                continue
            if key in {"po.run_dir", "po.rig_path"}:
                continue
            items.append((m.group(1), int(m.group(2)), key, value))

    if not items:
        print(f"no po.* verdict metadata found on iter beads of {issue_id}")
        return

    items.sort(key=lambda t: (t[0], t[1], t[2]))
    for step, iter_n, key, value in items:
        if isinstance(value, dict):
            verdict = str(value.get("verdict") or value.get("passed") or value.get("ralph_found_improvement") or "?")
            reason = value.get("reason") or value.get("summary") or ""
            first = str(reason).splitlines()[0] if reason else ""
        else:
            verdict = str(value)
            first = ""
        label = f"{step}-iter-{iter_n} {key.removeprefix('po.')}"
        print(f"  {label:38s}  {verdict:12s}  {first}")
