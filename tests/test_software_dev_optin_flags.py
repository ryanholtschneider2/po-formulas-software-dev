"""`software_dev_full` exposes opt-in flags for ralph + full-test-gate.

Both phases default OFF — even at `complexity=complex` they only run
when the caller explicitly passes `enable_ralph=True` /
`enable_full_test_gate=True`. Tests assert the public signature has
both flags and they default to False.

This is a contract test: the flow's CLI surface (via `po run`) reflects
its function signature, so adding/removing/renaming these params is a
breaking change that should be caught here.
"""

from __future__ import annotations

import inspect

from po_formulas.software_dev import software_dev_full


def _params() -> dict[str, inspect.Parameter]:
    """Unwrap the Prefect @flow to inspect the underlying function."""
    fn = getattr(software_dev_full, "fn", software_dev_full)
    return dict(inspect.signature(fn).parameters)


def test_enable_ralph_param_exists_and_defaults_off() -> None:
    p = _params()
    assert "enable_ralph" in p, "enable_ralph must be on the public flow signature"
    assert p["enable_ralph"].default is False, (
        "enable_ralph must default to False — ralph is opt-in even on complex tier"
    )


def test_enable_full_test_gate_param_exists_and_defaults_off() -> None:
    p = _params()
    assert "enable_full_test_gate" in p
    assert p["enable_full_test_gate"].default is False, (
        "enable_full_test_gate must default to False — gate is opt-in even on complex tier"
    )


def test_existing_caps_unchanged() -> None:
    """The new opt-in flags do NOT replace the iter_cap parameters; both
    sets of knobs coexist (caps bound how many iters each phase runs;
    enable_ralph gates whether the phase runs at all)."""
    p = _params()
    assert p["ralph_iter_cap"].default == 3
    assert p["gate_iter_cap"].default == 2
    assert p["verify_iter_cap"].default == 3
