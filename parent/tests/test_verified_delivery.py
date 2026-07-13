"""Contract and persistence tests for verified-delivery evidence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from po_formulas import verified_delivery as vd


def test_default_contract_contains_every_delivery_surface() -> None:
    contract = vd.normalize()
    assert contract["schema"] == "po.verified-delivery"
    assert contract["version"] == 1
    assert contract["revisions"] == {
        "base": None,
        "head": None,
        "integration": None,
    }
    assert contract["pull_request"] == {
        "number": None,
        "url": None,
        "target": None,
    }
    assert contract["acceptance_criteria"] == []
    assert contract["changed_surfaces"] == []
    assert contract["live_verification"] == {"plan": [], "results": []}
    assert contract["preview"] == {"url": None, "revision": None}
    assert contract["screenshots"] == []
    assert contract["demo"] == {"path": None, "url": None}
    assert contract["deferrals"] == []
    assert contract["terminal"]["state"] == "running"
    assert set(contract["provenance"]) == {
        "formula",
        "backend",
        "provider",
        "account",
        "account_class",
        "model",
        "effort",
        "rig",
        "rig_path",
        "pack_path",
        "parent_epic",
        "flow_run_id",
        "dispatch_command",
    }


def test_normalize_imports_legacy_bead_metadata() -> None:
    contract = vd.normalize(
        {
            "metadata": {
                "po.base_sha": "abc",
                "po.head_sha": "def",
                "po.pr_target": "release",
                "po.preview_url": "https://preview.example",
            }
        }
    )
    assert contract["revisions"]["base"] == "abc"
    assert contract["revisions"]["head"] == "def"
    assert contract["pull_request"]["target"] == "release"
    assert contract["preview"]["url"] == "https://preview.example"


def test_structured_values_win_over_legacy_values() -> None:
    contract = vd.normalize({"head_sha": "old", "revisions": {"head": "new"}})
    assert contract["revisions"]["head"] == "new"


def test_update_deep_merges_and_preserves_unknown_keys(tmp_path: Path) -> None:
    vd.update(
        tmp_path,
        {
            "changed_surfaces": ["api"],
            "provenance": {"model": "gpt-test"},
            "future_extension": {"enabled": True},
        },
    )
    contract = vd.update(
        tmp_path,
        {
            "changed_surfaces": ["api", "docs"],
            "provenance": {"effort": "high"},
        },
    )
    assert contract["changed_surfaces"] == ["api", "docs"]
    assert contract["provenance"]["model"] == "gpt-test"
    assert contract["provenance"]["effort"] == "high"
    assert contract["future_extension"] == {"enabled": True}
    assert json.loads((tmp_path / vd.ARTIFACT_NAME).read_text()) == contract


def test_read_rejects_malformed_or_incompatible_artifact(tmp_path: Path) -> None:
    path = tmp_path / vd.ARTIFACT_NAME
    path.write_text("not json")
    with pytest.raises(ValueError, match="invalid verified-delivery JSON"):
        vd.read(tmp_path)
    path.write_text('{"version": 2}')
    with pytest.raises(ValueError, match="version"):
        vd.read(tmp_path)


def test_failed_replace_preserves_previous_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = vd.update(tmp_path, {"revisions": {"head": "safe"}})

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("disk refused replace")

    monkeypatch.setattr(vd.os, "replace", fail_replace)
    with pytest.raises(OSError, match="disk refused"):
        vd.update(tmp_path, {"revisions": {"head": "unsafe"}})

    assert json.loads((tmp_path / vd.ARTIFACT_NAME).read_text()) == original
    assert list(tmp_path.glob(f".{vd.ARTIFACT_NAME}.*")) == []
