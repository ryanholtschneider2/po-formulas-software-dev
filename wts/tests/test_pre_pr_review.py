"""Unit tests for po_formulas_wts.pre_pr_review.

Coverage map (per plan §Verification Strategy):
  test_mutual_exclusion_both_none        → ValueError when neither --epic-id nor --branch
  test_mutual_exclusion_both_set         → ValueError when both --epic-id and --branch given
  test_parse_pillar2_findings_three      → parser extracts 3 (title, body) tuples from fixture
  test_parse_pillar2_findings_approved   → parser returns [] when ## Findings is empty
  test_missing_worktree_blocks_run       → pillar-0-prelude.md written, validation=blocked stamped
  test_pillar1_regression_files_bug      → pillar-1 FAIL → bd create --type=bug called
  test_devup_unsupported_skips           → make -n dev-up rc=2 → pillar-3 SKIPPED, report says dev_env_bootable=false
  test_baseline_checkout_restores_on_exception → prior HEAD restored after exception inside ctx
  test_report_section_headings           → deterministic H1 ordering in validation-report.md
  test_pillar3_teardown_after_exception  → teardown called even when agent_step raises
  test_pillar2_findings_parsed_and_filed → 3-finding fixture → 3 bd create --type=bug calls
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import po_formulas_wts.pre_pr_review as ppr
from po_formulas_wts.pre_pr_review import (
    PillarResult,
    _VERDICT_FAILED,
    _VERDICT_PASSED,
    _VERDICT_SKIPPED,
    _baseline_checkout,
    _existing_open_finding_titles,
    _open_child_ids,
    _parse_pillar2_findings,
    _quiescence_block_reason,
    _worktree_dirty_paths,
)

FIXTURES = Path(__file__).parent / "fixtures"


# ─────────────────────── helpers ─────────────────────────────────────


def _cp(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=""
    )


def _patch_subprocess(monkeypatch, side_effect_fn=None):
    """Patch both ppr.subprocess.run and ppr._run to the fake."""
    fake = (
        MagicMock(side_effect=side_effect_fn)
        if side_effect_fn
        else MagicMock(return_value=_cp())
    )
    monkeypatch.setattr(ppr.subprocess, "run", fake)
    monkeypatch.setattr(ppr, "_run", lambda cmd, **kw: fake(cmd, **kw))
    return fake


# ─────────────────────── mutual exclusion ────────────────────────────


def test_mutual_exclusion_both_none(tmp_path):
    with pytest.raises(ValueError, match="exactly one"):
        ppr.pre_pr_review.fn(epic_id=None, branch=None, rig_path=str(tmp_path))


def test_mutual_exclusion_both_set(tmp_path):
    with pytest.raises(ValueError, match="exactly one"):
        ppr.pre_pr_review.fn(
            epic_id="nanocorps-abc", branch="my-branch", rig_path=str(tmp_path)
        )


# ─────────────────────── pillar-2 parser ─────────────────────────────


def test_parse_pillar2_findings_three():
    text = (FIXTURES / "pillar-2-critique-3-findings.md").read_text()
    findings = _parse_pillar2_findings(text)
    assert len(findings) == 3
    assert findings[0][0] == "Pillar-3 smoke test not implemented"
    assert findings[1][0] == "Missing mutation in metadata.validation"
    assert findings[2][0] == "Report heading order not deterministic"
    # each body is non-empty
    for _, body in findings:
        assert body.strip()


def test_parse_pillar2_findings_approved():
    text = "# Pillar 2\n\n**Verdict:** approved\n\n## Findings\n\n"
    findings = _parse_pillar2_findings(text)
    assert findings == []


# ─────────────────────── missing worktree ────────────────────────────


def test_missing_worktree_blocks_run(tmp_path, monkeypatch):
    bd_calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        if "bd" in cmd:
            bd_calls.append(list(cmd))
        return _cp()

    monkeypatch.setattr(
        ppr,
        "_bd_show_metadata",
        lambda *a, **kw: {"work_dir": "", "no_worktree": "false"},
    )
    monkeypatch.setattr(ppr, "_run", fake_run)
    monkeypatch.setattr(ppr.subprocess, "run", lambda cmd, **kw: _cp())

    result = ppr.pre_pr_review.fn(
        epic_id="nanocorps-test", branch=None, rig_path=str(tmp_path), dry_run=True
    )

    # All three pillars should be SKIPPED
    assert result["pillars"]["pillar-1"] == _VERDICT_SKIPPED
    assert result["pillars"]["pillar-2"] == _VERDICT_SKIPPED
    assert result["pillars"]["pillar-3"] == _VERDICT_SKIPPED

    # pillar-0-prelude.md must be written
    prelude = list(tmp_path.glob("**/*prelude*"))
    assert prelude, "pillar-0-prelude.md not found"


# ─────────────────────── pillar-1 regression → bead ─────────────────


def test_pillar1_regression_files_bug(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ppr, "_resolve_worktree", lambda *a, **kw: (tmp_path, "my-branch", "main", [])
    )
    # pillar-1 uses _make_target_outcomes + _baseline_checkout; stub both
    monkeypatch.setattr(
        ppr,
        "_run_pillar_1",
        lambda *a, **kw: PillarResult(
            name="pillar-1",
            verdict=_VERDICT_FAILED,
            findings=[("lint", "FAILED log")],
        ),
    )
    monkeypatch.setattr(
        ppr,
        "_run_pillar_2",
        lambda *a, **kw: PillarResult(name="pillar-2", verdict=_VERDICT_SKIPPED),
    )
    monkeypatch.setattr(
        ppr,
        "_run_pillar_3",
        lambda *a, **kw: PillarResult(name="pillar-3", verdict=_VERDICT_SKIPPED),
    )

    bd_create_calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        if "create" in cmd:
            bd_create_calls.append(list(cmd))
        return _cp()

    monkeypatch.setattr(ppr, "_run", fake_run)
    monkeypatch.setattr(ppr.subprocess, "run", lambda cmd, **kw: _cp())

    result = ppr.pre_pr_review.fn(
        epic_id="nanocorps-test", branch=None, rig_path=str(tmp_path), dry_run=False
    )

    # Pillar-1 should have FAILED verdict
    assert result["pillars"]["pillar-1"] == _VERDICT_FAILED
    # Regression findings should be filed as bugs
    assert any(
        "--type=bug" in " ".join(c) or "type=bug" in " ".join(c)
        for c in bd_create_calls
    ), f"no bug bead created; bd calls: {bd_create_calls}"


def _record(lst, cmd):
    lst.append(list(cmd))
    return None


# ─────────────────────── pillar-3 dev-up unsupported ─────────────────


def test_devup_unsupported_skips(tmp_path, monkeypatch):
    monkeypatch.setattr(ppr, "_devup_supported", lambda work_dir: False)
    result = ppr._run_pillar_3(
        work_dir=tmp_path,
        rig_path=tmp_path,
        report_dir=tmp_path,
        seed_id="nanocorps-test",
        dry_run=True,
    )
    assert result.verdict == _VERDICT_SKIPPED
    prelude = tmp_path / "pillar-3-prelude.md"
    assert prelude.exists()
    assert "dev_env_bootable=false" in prelude.read_text()


# ─────────────────────── baseline checkout crash-safe ────────────────


def test_baseline_checkout_restores_on_exception(tmp_path, monkeypatch):
    restored: list[str] = []

    def fake_run(cmd, **kw):
        if "stash" in cmd and "create" in cmd:
            return _cp(stdout="stash-ref-abc")
        if "rev-parse" in cmd:
            return _cp(stdout="abc123")
        if "checkout" in cmd:
            if "origin/main" in cmd:
                return _cp()
            # capture restore
            restored.append(cmd[-1])
            return _cp()
        if "stash" in cmd and "apply" in cmd:
            return _cp()
        return _cp()

    monkeypatch.setattr(ppr, "_run", fake_run)

    with pytest.raises(RuntimeError, match="deliberate"):
        with _baseline_checkout(tmp_path, "main"):
            raise RuntimeError("deliberate test exception")

    # prior HEAD (abc123) should have been restored
    assert "abc123" in restored


# ─────────────────────── pillar-3 teardown after exception ───────────


def test_pillar3_teardown_after_exception(tmp_path, monkeypatch):
    teardown_called = []

    def fake_teardown(work_dir, proc):
        teardown_called.append(True)
        return True

    monkeypatch.setattr(ppr, "_devup_supported", lambda work_dir: True)
    monkeypatch.setattr(ppr, "_teardown_devenv", fake_teardown)

    # Fake Popen that's already done
    mock_proc = MagicMock()
    mock_proc.pid = 99999
    mock_proc.poll.return_value = None

    monkeypatch.setattr(ppr.subprocess, "Popen", lambda *a, **kw: mock_proc)
    monkeypatch.setattr(
        ppr, "_run", lambda cmd, **kw: _cp(returncode=1, stdout="dev-up started")
    )

    # Make agent_step raise to simulate mid-pillar failure
    monkeypatch.setattr(
        ppr, "_agent_step_task", MagicMock(side_effect=RuntimeError("agent crashed"))
    )

    with pytest.raises(RuntimeError, match="agent crashed"):
        ppr._run_pillar_3(
            work_dir=tmp_path,
            rig_path=tmp_path,
            report_dir=tmp_path,
            seed_id="nanocorps-test",
            dry_run=True,
        )

    # teardown must have been called even though agent_step raised
    assert teardown_called, "teardown not called after agent_step exception"


# ─────────────────────── report heading order ────────────────────────


def test_report_section_headings(tmp_path):
    p1 = PillarResult(name="pillar-1", verdict=_VERDICT_PASSED, summary="ok")
    p2 = PillarResult(name="pillar-2", verdict=_VERDICT_PASSED, summary="ok")
    p3 = PillarResult(name="pillar-3", verdict=_VERDICT_SKIPPED, summary="skipped")
    report_path = ppr._write_validation_report(
        tmp_path, p1, p2, p3, [], "my-branch", "main"
    )
    text = report_path.read_text()
    h1_positions = {
        "pillar1": text.find("# Pillar 1"),
        "pillar2": text.find("# Pillar 2"),
        "pillar3": text.find("# Pillar 3"),
        "summary": text.find("# Summary"),
    }
    assert h1_positions["pillar1"] < h1_positions["pillar2"]
    assert h1_positions["pillar2"] < h1_positions["pillar3"]
    assert h1_positions["pillar3"] < h1_positions["summary"]


# ─────────────────────── pillar-2 findings → beads ───────────────────


def test_pillar2_findings_parsed_and_filed(tmp_path, monkeypatch):
    bd_create_calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        if "create" in cmd:
            bd_create_calls.append(list(cmd))
        return _cp()

    monkeypatch.setattr(ppr, "_run", fake_run)

    text = (FIXTURES / "pillar-2-critique-3-findings.md").read_text()
    raw_findings = _parse_pillar2_findings(text)
    # _file_findings_as_beads expects (source_pillar, title, body) triples
    findings = [("pillar-2", title, body) for title, body in raw_findings]
    ppr._file_findings_as_beads(findings, "nanocorps-test", tmp_path, dry_run=False)

    assert len(bd_create_calls) == 3
    for c in bd_create_calls:
        joined = " ".join(c)
        assert "--type=bug" in joined or "type=bug" in joined
        assert "priority=1" in joined or "--priority=1" in joined


# ─────────────────────── pillar 2 ref resolution ─────────────────────


def test_pillar2_uses_local_merge_target_when_no_origin(tmp_path, monkeypatch):
    """When local 'main' exists, cumulative diff uses 'main..branch', not 'origin/main..branch'."""
    diff_calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        if cmd[:3] == ["git", "rev-parse", "--verify"]:
            # local 'main' exists
            return _cp(stdout="abc123", returncode=0)
        if "diff" in cmd:
            diff_calls.append(list(cmd))
        return _cp(stdout="")

    monkeypatch.setattr(ppr, "_run", fake_run)

    report_dir = tmp_path / "report"
    report_dir.mkdir()
    ppr._stage_pillar2_inputs(
        None, "wts-branch", tmp_path, "main", tmp_path, report_dir
    )

    assert len(diff_calls) == 1
    assert diff_calls[0][2] == "main..wts-branch"


def test_pillar2_falls_back_to_origin_when_local_branch_missing(tmp_path, monkeypatch):
    """When local 'main' doesn't exist, cumulative diff falls back to 'origin/main..branch'."""
    diff_calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        if cmd[:3] == ["git", "rev-parse", "--verify"]:
            # local 'main' not found
            return _cp(stdout="", returncode=128)
        if "diff" in cmd:
            diff_calls.append(list(cmd))
        return _cp(stdout="")

    monkeypatch.setattr(ppr, "_run", fake_run)

    report_dir = tmp_path / "report"
    report_dir.mkdir()
    ppr._stage_pillar2_inputs(
        None, "wts-branch", tmp_path, "main", tmp_path, report_dir
    )

    assert len(diff_calls) == 1
    assert diff_calls[0][2] == "origin/main..wts-branch"


