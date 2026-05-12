from __future__ import annotations

from pathlib import Path

import pytest

from po_formulas.commands import planning_init


def test_product_scaffold_creates_expected_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    planning_init(kind="product", slug="agent-native-apps", title="Agent Native Apps")

    vision = (
        tmp_path / ".planning/products/agent-native-apps/agent-native-apps-vision.md"
    )
    epics = tmp_path / ".planning/products/agent-native-apps/agent-native-apps-epics.md"

    assert vision.exists()
    assert epics.exists()
    assert "# Agent Native Apps Vision" in vision.read_text()
    assert "## Success Signals" in vision.read_text()
    assert "# Agent Native Apps Epic Outline" in epics.read_text()


def test_epic_scaffold_creates_expected_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    planning_init(
        kind="epic", slug="roadmap-first-planning", title="Roadmap First Planning"
    )

    base = tmp_path / ".planning/epics/roadmap-first-planning"
    brainstorm = base / "roadmap-first-planning-brainstorm.md"
    design = base / "roadmap-first-planning-design.md"
    epic_plan = base / "roadmap-first-planning-epic-plan.md"
    issues = base / "roadmap-first-planning-issues.md"

    assert brainstorm.exists()
    assert design.exists()
    assert epic_plan.exists()
    assert issues.exists()
    assert "# Roadmap First Planning Brainstorm" in brainstorm.read_text()
    assert "## Proposed Approach" in design.read_text()
    assert "## Dispatch Plan" in epic_plan.read_text()
    assert "## Inline vs PO Routing" in issues.read_text()


def test_existing_file_blocks_rerun_without_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    planning_init(kind="product", slug="agent-native-apps", title="Agent Native Apps")
    vision = (
        tmp_path / ".planning/products/agent-native-apps/agent-native-apps-vision.md"
    )
    original = "custom content\n"
    vision.write_text(original)

    with pytest.raises(SystemExit) as excinfo:
        planning_init(
            kind="product",
            slug="agent-native-apps",
            title="Agent Native Apps",
        )

    assert excinfo.value.code == 2
    assert vision.read_text() == original


def test_invalid_kind_exits_with_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as excinfo:
        planning_init(kind="feature", slug="agent-native-apps")

    assert excinfo.value.code == 2
