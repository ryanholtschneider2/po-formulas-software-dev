"""Tests for the `po write-verdict` command (prefect-orchestration-ysw).

The command is the backend-agnostic seam role prompts call instead of a raw
`bd update --metadata`. It resolves the rig's beads backend and routes the
verdict through `prefect_orchestration.beads_backend.write_verdict`, which
picks the dolt (`bd ... --set-metadata`) or br (`br comments add`) write form.

These tests stub `prefect_orchestration.beads_backend` via `sys.modules` so
they run against any core checkout (the seam may not be present on an older
core). A real bd/br round-trip against the live seam is exercised out-of-band
in the build's close-the-loop step, not here.
"""

from __future__ import annotations

import sys
import types

import pytest

from po_formulas.commands import write_verdict


def _install_fake_backend(
    monkeypatch: pytest.MonkeyPatch, *, backend: str, calls: list
) -> None:
    """Inject a fake `prefect_orchestration.beads_backend` module.

    `resolve_backend` returns *backend*; `write_verdict` appends its
    arguments to *calls* so the test can assert the routing.
    """
    mod = types.ModuleType("prefect_orchestration.beads_backend")

    def _resolve_backend(rig_path):  # noqa: ANN001 - mirror real loose typing
        return backend

    def _write_verdict(bead_id, name, payload, *, backend, rig_path):  # noqa: ANN001
        calls.append(
            {
                "bead_id": bead_id,
                "name": name,
                "payload": payload,
                "backend": backend,
                "rig_path": rig_path,
            }
        )

    mod.resolve_backend = _resolve_backend
    mod.write_verdict = _write_verdict
    monkeypatch.setitem(sys.modules, "prefect_orchestration.beads_backend", mod)


def test_routes_to_backend_write_verdict_dolt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list = []
    _install_fake_backend(monkeypatch, backend="dolt", calls=calls)

    write_verdict(
        bead_id="seed-triage-iter1",
        name="triage",
        payload='{"has_ui": false, "complexity": "moderate"}',
        rig_path="/some/rig",
    )

    assert len(calls) == 1
    call = calls[0]
    assert call["bead_id"] == "seed-triage-iter1"
    assert call["name"] == "triage"
    # Payload is parsed from JSON to a dict before the seam stamps it.
    assert call["payload"] == {"has_ui": False, "complexity": "moderate"}
    assert call["backend"] == "dolt"
    assert call["rig_path"] == "/some/rig"
    assert (
        "wrote po.triage verdict on seed-triage-iter1 via dolt"
        in capsys.readouterr().out
    )


def test_routes_to_backend_write_verdict_br(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list = []
    _install_fake_backend(monkeypatch, backend="br", calls=calls)

    write_verdict(
        bead_id="seed-full_test_gate-iter1",
        name="full_test_gate",
        payload='{"passed": true, "summary": "all green"}',
    )

    assert calls[0]["backend"] == "br"
    # Default rig_path is the current directory when the flag is omitted.
    assert calls[0]["rig_path"] == "."
    assert calls[0]["payload"] == {"passed": True, "summary": "all green"}
    assert "via br" in capsys.readouterr().out


def test_bad_json_payload_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list = []
    _install_fake_backend(monkeypatch, backend="dolt", calls=calls)

    with pytest.raises(SystemExit) as exc:
        write_verdict(bead_id="b1", name="triage", payload="{not json}")

    assert exc.value.code == 2
    assert not calls  # never reached the seam
    assert "not valid JSON" in capsys.readouterr().out


def test_write_failure_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = types.ModuleType("prefect_orchestration.beads_backend")
    mod.resolve_backend = lambda rig_path: "dolt"  # noqa: ARG005

    def _boom(bead_id, name, payload, *, backend, rig_path):  # noqa: ANN001
        raise RuntimeError("bd exited 1")

    mod.write_verdict = _boom
    monkeypatch.setitem(sys.modules, "prefect_orchestration.beads_backend", mod)

    with pytest.raises(SystemExit) as exc:
        write_verdict(
            bead_id="b1", name="ralph", payload='{"ralph_found_improvement": false}'
        )

    assert exc.value.code == 1
    assert "failed (dolt)" in capsys.readouterr().out


def test_missing_seam_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An older core without `beads_backend` fails loudly, not silently."""
    # Ensure any cached real/fake module is gone, then block the import.
    monkeypatch.delitem(
        sys.modules, "prefect_orchestration.beads_backend", raising=False
    )
    real_import = __import__

    def _blocked_import(name, *args, **kwargs):
        if name == "prefect_orchestration.beads_backend":
            raise ImportError("no module named beads_backend")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _blocked_import)

    with pytest.raises(SystemExit) as exc:
        write_verdict(bead_id="b1", name="triage", payload="{}")

    assert exc.value.code == 2
    assert "lacks prefect_orchestration.beads_backend" in capsys.readouterr().out