# ─────────────────────── 3c: tool-error not a finding ────────────────


def test_baseline_checkout_cleans_tree_before_checkout(tmp_path, monkeypatch):
    """3c: a dirty tree must not abort the baseline checkout.

    `git stash create` only snapshots; the context must `git reset --hard`
    before `git checkout origin/<target>` so local tracked edits can't block
    it. The snapshot is re-applied on exit (lossless).
    """
    cmds: list[list[str]] = []

    def fake_run(cmd, **kw):
        cmds.append(list(cmd))
        if "stash" in cmd and "create" in cmd:
            return _cp(stdout="stash-ref-xyz")
        if "rev-parse" in cmd:
            return _cp(stdout="head-sha")
        return _cp()

    monkeypatch.setattr(ppr, "_run", fake_run)

    with _baseline_checkout(tmp_path, "main"):
        pass

    # A `git reset --hard HEAD` must precede the `git checkout origin/main`.
    reset_idx = next(
        i for i, c in enumerate(cmds) if c[:3] == ["git", "reset", "--hard"]
    )
    checkout_idx = next(
        i
        for i, c in enumerate(cmds)
        if c[:2] == ["git", "checkout"] and "origin/main" in c
    )
    assert reset_idx < checkout_idx
    # And the snapshot is re-applied afterwards (lossless restore).
    assert any(c[:2] == ["git", "stash"] and "apply" in c for c in cmds)


