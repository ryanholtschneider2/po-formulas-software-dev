"""Tests for `.po-env` per-rig env-var loader.

The formula reads `<rig_path>/.po-env` at flow start so a rig can opt
out of slow test layers (PO_SKIP_E2E=1, etc.) without exporting in
every shell. Existing process env wins — the file is the rig default,
not an override.
"""

from __future__ import annotations

import os
from pathlib import Path

from po_formulas.software_dev import _load_rig_env


def test_loads_simple_keys(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / ".po-env").write_text("PO_SKIP_E2E=1\nFOO=bar\n")
    monkeypatch.delenv("PO_SKIP_E2E", raising=False)
    monkeypatch.delenv("FOO", raising=False)

    _load_rig_env(tmp_path)

    assert os.environ["PO_SKIP_E2E"] == "1"
    assert os.environ["FOO"] == "bar"


def test_existing_env_wins(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / ".po-env").write_text("PO_SKIP_E2E=1\n")
    monkeypatch.setenv("PO_SKIP_E2E", "0")

    _load_rig_env(tmp_path)

    assert os.environ["PO_SKIP_E2E"] == "0"


def test_no_file_is_noop(tmp_path: Path) -> None:
    # Should not raise, should not modify env unexpectedly.
    sentinel = "PO_TEST_SENTINEL_XYZ"
    assert sentinel not in os.environ
    _load_rig_env(tmp_path)
    assert sentinel not in os.environ


def test_ignores_blank_lines_and_comments(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / ".po-env").write_text(
        "# comment\n\nPO_KEY_A=hello\n   # indented comment\nPO_KEY_B=world\n"
    )
    monkeypatch.delenv("PO_KEY_A", raising=False)
    monkeypatch.delenv("PO_KEY_B", raising=False)

    _load_rig_env(tmp_path)

    assert os.environ["PO_KEY_A"] == "hello"
    assert os.environ["PO_KEY_B"] == "world"


def test_strips_quotes(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / ".po-env").write_text('PO_QUOTED="quoted-val"\n')
    monkeypatch.delenv("PO_QUOTED", raising=False)
    _load_rig_env(tmp_path)
    assert os.environ["PO_QUOTED"] == "quoted-val"


def test_skips_lines_without_equals(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / ".po-env").write_text("PO_GOOD=1\nbad-line-no-equals\nPO_OK=2\n")
    monkeypatch.delenv("PO_GOOD", raising=False)
    monkeypatch.delenv("PO_OK", raising=False)

    _load_rig_env(tmp_path)

    assert os.environ["PO_GOOD"] == "1"
    assert os.environ["PO_OK"] == "2"
