"""Regression tests for po-formulas-software-dev-1y0.

epic-wts/graph-wts must dispatch children via the -wts variants of the
software-dev formulas. The non-wts variants don't accept
`parent_epic_worktree=...`, silently ignore it, and let agents commit
to the main rig — see the bug bead for the full incident.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import patch

import pytest

import po_formulas_wts.graph as wts_graph


class _StubEP:
    """Minimal entry_points()-compatible object."""

    def __init__(self, name: str) -> None:
        self.name = name


def _make_eps(*names: str):
    """Return a callable replacement for `entry_points(group=...)`."""

    eps = [_StubEP(n) for n in names]

    def _fake(*, group: str | None = None):
        assert group == "po.formulas"
        return eps

    return _fake


class TestPromoteToWts:
    def test_promotes_when_wts_variant_exists(self):
        with patch.object(
            wts_graph,
            "entry_points",
            _make_eps("software-dev-full", "software-dev-full-wts"),
        ):
            assert wts_graph._promote_to_wts("software-dev-full") == "software-dev-full-wts"

    def test_passes_through_when_no_wts_variant(self):
        """`agent-step`, `prompt`, etc. have no -wts sibling; preserve as-is."""
        with patch.object(wts_graph, "entry_points", _make_eps("agent-step", "prompt")):
            assert wts_graph._promote_to_wts("agent-step") == "agent-step"
            assert wts_graph._promote_to_wts("prompt") == "prompt"

    def test_idempotent_for_already_wts(self):
        """`-wts` names return as-is (no `-wts-wts`)."""
        with patch.object(
            wts_graph,
            "entry_points",
            _make_eps("software-dev-full-wts"),
        ):
            assert wts_graph._promote_to_wts("software-dev-full-wts") == "software-dev-full-wts"

    def test_handles_each_known_wts_pair(self):
        with patch.object(
            wts_graph,
            "entry_points",
            _make_eps(
                "software-dev-full", "software-dev-full-wts",
                "software-dev-fast", "software-dev-fast-wts",
                "software-dev-edit", "software-dev-edit-wts",
            ),
        ):
            assert wts_graph._promote_to_wts("software-dev-full") == "software-dev-full-wts"
            assert wts_graph._promote_to_wts("software-dev-fast") == "software-dev-fast-wts"
            assert wts_graph._promote_to_wts("software-dev-edit") == "software-dev-edit-wts"


class TestResolvePerBeadFormula:
    """The wts-aware bead-level resolver must apply promotion."""

    def _logger(self):
        return logging.getLogger("test")

    def test_promotes_stamped_software_dev_full(self):
        """A bead with `po.formula=software-dev-full` resolves to the wts variant."""
        wts_callable = lambda: "wts-was-called"

        def _fake_resolve(name: str):
            assert name == "software-dev-full-wts", (
                f"resolver got {name!r}, expected promotion to -wts"
            )
            return wts_callable

        with patch.object(
            wts_graph, "entry_points",
            _make_eps("software-dev-full", "software-dev-full-wts"),
        ), patch.object(wts_graph, "_resolve_formula", side_effect=_fake_resolve):
            result = wts_graph._resolve_per_bead_formula(
                node={"id": "test-1", "metadata": {"po.formula": "software-dev-full"}},
                default_callable=lambda: "default-was-called",
                rig_path="/tmp/fake",
                logger=self._logger(),
            )
            assert result is wts_callable

    def test_passes_through_for_no_wts_variant(self):
        """A bead stamped `po.formula=agent-step` resolves to bare `agent-step`."""
        agent_step = lambda: "agent-step-was-called"

        def _fake_resolve(name: str):
            assert name == "agent-step", f"unexpected promotion: got {name!r}"
            return agent_step

        with patch.object(
            wts_graph, "entry_points", _make_eps("agent-step"),
        ), patch.object(wts_graph, "_resolve_formula", side_effect=_fake_resolve):
            result = wts_graph._resolve_per_bead_formula(
                node={"id": "test-2", "metadata": {"po.formula": "agent-step"}},
                default_callable=lambda: "default",
                rig_path="/tmp/fake",
                logger=self._logger(),
            )
            assert result is agent_step

    def test_no_metadata_returns_default(self):
        """Beads without `po.formula` metadata still use the caller's default."""
        default = lambda: "default-callable"
        result = wts_graph._resolve_per_bead_formula(
            node={"id": "test-3", "metadata": {}},
            default_callable=default,
            rig_path="/tmp/fake",
            logger=self._logger(),
        )
        assert result is default

    @pytest.mark.parametrize("sentinel", ["none", "no", "human", "skip", "NONE", "Human"])
    def test_human_sentinels_return_none(self, sentinel):
        """`po.formula in {none, no, human, skip}` → None (caller skips dispatch)."""
        result = wts_graph._resolve_per_bead_formula(
            node={"id": "test-4", "metadata": {"po.formula": sentinel}},
            default_callable=lambda: "default",
            rig_path="/tmp/fake",
            logger=self._logger(),
        )
        assert result is None


class TestEpicWtsDefault:
    """`epic_run_wts` must default to the -wts variant, not the parent one."""

    def test_module_source_picks_wts(self):
        """Source-level check: the hardcoded default in epic.py says -wts."""
        import po_formulas_wts.epic as epic_mod
        src = (epic_mod.__file__,)
        text = open(src[0]).read()
        # Find the formula_callable line; must be -wts
        assert '_resolve_formula("software-dev-full-wts")' in text, (
            "epic_run_wts should default to software-dev-full-wts; "
            "the bare variant silently ignores parent_epic_worktree."
        )
        # And the bare variant must NOT appear as a hardcoded resolve
        assert '_resolve_formula("software-dev-full")\n' not in text