def test_baseline_checkout_skips_reset_when_tree_clean(tmp_path, monkeypatch):
    """No snapshot (clean tree) → no destructive reset is issued."""
    cmds: list[list[str]] = []

    def fake_run(cmd, **kw):
        cmds.append(list(cmd))
        if "stash" in cmd and "create" in cmd:
            return _cp(stdout="")  # clean tree → empty stash ref
        if "rev-parse" in cmd:
            return _cp(stdout="head-sha")
        return _cp()

    monkeypatch.setattr(ppr, "_run", fake_run)

    with _baseline_checkout(tmp_path, "main"):
        pass

    assert not any(c[:3] == ["git", "reset", "--hard"] for c in cmds)


def test_pillar2_diff_failure_fails_loud_not_zero_diff(tmp_path, monkeypatch):
    """3c: a failed cumulative diff → pillar-2 FAILED with the real error, no findings."""

    def fake_run(cmd, **kw):
        if cmd[:3] == ["git", "rev-parse", "--verify"]:
            return _cp(stdout="", returncode=128)  # local ref missing
        if "diff" in cmd:
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=128,
                stdout="",
                stderr="fatal: ambiguous argument '(unknown)'",
            )
        return _cp()

    monkeypatch.setattr(ppr, "_run", fake_run)
    # Guard: the agent must NOT be dispatched when the diff couldn't be built.
    monkeypatch.setattr(
        ppr,
        "_agent_step_task",
        MagicMock(side_effect=AssertionError("agent dispatched despite diff failure")),
    )

    report_dir = tmp_path / "report"
    report_dir.mkdir()
    result = ppr._run_pillar_2(
        None,
        "(unknown)",
        tmp_path,
        tmp_path,
        "main",
        report_dir,
        seed_id="seed",
        dry_run=False,
    )

    assert result.verdict == _VERDICT_FAILED
    assert "could not compute cumulative diff" in result.summary
    assert result.findings == []  # NOT a fabricated "zero diff" finding
    assert "ambiguous argument" in result.summary
    # The staged diff file records the real error, not an empty diff.
    assert (
        "could not compute cumulative diff"
        in (report_dir / "cumulative.diff").read_text()
    )


