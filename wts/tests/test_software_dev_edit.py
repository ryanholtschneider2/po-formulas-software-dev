"""Smoke test: software_dev_edit (wts fork) runs plan→build→close with no lint step."""

import inspect

from po_formulas_wts.software_dev import software_dev_edit


def test_no_lint_step_in_edit():
    src = inspect.getsource(software_dev_edit)
    assert "linter" not in src, "software_dev_edit must not call the linter"
    assert 'step="lint"' not in src and "step='lint'" not in src


def test_docstring_matches_pipeline():
    doc = software_dev_edit.__doc__ or ""
    assert "plan → build → close" in doc
    first_line = doc.strip().splitlines()[0]
    assert "lint" not in first_line.lower()


def test_flow_name_ends_with_wts():
    assert getattr(software_dev_edit, "name", "").endswith("_wts"), (
        f"WTS flow name must end in _wts, got: {getattr(software_dev_edit, 'name', None)}"
    )
