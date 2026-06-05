"""Pin that structured-verdict role prompts route through `po write-verdict`.

prefect-orchestration-ysw moved the four roles that write a *structured*
verdict (read back by `parsing.read_bead_verdict`) off the dolt-only
`bd update <id> --metadata '{"po.<name>": ...}'` form and onto the
backend-agnostic `po write-verdict` command, so a full software_dev_full
run records verdicts on a br rig too. These assertions stop a future prompt
edit from silently reintroducing the hardcoded-`bd` write.
"""

from __future__ import annotations

import re

import pytest

from po_formulas.software_dev import _AGENTS_DIR

# (role dir, verdict name without the `po.` prefix)
_VERDICT_ROLES = [
    ("triager", "triage"),
    ("ralph", "ralph"),
    ("full-test-gate", "full_test_gate"),
    ("code-health-reviewer", "code_health"),
]

# A raw `bd update ... --metadata '{...}'` verdict write — the thing ysw removed.
_RAW_METADATA_WRITE = re.compile(r"bd\s+update\b.*--metadata\b")


def _task_text(role: str) -> str:
    return (_AGENTS_DIR / role / "task.md").read_text()


@pytest.mark.parametrize("role,name", _VERDICT_ROLES)
def test_role_uses_po_write_verdict(role: str, name: str) -> None:
    text = _task_text(role)
    assert "po write-verdict" in text, f"{role} should call `po write-verdict`"
    assert f"--name {name}" in text, f"{role} should pass --name {name}"


@pytest.mark.parametrize("role,_name", _VERDICT_ROLES)
def test_role_has_no_raw_bd_metadata_write(role: str, _name: str) -> None:
    text = _task_text(role)
    assert not _RAW_METADATA_WRITE.search(text), (
        f"{role}/task.md still has a raw `bd update --metadata` verdict write; "
        "route it through `po write-verdict` (backend-agnostic)."
    )