def test_stage_pillar2_inputs_returns_none_on_success(tmp_path, monkeypatch):
    """A successful diff (rc=0) returns None — no error signalled."""

    def fake_run(cmd, **kw):
        if cmd[:3] == ["git", "rev-parse", "--verify"]:
            return _cp(stdout="abc", returncode=0)
        return _cp(stdout="some diff", returncode=0)

    monkeypatch.setattr(ppr, "_run", fake_run)
    report_dir = tmp_path / "report"
    report_dir.mkdir()
    err = ppr._stage_pillar2_inputs(None, "br", tmp_path, "main", tmp_path, report_dir)
    assert err is None
    assert (report_dir / "cumulative.diff").read_text() == "some diff"


# ─────────────────────── 3b: dedup finding emission ──────────────────


def test_existing_open_finding_titles_filters_closed(tmp_path, monkeypatch):
    rows = (
        '[{"issue_id":"e.1","title":"[pillar-2] A","status":"open"},'
        '{"issue_id":"e.2","title":"[pillar-2] B","status":"closed"},'
        '{"issue_id":"e.3","title":"[pillar-1] make lint","status":"in_progress"}]'
    )
    monkeypatch.setattr(ppr, "_run", lambda cmd, **kw: _cp(stdout=rows))
    titles = _existing_open_finding_titles("e", tmp_path)
    assert titles == {"[pillar-2] A", "[pillar-1] make lint"}


