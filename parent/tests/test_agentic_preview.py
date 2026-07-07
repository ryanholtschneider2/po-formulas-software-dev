"""Unit tests for the preview/demo knobs of `software_dev_agentic`
(po-formulas-software-dev-rha).

The agentic flow ends a run with a reachable preview where applicable,
surfaced as `po.preview_url` bead metadata. Per-rig knobs come from
`.po-env`: `PO_PREVIEW=local|cloud|off` and `PO_DEMO_VIDEO=0|1`.

Design under test (verdict-file pattern — never parse agent reply text):

  * the worker is handed a `preview_note` instruction block via ctx,
  * the worker writes its URL to `<run_dir>/preview_url.txt`,
  * on a critic pass the flow reads that file and stamps `po.preview_url`.

`agentic.agent_step` / `subprocess` are monkeypatched — no real agents,
no real `bd`.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from prefect_orchestration.agent_step import AgentStepResult

import po_formulas.agentic as ag
from po_formulas.software_dev import _load_rig_env

_NULL_LOGGER = logging.getLogger("po_formulas.agentic.preview.test")


# ─────────── .po-env round-trip (the knobs' real config path) ────────


def test_po_env_drives_preview_knobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`<rig>/.po-env` is the documented per-rig config for the knobs."""
    monkeypatch.delenv("PO_PREVIEW", raising=False)
    monkeypatch.delenv("PO_DEMO_VIDEO", raising=False)
    (tmp_path / ".po-env").write_text("PO_PREVIEW=cloud\nPO_DEMO_VIDEO=1\n")

    _load_rig_env(tmp_path)

    assert ag._resolve_preview_mode() == "cloud"
    assert ag._demo_video_requested() is True


# ─────────────────────── _resolve_preview_mode ──────────────────────


def test_preview_mode_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PO_PREVIEW", raising=False)
    assert ag._resolve_preview_mode() == "off"


@pytest.mark.parametrize("val", ["local", "LOCAL", "  cloud  ", "off"])
def test_preview_mode_accepts_valid(monkeypatch: pytest.MonkeyPatch, val: str) -> None:
    monkeypatch.setenv("PO_PREVIEW", val)
    assert ag._resolve_preview_mode() == val.strip().lower()


@pytest.mark.parametrize("val", ["", "bogus", "localhost", "1"])
def test_preview_mode_invalid_falls_back_off(
    monkeypatch: pytest.MonkeyPatch, val: str
) -> None:
    monkeypatch.setenv("PO_PREVIEW", val)
    assert ag._resolve_preview_mode() == "off"


# ─────────────────────── _demo_video_requested ──────────────────────


def test_demo_video_defaults_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PO_DEMO_VIDEO", raising=False)
    assert ag._demo_video_requested() is False


@pytest.mark.parametrize("val", ["1", "true", "YES", "on"])
def test_demo_video_truthy(monkeypatch: pytest.MonkeyPatch, val: str) -> None:
    monkeypatch.setenv("PO_DEMO_VIDEO", val)
    assert ag._demo_video_requested() is True


@pytest.mark.parametrize("val", ["0", "false", "no", ""])
def test_demo_video_falsy(monkeypatch: pytest.MonkeyPatch, val: str) -> None:
    monkeypatch.setenv("PO_DEMO_VIDEO", val)
    assert ag._demo_video_requested() is False


# ─────────────────────── _preview_note ──────────────────────────────


def test_preview_note_off_and_no_demo_is_empty(tmp_path: Path) -> None:
    assert ag._preview_note("off", tmp_path, demo=False) == ""


def test_preview_note_local_mentions_localhost_and_file(tmp_path: Path) -> None:
    note = ag._preview_note("local", tmp_path, demo=False)
    assert "PO_PREVIEW=local" in note
    assert "localhost" in note
    assert str(tmp_path / ag._PREVIEW_URL_FILE) in note
    assert "po.preview_url" in note


def test_preview_note_cloud_mentions_rpreview(tmp_path: Path) -> None:
    note = ag._preview_note("cloud", tmp_path, demo=False)
    assert "PO_PREVIEW=cloud" in note
    assert "rpreview" in note
    assert str(tmp_path / ag._PREVIEW_URL_FILE) in note


def test_preview_note_demo_appends_block(tmp_path: Path) -> None:
    note = ag._preview_note("off", tmp_path, demo=True)
    assert "PO_DEMO_VIDEO=1" in note
    assert "demo video" in note.lower()


def test_preview_note_local_plus_demo_has_both(tmp_path: Path) -> None:
    note = ag._preview_note("local", tmp_path, demo=True)
    assert "localhost" in note
    assert "PO_DEMO_VIDEO=1" in note


# ─────────────────────── _stamp_preview_url ─────────────────────────


