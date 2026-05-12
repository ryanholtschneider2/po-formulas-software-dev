"""
Unit tests for the po-formulas-software-dev-wts pack skeleton.

AC coverage:
- AC #1: New pack dir exists; pyproject.toml project name correct
- AC #2: Entry-point keys software-dev-full-wts + software-dev-fast-wts registered
- AC #3: No EP-key collision with originals (all four coexist in one venv)
- AC #4: Inner package is po_formulas_wts (not po_formulas), preventing sys.path shadowing
- AC #5: @flow(name=…) decorators end with _wts so Prefect flow registry is unambiguous
"""

import importlib.metadata
from pathlib import Path

PACK_ROOT = Path(__file__).resolve().parent.parent
FORMULAS_PKG = PACK_ROOT / "po_formulas_wts"


def test_pack_root_exists():
    assert PACK_ROOT.is_dir()
    assert (PACK_ROOT / "pyproject.toml").exists()
    assert (PACK_ROOT / "po_formulas_wts").is_dir()


def test_pyproject_name():
    text = (PACK_ROOT / "pyproject.toml").read_text()
    assert 'name = "po-formulas-software-dev-wts"' in text


def test_entry_points_registered():
    eps = {ep.name: ep for ep in importlib.metadata.entry_points(group="po.formulas")}
    assert "software-dev-full-wts" in eps, "software-dev-full-wts EP not found"
    assert "software-dev-fast-wts" in eps, "software-dev-fast-wts EP not found"
    assert "software-dev-edit-wts" in eps, "software-dev-edit-wts EP not found"
    assert "pr-writer" in eps, "pr-writer EP not found"


def test_no_collision_with_originals():
    eps = {ep.name: ep for ep in importlib.metadata.entry_points(group="po.formulas")}
    # All four should coexist
    assert "software-dev-full" in eps
    assert "software-dev-fast" in eps
    assert "software-dev-full-wts" in eps
    assert "software-dev-fast-wts" in eps


def test_entry_points_load_to_distinct_objects():
    eps = {ep.name: ep for ep in importlib.metadata.entry_points(group="po.formulas")}
    full_orig = eps["software-dev-full"].load()
    full_wts = eps["software-dev-full-wts"].load()
    fast_orig = eps["software-dev-fast"].load()
    fast_wts = eps["software-dev-fast-wts"].load()
    # Different callable objects (different packages)
    assert full_orig is not full_wts
    assert fast_orig is not fast_wts


def test_flow_names_end_with_wts():
    eps = {ep.name: ep for ep in importlib.metadata.entry_points(group="po.formulas")}
    full_wts = eps["software-dev-full-wts"].load()
    fast_wts = eps["software-dev-fast-wts"].load()
    edit_wts = eps["software-dev-edit-wts"].load()
    pr_writer = eps["pr-writer"].load()
    assert getattr(full_wts, "name", "").endswith("_wts"), (
        f"Expected flow name ending in _wts, got: {getattr(full_wts, 'name', None)}"
    )
    assert getattr(fast_wts, "name", "").endswith("_wts"), (
        f"Expected flow name ending in _wts, got: {getattr(fast_wts, 'name', None)}"
    )
    assert getattr(edit_wts, "name", "").endswith("_wts"), (
        f"Expected flow name ending in _wts, got: {getattr(edit_wts, 'name', None)}"
    )
    assert getattr(pr_writer, "name", "").endswith("_wts"), (
        f"Expected flow name ending in _wts, got: {getattr(pr_writer, 'name', None)}"
    )


def test_inner_package_is_po_formulas_wts():
    # sys.path shadowing guard: the package must NOT be named po_formulas
    assert FORMULAS_PKG.is_dir(), "po_formulas_wts/ package dir missing"
    orig_pkg = PACK_ROOT / "po_formulas"
    assert not orig_pkg.exists(), "po_formulas/ found — sys.path shadow risk"


def test_no_stale_po_formulas_imports():
    """All intra-package imports use po_formulas_wts, not po_formulas."""
    for py_file in FORMULAS_PKG.rglob("*.py"):
        text = py_file.read_text()
        # Allow 'po_formulas' only as a substring of 'po_formulas_wts'
        import re

        bad = re.findall(r"\bpo_formulas\b(?!_wts)", text)
        assert not bad, (
            f"{py_file.relative_to(PACK_ROOT)}: stale 'po_formulas' import(s): {bad[:3]}"
        )


def test_no_nested_git_dir():
    assert not (PACK_ROOT / ".git").exists(), (
        ".git/ should have been stripped from the wts copy"
    )


# AC: all role prompts include the metadata.work_dir cd prelude (nanocorps-dxt)
_WORK_DIR_SNIPPET = 'WORK_DIR=$(bd show'
_FALLTHROUGH_SNIPPET = '// empty'


def test_all_role_prompts_have_work_dir_prelude():
    """Every agent prompt.md must contain the work_dir cd block (AC #1)."""
    agents_dir = PACK_ROOT / "po_formulas_wts" / "agents"
    prompt_files = list(agents_dir.glob("*/prompt.md"))
    assert prompt_files, "No prompt.md files found under po_formulas_wts/agents/"
    missing = [p for p in prompt_files if _WORK_DIR_SNIPPET not in p.read_text()]
    assert not missing, (
        f"work_dir prelude missing from: {[str(p.relative_to(PACK_ROOT)) for p in missing]}"
    )


def test_work_dir_prelude_falls_through_when_absent():
    """The snippet must use '// empty' so jq returns '' when work_dir is unset (AC #2)."""
    agents_dir = PACK_ROOT / "po_formulas_wts" / "agents"
    prompt_files = list(agents_dir.glob("*/prompt.md"))
    missing_fallthrough = [
        p for p in prompt_files if _FALLTHROUGH_SNIPPET not in p.read_text()
    ]
    assert not missing_fallthrough, (
        f"fallthrough guard missing from: "
        f"{[str(p.relative_to(PACK_ROOT)) for p in missing_fallthrough]}"
    )