def test_dedup_skips_finding_matching_open_child(tmp_path, monkeypatch):
    """3b: a finding whose title already exists as an open child is not re-filed."""
    create_calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        if cmd[:3] == ["bd", "dep", "list"]:
            return _cp(
                stdout='[{"issue_id":"e.1","title":"[pillar-2] Dup finding","status":"open"}]'
            )
        if "create" in cmd:
            create_calls.append(list(cmd))
        return _cp()

    monkeypatch.setattr(ppr, "_run", fake_run)

    findings = [
        ("pillar-2", "Dup finding", "body"),  # already open → skip
        ("pillar-2", "Fresh finding", "body"),  # new → file
    ]
    ids = ppr._file_findings_as_beads(findings, "e", tmp_path, dry_run=False)

    assert len(create_calls) == 1
    assert any("[pillar-2] Fresh finding" in " ".join(c) for c in create_calls)
    assert all("Dup finding" not in " ".join(c) for c in create_calls)
    assert len(ids) == 1


def test_dedup_within_batch(tmp_path, monkeypatch):
    """Identical findings in one batch are filed once."""
    create_calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        if cmd[:3] == ["bd", "dep", "list"]:
            return _cp(stdout="[]")
        if "create" in cmd:
            create_calls.append(list(cmd))
        return _cp()

    monkeypatch.setattr(ppr, "_run", fake_run)

    findings = [
        ("pillar-2", "Same", "body1"),
        ("pillar-2", "Same", "body2"),
    ]
    ppr._file_findings_as_beads(findings, "e", tmp_path, dry_run=False)
    assert len(create_calls) == 1


# ─────────────────────── 3a: quiescence gate ─────────────────────────


