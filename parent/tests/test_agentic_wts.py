"""Unit tests for wts/po_formulas_wts/agentic.py (po-formulas-software-dev-63s)."""

from __future__ import annotations

import importlib.metadata

import pytest

_WTS_INSTALLED = any(
    ep.name == "software-dev-agentic-wts"
    for ep in importlib.metadata.entry_points(group="po.formulas")
)
wts_only = pytest.mark.skipif(
    not _WTS_INSTALLED,
    reason="wts pack not installed in this venv",
)


@wts_only
def test_entry_point_registered():
    """software-dev-agentic-wts must appear in the po.formulas EP group."""
    eps = {ep.name: ep for ep in importlib.metadata.entry_points(group="po.formulas")}
    ep = eps["software-dev-agentic-wts"]
    assert ep.value == "po_formulas_wts.agentic:software_dev_agentic_wts"


@wts_only
def test_import_and_flow_name():
    """The flow must have the right name when the wts pack is importable."""
    mod = pytest.importorskip("po_formulas_wts.agentic")
    flow = mod.software_dev_agentic_wts
    assert flow.__name__ == "software_dev_agentic_wts"
    assert flow.name == "software_dev_agentic_wts"
