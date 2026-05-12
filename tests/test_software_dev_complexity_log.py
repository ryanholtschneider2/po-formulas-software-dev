"""Verify `software_dev_full` logs 'complexity tier: <tier>' as a
prominent standalone line, before the existing multi-key 'triage:' log.

Covers prefect-orchestration-89e:
  - logger.info("complexity tier: %s", tier) emits with format
    "complexity tier: <tier>"
  - Appears BEFORE the existing 'triage: complexity=... docs_only=...'
    line so it surfaces first in `po watch`.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

import po_formulas.software_dev as sd_mod


def test_complexity_tier_logged_before_triage_line(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Trivial-path short-circuit: stub triage to return complexity=trivial,
    flow returns immediately after the two log lines we care about."""
    test_logger = logging.getLogger("po_formulas.software_dev.test")
    test_logger.setLevel(logging.INFO)

    monkeypatch.setattr(sd_mod, "get_run_logger", lambda: test_logger)
    monkeypatch.setattr(sd_mod, "_agent_step_task", lambda **kw: {"status": "ok"})
    monkeypatch.setattr(
        sd_mod,
        "_read_triage_flags",
        lambda *a, **kw: {
            "complexity": "trivial",
            "is_docs_only": False,
            "has_ui": False,
        },
    )
    monkeypatch.setattr(sd_mod, "claim_issue", lambda *a, **kw: None)
    monkeypatch.setattr(sd_mod, "close_issue", lambda *a, **kw: None)

    rig = tmp_path / "rig"
    rig.mkdir()

    with caplog.at_level(logging.INFO, logger=test_logger.name):
        result = sd_mod.software_dev_full.fn(
            issue_id="test-89e",
            rig="rig",
            rig_path=str(rig),
            claim=False,
            dry_run=True,
        )

    assert result["complexity"] == "trivial"

    msgs = [rec.getMessage() for rec in caplog.records if rec.name == test_logger.name]
    tier_idx = next(
        (i for i, m in enumerate(msgs) if m == "complexity tier: trivial"),
        -1,
    )
    triage_idx = next(
        (i for i, m in enumerate(msgs) if m.startswith("triage: complexity=")),
        -1,
    )

    assert tier_idx >= 0, f"missing 'complexity tier: trivial' in logs; got: {msgs}"
    assert triage_idx >= 0, (
        f"missing 'triage: complexity=...' line in logs; got: {msgs}"
    )
    assert tier_idx < triage_idx, (
        f"'complexity tier' must precede 'triage:' line; order was: {msgs}"
    )