def test_worktree_dirty_paths_parses_porcelain(tmp_path, monkeypatch):
    porcelain = " M cdr/http/routes/minerals.py\nM  staged.py\n"
    monkeypatch.setattr(ppr, "_run", lambda cmd, **kw: _cp(stdout=porcelain))
    paths = _worktree_dirty_paths(tmp_path)
    assert paths == ["cdr/http/routes/minerals.py", "staged.py"]


def test_worktree_dirty_paths_clean(tmp_path, monkeypatch):
    monkeypatch.setattr(ppr, "_run", lambda cmd, **kw: _cp(stdout=""))
    assert _worktree_dirty_paths(tmp_path) == []


def test_open_child_ids_skips_closed(tmp_path, monkeypatch):
    rows = (
        '[{"issue_id":"e.1","status":"open"},'
        '{"issue_id":"e.2","status":"closed"},'
        '{"issue_id":"e.3","status":"in_progress"}]'
    )
    monkeypatch.setattr(ppr, "_run", lambda cmd, **kw: _cp(stdout=rows))
    assert _open_child_ids("e", tmp_path) == ["e.1", "e.3"]


def test_quiescence_reason_none_when_clean_and_closed(tmp_path, monkeypatch):
    def fake_run(cmd, **kw):
        if cmd[:2] == ["git", "status"]:
            return _cp(stdout="")  # clean
        if cmd[:3] == ["bd", "dep", "list"]:
            return _cp(stdout="[]")  # no open children
        return _cp()

    monkeypatch.setattr(ppr, "_run", fake_run)
    assert _quiescence_block_reason("e", tmp_path, tmp_path) is None


def test_quiescence_reason_reports_dirty_and_open(tmp_path, monkeypatch):
    def fake_run(cmd, **kw):
        if cmd[:2] == ["git", "status"]:
            return _cp(stdout=" M foo.py\n")
        if cmd[:3] == ["bd", "dep", "list"]:
            return _cp(stdout='[{"issue_id":"e.1","status":"open"}]')
        return _cp()

    monkeypatch.setattr(ppr, "_run", fake_run)
    reason = _quiescence_block_reason("e", tmp_path, tmp_path)
    assert reason is not None
    assert "uncommitted tracked changes" in reason
    assert "foo.py" in reason
    assert "1 child(ren) still open" in reason


def test_flow_blocks_on_dirty_tree_files_no_beads(tmp_path, monkeypatch):
    """3a: a dirty worktree blocks the run with no fanned-out finding-beads."""
    monkeypatch.setattr(
        ppr, "_resolve_worktree", lambda *a, **kw: (tmp_path, "wts-br", "main", [])
    )
    # If any pillar runs, the gate failed.
    monkeypatch.setattr(
        ppr,
        "_run_pillar_1",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("pillar-1 ran")),
    )

    bd_create_calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        if cmd[:2] == ["git", "status"]:
            return _cp(stdout=" M cdr/routes/minerals.py\n")
        if cmd[:3] == ["bd", "dep", "list"]:
            return _cp(stdout="[]")
        if "create" in cmd:
            bd_create_calls.append(list(cmd))
        return _cp()

    monkeypatch.setattr(ppr, "_run", fake_run)
    monkeypatch.setattr(ppr.subprocess, "run", lambda cmd, **kw: _cp())

    result = ppr.pre_pr_review.fn(
        epic_id="nanocorps-test", branch=None, rig_path=str(tmp_path), dry_run=True
    )

    assert result["validation"] == "blocked"
    assert "uncommitted tracked changes" in result["quiescence"]
    assert result["pillars"]["pillar-1"] == _VERDICT_SKIPPED
    assert result["bead_ids"] == []
    assert bd_create_calls == []  # no fabricated finding-beads
    # the not-quiesced prelude is written
    prelude = (
        tmp_path / ".planning" / "pre-pr-review" / "wts-br" / "pillar-0-prelude.md"
    )
    assert prelude.exists()
    assert "not quiesced" in prelude.read_text().lower()
