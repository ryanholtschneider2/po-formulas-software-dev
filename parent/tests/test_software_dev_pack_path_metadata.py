"""Resolution precedence for pack_path: CLI > bd metadata > rig_path.

Covers AC3 (`po.target_pack` bd metadata override).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from prefect_orchestration import role_registry as sd_mod
from prefect_orchestration.role_registry import _resolve_pack_path


def test_explicit_pack_path_wins_over_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = tmp_path / "rig"
    rig.mkdir()
    explicit = tmp_path / "explicit-pack"
    explicit.mkdir()
    md_target = tmp_path / "md-pack"
    md_target.mkdir()

    # Stub bd lookup to claim metadata says md_target — but explicit must win.
    def fake_run(cmd: list[str], **kwargs: object) -> object:
        out = json.dumps([{"metadata": {"po.target_pack": str(md_target)}}])
        return type("P", (), {"returncode": 0, "stdout": out, "stderr": ""})()

    monkeypatch.setattr(sd_mod.shutil, "which", lambda _: "/usr/bin/bd")
    monkeypatch.setattr(sd_mod, "_metadata_binary", lambda _: "/usr/bin/bd")
    monkeypatch.setattr(sd_mod.subprocess, "run", fake_run)

    got = _resolve_pack_path(str(explicit), "x-1", rig)
    assert got == explicit.resolve()


def test_bd_metadata_overrides_rig_path_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = tmp_path / "rig"
    rig.mkdir()
    md_target = tmp_path / "md-pack"
    md_target.mkdir()

    def fake_run(cmd: list[str], **kwargs: object) -> object:
        out = json.dumps([{"metadata": {"po.target_pack": str(md_target)}}])
        return type("P", (), {"returncode": 0, "stdout": out, "stderr": ""})()

    monkeypatch.setattr(sd_mod.shutil, "which", lambda _: "/usr/bin/bd")
    monkeypatch.setattr(sd_mod, "_metadata_binary", lambda _: "/usr/bin/bd")
    monkeypatch.setattr(sd_mod.subprocess, "run", fake_run)

    got = _resolve_pack_path(None, "x-1", rig)
    assert got == md_target.resolve()


def test_falls_back_to_rig_path_when_neither_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = tmp_path / "rig"
    rig.mkdir()

    def fake_run(cmd: list[str], **kwargs: object) -> object:
        out = json.dumps([{"metadata": {}}])
        return type("P", (), {"returncode": 0, "stdout": out, "stderr": ""})()

    monkeypatch.setattr(sd_mod.shutil, "which", lambda _: "/usr/bin/bd")
    monkeypatch.setattr(sd_mod, "_metadata_binary", lambda _: "/usr/bin/bd")
    monkeypatch.setattr(sd_mod.subprocess, "run", fake_run)

    got = _resolve_pack_path(None, "x-1", rig)
    assert got == rig


def test_falls_back_when_bd_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = tmp_path / "rig"
    rig.mkdir()
    monkeypatch.setattr(sd_mod.shutil, "which", lambda _: None)
    got = _resolve_pack_path(None, "x-1", rig)
    assert got == rig