def _capture_bd(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record argv of every `subprocess.run` the module makes."""
    calls: list[list[str]] = []

    def fake_run(argv, **kw):  # noqa: ANN001, ANN003
        calls.append(list(argv))

        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr(ag.subprocess, "run", fake_run)
    return calls


def test_stamp_preview_url_reads_file_and_stamps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _capture_bd(monkeypatch)
    (tmp_path / ag._PREVIEW_URL_FILE).write_text("http://localhost:5173\n")

    url = ag._stamp_preview_url("seed-1", tmp_path, tmp_path)

    assert url == "http://localhost:5173"
    assert calls == [
        [
            "bd",
            "update",
            "seed-1",
            "--set-metadata",
            "po.preview_url=http://localhost:5173",
        ]
    ]


def test_stamp_preview_url_missing_file_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _capture_bd(monkeypatch)
    assert ag._stamp_preview_url("seed-1", tmp_path, tmp_path) == ""
    assert calls == []


def test_stamp_preview_url_empty_file_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _capture_bd(monkeypatch)
    (tmp_path / ag._PREVIEW_URL_FILE).write_text("\n   \n")
    assert ag._stamp_preview_url("seed-1", tmp_path, tmp_path) == ""
    assert calls == []


def test_stamp_preview_url_takes_last_nonempty_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _capture_bd(monkeypatch)
    (tmp_path / ag._PREVIEW_URL_FILE).write_text(
        "started dev server on :5173\nhttp://localhost:5173\n"
    )
    url = ag._stamp_preview_url("seed-1", tmp_path, tmp_path)
    assert url == "http://localhost:5173"
    assert calls[0][-1] == "po.preview_url=http://localhost:5173"


# ─────────────────────── flow integration ───────────────────────────


def _patch_common(monkeypatch: pytest.MonkeyPatch, closed: list[str]) -> None:
    monkeypatch.setattr(ag, "get_run_logger", lambda: _NULL_LOGGER)
    monkeypatch.setattr(ag, "claim_issue", lambda *a, **kw: None)
    monkeypatch.setattr(ag, "close_issue", lambda iid, *a, **kw: closed.append(iid))


def test_flow_stamps_preview_url_on_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PO_PREVIEW=local + worker writes preview_url.txt → flow stamps it."""
    monkeypatch.setenv("PO_PREVIEW", "local")
    monkeypatch.delenv("PO_DEMO_VIDEO", raising=False)

    rig = tmp_path / "rig"
    run_dir = rig / ".planning" / "software-dev-agentic" / "seed-p"
    run_dir.mkdir(parents=True)

    calls: list[dict] = []
    bd_calls = _capture_bd(monkeypatch)

    def fake(**kw: object) -> AgentStepResult:
        calls.append(dict(kw))
        step = kw.get("step")
        bead = f"{kw['seed_id']}.{step}.iter{kw.get('iter_n')}"
        if step == "agentic":
            # The worker leaves a reachable preview behind.
            (run_dir / ag._PREVIEW_URL_FILE).write_text("http://localhost:5173\n")
            return AgentStepResult(bead_id=bead, verdict="complete", closed_by="agent")
        return AgentStepResult(bead_id=bead, verdict="pass", closed_by="agent")

    monkeypatch.setattr(ag, "agent_step", fake)
    closed: list[str] = []
    _patch_common(monkeypatch, closed)

    result = ag.software_dev_agentic.fn(
        issue_id="seed-p", rig="rig", rig_path=str(rig), iter_cap=1
    )

    assert result["preview_url"] == "http://localhost:5173"
    # The worker turn carried the preview instruction block.
    worker = next(c for c in calls if c.get("step") == "agentic")
    assert "localhost" in worker["ctx"]["preview_note"]
    # The flow stamped po.preview_url exactly once.
    stamp = [c for c in bd_calls if "po.preview_url=http://localhost:5173" in c]
    assert len(stamp) == 1
    assert closed == ["seed-p"]


def test_flow_off_mode_stamps_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default PO_PREVIEW=off → empty preview_note, no stamp (back-compat)."""
    monkeypatch.delenv("PO_PREVIEW", raising=False)
    monkeypatch.delenv("PO_DEMO_VIDEO", raising=False)

    rig = tmp_path / "rig"
    rig.mkdir()

    calls: list[dict] = []
    bd_calls = _capture_bd(monkeypatch)

    def fake(**kw: object) -> AgentStepResult:
        calls.append(dict(kw))
        step = kw.get("step")
        bead = f"{kw['seed_id']}.{step}.iter{kw.get('iter_n')}"
        verdict = "pass" if step in {"design-review", "review"} else "complete"
        return AgentStepResult(bead_id=bead, verdict=verdict, closed_by="agent")

    monkeypatch.setattr(ag, "agent_step", fake)
    closed: list[str] = []
    _patch_common(monkeypatch, closed)

    result = ag.software_dev_agentic.fn(
        issue_id="seed-o", rig="rig", rig_path=str(rig), iter_cap=1
    )

    assert result["preview_url"] == ""
    worker = next(c for c in calls if c.get("step") == "agentic")
    assert worker["ctx"]["preview_note"] == ""
    assert [c for c in bd_calls if any("po.preview_url" in p for p in c)] == []
