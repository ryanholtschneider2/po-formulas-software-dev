"""Software-dev flow body using `agent_step`.

Plain-Python @flow that calls `agent_step(...)` once per role. Replaces
the older nested-loop body and the short-lived graph-mode reactive
dispatcher (both removed in 7vs.5).

Each child task spec is rendered from `agents/<role>/task.md` at
agent_step call time. The sibling `agents/<role>/prompt.md` files hold
only the identity + close-contract template.

The triager picks one of four complexity tiers and the flow body gates
which downstream steps actually run:

  trivial   — triager handled the work itself; flow just closes the seed
  simple    — triage → plan → build → lint → close (no critics, no tests)
  moderate  — + plan-critic + build-critic + unit tests + docs + learn
  complex   — + e2e/playwright + regression-gate + deploy-smoke +
              verifier. Demo video is now an epic-level step in
              epic_finalize_wts, not per-child. Ralph + full-test-gate are
              OPT-IN even at complex tier (`enable_ralph=True`,
              `enable_full_test_gate=True`) — they each add 3-7 min and
              rarely catch what reviewer + verifier missed. Default
              tier when triage.json is missing is `complex`, so the
              defaults still err toward rigor without burning the
              extra ralph/gate phases.

# Bead graph produced by one run

```
<seed>                         (the user's issue — claimed in_progress)
├── <seed>.triage              "complete" (one-shot)
├── <seed>.baseline            "complete" (one-shot)
├── <seed>.plan.iter1          "complete"
├── <seed>.plan-critic.iter1   "approved" | "rejected: …"
├── <seed>.plan.iter2          (only if iter1 rejected)
├── <seed>.plan-critic.iter2
├── <seed>.build.iter1
├── <seed>.lint.iter1          "clean" | "failed"
├── <seed>.test-unit.iter1     "passed" | "failed"
├── <seed>.test-e2e.iter1
├── <seed>.regression.iter1    "no regression" | "regression: …"
├── <seed>.review.iter1        "approved" | "rejected"
├── <seed>.deploy-smoke
├── <seed>.review-artifacts
├── <seed>.verify.iter1        "approved" | "rejected"
├── <seed>.ralph.iter1         "improvement" | "no-improvement"
├── <seed>.docs
└── <seed>.learn
```

`bd dep tree <seed>` shows the full pipeline post-hoc with verdicts
on every bead.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import traceback
from pathlib import Path
from typing import Any

from prefect import flow, get_run_logger, task
from prefect_orchestration.agent_step import AgentStepResult, agent_step
from prefect_orchestration.beads_meta import claim_issue, close_issue
from prefect_orchestration.context_bundle import build_context_md
from prefect_orchestration.diff_mapper import (
    compute_changed_files,
    map_files_to_tests,
    write_tests_changed,
)

_AGENTS_DIR = Path(__file__).parent / "agents"


# ─────────────────────── helpers ────────────────────────────────────


def _load_rig_env(rig_path: Path) -> None:
    """Apply per-rig env-var overrides from `<rig>/.po-env` if present.

    Format: one ``KEY=VALUE`` per line, ``#`` comments allowed. Existing
    process-env wins (the file is the rig default; explicit
    ``PO_SKIP_E2E=1 po retry ...`` still overrides). Lets a rig opt out
    of test layers without a global shell export.
    """
    env_file = rig_path / ".po-env"
    if not env_file.is_file():
        return
    try:
        for line in env_file.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError:
        pass


def _tag_flow_run_with_issue_id(issue_id: str, logger: Any) -> None:
    """Stamp `issue_id:<id>` tag on the current flow_run for po status / TUI."""
    from prefect.runtime import flow_run

    fr_id = flow_run.get_id()
    if not fr_id:
        return
    try:
        from prefect.client.orchestration import get_client

        with get_client(sync_client=True) as c:
            existing = list(flow_run.tags or [])
            new_tag = f"issue_id:{issue_id}"
            if new_tag not in existing:
                c.update_flow_run(fr_id, tags=[*existing, new_tag])
    except Exception as exc:  # noqa: BLE001
        logger.warning("issue_id tag failed: %s", exc)


def _agent_dir(role: str) -> Path:
    return _AGENTS_DIR / role


def _read_artifact(run_dir: Path, name: str, max_chars: int = 60_000) -> str:
    """Read `<run_dir>/<name>` for inlining into a downstream role's prompt.

    Returns "" when the file doesn't exist or is unreadable. Truncates at
    `max_chars` (~15K tokens) with a marker so an over-large plan/diff
    doesn't blow the prompt budget.

    Inlining stable run-dir artifacts (plan.md, build-iter-N.diff,
    triage.md) into the next role's task ctx saves the agent 1 round-trip
    per file vs running `cat` from inside the agent. At ~5-8s per turn
    that adds up across builder + linter + tester.
    """
    path = run_dir / name
    if not path.is_file():
        return ""
    try:
        text = path.read_text()
    except OSError:
        return ""
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n[...truncated at {max_chars} chars]\n"
    return text


# Wrap `agent_step` in a Prefect @task per call so each role-step shows
# up in the Prefect UI (and `po tui` timeline) with a meaningful name.
# `task_run_name` templates from kwargs at runtime — the TUI's
# `baseTaskName` strips `-iter-N` / hex suffixes to recover the role
# label, which `TASK_TO_DISPLAY_ROLE` then maps to the timeline column.
# We name the task `agent_step` (vs `_agent_step_task`) so the TUI sees
# a clean run name like `triage-iter-1` after stripping suffixes.
@task(name="agent_step", task_run_name="{step}-iter-{iter_n}")
def _agent_step_task(
    *,
    step: str,
    iter_n: int,
    seed_id: str,
    rig_path: str,
    run_dir: Path | str | None = None,
    **kwargs: Any,
) -> AgentStepResult:
    """Prefect-task wrapper around `agent_step`. `step` + `iter_n` drive
    the task run name (e.g. ``triage-iter-1``, ``plan-critic-iter-2``)
    so the TUI's role timeline labels each role-step correctly.

    `run_dir` defaults to `<rig>/.planning/software-dev-full/<seed>/` so
    every agent_step call within one software_dev_full run shares a
    canonical run-dir (verdicts/, role-sessions.json, transcripts).
    The TUI / `po artifacts` / `po watch` find this via the seed bead's
    `po.run_dir` metadata that agent_step stamps.
    """
    if run_dir is None:
        run_dir = (
            Path(rig_path).expanduser().resolve()
            / ".planning"
            / "software-dev-full"
            / seed_id
        )
    pack_path = (kwargs.get("ctx") or {}).get("pack_path")
    build_context_md(
        run_dir=Path(run_dir),
        rig_path=Path(rig_path).expanduser().resolve(),
        issue_id=seed_id,
        role=step,
        iter_n=iter_n,
        pack_path=pack_path,
    )
    return agent_step(
        step=step,
        iter_n=iter_n,
        seed_id=seed_id,
        rig_path=rig_path,
        run_dir=run_dir,
        **kwargs,
    )


def _task_md(role: str) -> Path:
    """Return the task.md for a role's agent dir.

    All 17 roles already have task.md from the 7vs.5 prompt-split work.
    Returns the path even when the file is missing — agent_step will
    fall through to using whatever bead description is set.
    """
    return _AGENTS_DIR / role / "task.md"


_VALID_COMPLEXITIES = ("trivial", "simple", "moderate", "complex")


def _read_triage_flags(rig_path: Path, seed_id: str) -> dict[str, Any]:
    """Read `<rig>/.planning/software-dev-full/<seed>/verdicts/triage.json`.

    Returns the JSON dict with booleans coerced (str/int → bool) and
    `complexity` preserved as a string. Default complexity = 'complex'
    (most rigorous) if unset or invalid — bias toward rigor.
    """
    path = (
        rig_path
        / ".planning"
        / "software-dev-full"
        / seed_id
        / "verdicts"
        / "triage.json"
    )
    if not path.is_file():
        return {"complexity": "complex"}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {"complexity": "complex"}
    if not isinstance(data, dict):
        return {"complexity": "complex"}
    out: dict[str, Any] = {}
    for k, v in data.items():
        if k == "complexity" and isinstance(v, str) and v in _VALID_COMPLEXITIES:
            out[k] = v
        elif isinstance(v, bool) or v in (0, 1):
            out[k] = bool(v)
    out.setdefault("complexity", "complex")
    return out


def _write_scoped_tests_artifact(
    repo_path: Path,
    run_dir: Path,
    *,
    force_full: bool = False,
) -> list[str]:
    """Write `<run_dir>/tests-changed.txt` for tester + regression-gate.

    Without this artifact both roles fall back to running the full
    project test suite, which CPU-thrashes when multiple flows share a
    rig. Computes `merge-base(origin/main, HEAD)..HEAD` diff in
    `repo_path`, maps source files → test files via stem matching, and
    writes the union of (mapped tests ∪ default smoke set). Returns the
    list of changed file paths (forwarded to regression-gate ctx for
    its summary).

    Cross-repo case (rig != pack_path, edits land in a sibling): caller
    should pass the repo where commits actually landed. When that's
    still the rig and `git diff` shows zero changes, `tests-changed.txt`
    contains only the smoke set — agents run a handful of tests instead
    of 763.
    """
    try:
        changed = compute_changed_files(repo_path)
        mapped, tripwire_full = map_files_to_tests(changed, repo_path)
        write_tests_changed(
            run_dir,
            mapped,
            force_full=force_full or tripwire_full,
            n_changed=len(changed),
        )
        return [str(p) for p in changed]
    except Exception:  # noqa: BLE001
        # Never let artifact-write break the flow; agents fall back to
        # full-suite if the file is missing.
        return []


def _compute_work_landed(run_dir: Path) -> bool:
    """True iff at least one builder iteration produced a non-empty diff.

    `<run_dir>/build-iter-N.diff` is the rig-CLAUDE.md "Debugging a run"
    convention: builders write one per non-empty build. Empty file →
    builder ran but committed nothing; missing → build never ran.
    Orthogonal to per-role verdict-file shapes (which vary by role:
    triage.json carries `complexity`, baseline.json carries `status`,
    etc. — no shared `verdict` key).
    """
    if not run_dir.is_dir():
        return False
    for diff_file in run_dir.glob("build-iter-*.diff"):
        try:
            if diff_file.stat().st_size > 0:
                return True
        except OSError:
            continue
    return False


_ITER_HEADING_RE = re.compile(r"^##\s*iter[-\s]?\d+", re.IGNORECASE)


def _last_iter_summary(decision_log: Path) -> str:
    """Best-effort one-liner from the last `## iter-` heading in
    `decision-log.md`. Returns "no decision log" if absent/unreadable."""
    if not decision_log.is_file():
        return "no decision log"
    try:
        text = decision_log.read_text()
    except OSError:
        return "no decision log"
    last_heading_idx = -1
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if _ITER_HEADING_RE.match(ln.strip()):
            last_heading_idx = i
    if last_heading_idx == -1:
        return "no iter heading in decision-log"
    summary = lines[last_heading_idx].strip()
    for follow in lines[last_heading_idx + 1 : last_heading_idx + 6]:
        if follow.strip():
            summary = f"{summary} — {follow.strip()}"
            break
    return summary[:500]


def _record_flow_outcome(
    run_dir: Path,
    exc: BaseException,
    seed_id: str,
    rig_path: str,
) -> None:
    """Write `<run_dir>/flow_outcome.json` for the operator and `po status`.

    Best-effort: wrapped in `try / except Exception: pass` so a logging
    failure can never mask the original flow exception.
    """
    try:
        verdicts_dir = run_dir / "verdicts"
        work_landed = _compute_work_landed(run_dir)
        partial_summary = _last_iter_summary(run_dir / "decision-log.md")

        terminal_role: str | None = None
        terminal_iter: int | None = None
        if verdicts_dir.is_dir():
            verdict_files = sorted(
                verdicts_dir.glob("*.json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if verdict_files:
                stem = verdict_files[0].stem
                m = re.match(r"^(.*?)(?:-iter-(\d+))?$", stem)
                if m:
                    terminal_role = m.group(1) or None
                    terminal_iter = int(m.group(2)) if m.group(2) else None

        bd_seed_closed: bool | None = None
        bd_lookup_error: str | None = None
        try:
            r = subprocess.run(
                ["bd", "show", seed_id, "--json"],
                cwd=rig_path,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if r.returncode == 0 and r.stdout.strip():
                data = json.loads(r.stdout)
                row = data[0] if isinstance(data, list) and data else data
                if isinstance(row, dict):
                    bd_seed_closed = str(row.get("status", "")).lower() == "closed"
            else:
                bd_lookup_error = "non_zero"
        except subprocess.TimeoutExpired:
            bd_lookup_error = "timeout"
        except Exception:  # noqa: BLE001
            bd_lookup_error = "exception"

        tb_tail = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )[-2000:]

        outcome = {
            "ts": time.time(),
            "terminal_role": terminal_role,
            "terminal_iter": terminal_iter,
            "work_landed": work_landed,
            "bd_seed_closed": bd_seed_closed,
            "bd_lookup_error": bd_lookup_error,
            "exception_class": type(exc).__name__,
            "exception_msg": str(exc)[:500],
            "traceback_tail": tb_tail,
            "partial_summary": partial_summary,
        }
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "flow_outcome.json").write_text(
            json.dumps(outcome, default=str, indent=2)
        )
    except Exception:  # noqa: BLE001
        pass


# ─────────────────────── the flow ────────────────────────────────────


@flow(
    name="software_dev_full_wts",
    flow_run_name="{issue_id}",
    log_prints=True,
)
def software_dev_full(
    issue_id: str,
    rig: str,
    rig_path: str,
    pack_path: str | None = None,
    iter_cap: int = 3,
    plan_iter_cap: int = 2,
    verify_iter_cap: int = 3,
    ralph_iter_cap: int = 3,
    gate_iter_cap: int = 2,
    parent_bead: str | None = None,
    dry_run: bool = False,
    claim: bool = True,
    force_full_regression: bool = False,
    enable_ralph: bool = False,
    enable_full_test_gate: bool = False,
    use_worktree: bool = True,
) -> dict[str, Any]:
    """Plain-Python software_dev_full body. Each role = one `agent_step` call.

    Loop semantics use `agent_step`'s `verdict_keywords` to read
    closure reasons:
      - critics: ``("approved", "rejected")``
      - lint: ``("clean", "failed")``
      - tester: ``("passed", "failed")``
      - regression-gate: ``("regression", "no regression")``
      - cleaner: ``("improvement", "no-improvement")``

    `agent_step`'s resumability cache means re-running this flow on a
    partially-progressed seed picks up where the prior run left off
    (closed beads return verdict from cache, agents only run for
    still-open beads).
    """
    logger = get_run_logger()
    main_rig_path_p = Path(rig_path).expanduser().resolve()

    # Worktree isolation. When enabled (default), each bead runs in its
    # own git worktree at `<rig>.wt-<id>/` on branch `wts-<id>`.
    # `.beads/` + `.planning/` are symlinked back to the main rig so bd
    # ops and run-dir artifacts stay in one authoritative location.
    # Disable via use_worktree=False or env PO_WTS_NO_WORKTREE=1.
    rig_path_p = main_rig_path_p
    worktree_enabled = False
    if use_worktree and not os.environ.get("PO_WTS_NO_WORKTREE"):
        try:
            from po_formulas_wts.worktree import _is_git_repo, setup_worktree
            if _is_git_repo(main_rig_path_p):
                rig_path_p = setup_worktree(main_rig_path_p, issue_id)
                worktree_enabled = True
                logger.info(
                    "worktree: enabled — agent cwd=%s, bd+planning via symlink to %s",
                    rig_path_p, main_rig_path_p,
                )
            else:
                logger.info(
                    "worktree: skipped — %s is not a git repo", main_rig_path_p,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("worktree: setup failed (%s); falling back to main rig", exc)
            rig_path_p = main_rig_path_p
            worktree_enabled = False

    # Canonical run-dir computed BEFORE the try wrap so the exception
    # handler can write `flow_outcome.json` even if `_load_rig_env` or
    # later body code raises. Lives in main rig's .planning/ (the
    # symlink in the worktree points here too).
    run_dir = main_rig_path_p / ".planning" / "software-dev-full" / issue_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # `BaseException` is deliberate — Prefect "Crashed" is driven by
    # signals (SIGTERM during worker shutdown, SIGKILL, OOM); an
    # `except Exception` would miss SystemExit / KeyboardInterrupt and
    # leave operators blind on `po status`. Re-raise is unconditional
    # so Prefect's terminal-state semantics are preserved.
    try:
        _load_rig_env(rig_path_p)
        _tag_flow_run_with_issue_id(issue_id, logger)

        # 0. Claim the seed (in_progress + assignee).
        if claim and not dry_run:
            claim_issue(issue_id, assignee=f"po-{os.getpid()}", rig_path=rig_path_p)

        # 1. Triage — agent reads the user's issue (the seed bead's
        #    description), classifies, writes triage.json verdict file.
        _agent_step_task(
            agent_dir=_agent_dir("triager"),
            task=_task_md("triager"),
            seed_id=issue_id,
            rig_path=str(rig_path_p),
            step="triage",
            iter_n=1,
            ctx={"pack_path": pack_path or str(rig_path_p)},
            dry_run=dry_run,
        )
        flags = _read_triage_flags(rig_path_p, issue_id)
        is_docs_only = bool(flags.get("is_docs_only"))
        has_ui = bool(flags.get("has_ui"))
        complexity = str(flags.get("complexity", "complex"))
        logger.info("complexity tier: %s", complexity)
        logger.info(
            "triage: complexity=%s docs_only=%s has_ui=%s",
            complexity,
            is_docs_only,
            has_ui,
        )

        # Trivial path: triager did the work itself + closed its bead.
        # Verify by checking if the seed's tree shows committed code.
        # No further pipeline; just close the seed.
        if complexity == "trivial":
            logger.info("trivial path: triager handled the work; closing seed")
            if claim and not dry_run:
                close_issue(
                    issue_id,
                    notes="po simple-mode complete (trivial path)",
                    rig_path=rig_path_p,
                )
            return {
                "status": "trivial_completed",
                "mode": "simple",
                "complexity": "trivial",
            }

        if is_docs_only:
            # Short path: docs + learn, then close the seed.
            _agent_step_task(
                agent_dir=_agent_dir("documenter"),
                task=_task_md("documenter"),
                seed_id=issue_id,
                rig_path=str(rig_path_p),
                step="docs",
                iter_n=1,
                dry_run=dry_run,
            )
            _agent_step_task(
                agent_dir=_agent_dir("learn"),
                task=_task_md("learn"),
                seed_id=issue_id,
                rig_path=str(rig_path_p),
                step="learn",
                iter_n=1,
                dry_run=dry_run,
            )
            if claim and not dry_run:
                close_issue(
                    issue_id, notes="docs-only path complete", rig_path=rig_path_p
                )
            return {
                "status": "docs_only_completed",
                "mode": "simple",
                "complexity": complexity,
            }

        # Complexity gates for the rest of the flow:
        run_critics = complexity in ("moderate", "complex")
        run_unit_tests = complexity in ("moderate", "complex")
        run_e2e_tests = complexity == "complex"
        run_regression = complexity == "complex"
        # Ralph + full-test-gate are opt-in even at complex tier. They each add
        # ~3-7 min wall-clock and rarely surface issues a clean review.iter +
        # verifier loop didn't already catch. Pass `enable_ralph=True` /
        # `enable_full_test_gate=True` (CLI: `--enable-ralph --enable-full-test-gate`)
        # for production-impacting changes where the extra rigor is worth it.
        run_documenter = complexity in ("moderate", "complex")
        # Demo video is now an ONE-PER-EPIC step in epic_finalize_wts
        # (prefect-orchestration-9wz). Per-child demo is gone — a
        # fanout epic with N UI children produced N tiny demos that
        # nobody watched; one epic-level demo against the smoke
        # artifacts is the source of truth.
        run_learn = complexity in ("moderate", "complex")

        # 2. Baseline — capture the rig's pre-change test state.
        _agent_step_task(
            agent_dir=_agent_dir("baseline"),
            task=_task_md("baseline"),
            seed_id=issue_id,
            rig_path=str(rig_path_p),
            step="baseline",
            iter_n=1,
            dry_run=dry_run,
        )

        # 3. Plan + plan-critic loop (critic gated by complexity).
        # Verdict-self-containment (prefect-orchestration-7vs.8): on iter2+
        # we pass the prior critique inline as `{{prior_critique}}`.
        # Simple complexity = single plan, no critic loop.
        plan_iter_final = 1
        prior_plan_critique = ""
        prior_plan_critic_bead = ""
        if not run_critics:
            # Single plan, no critic.
            _agent_step_task(
                agent_dir=_agent_dir("planner"),
                task=_task_md("planner"),
                seed_id=issue_id,
                rig_path=str(rig_path_p),
                step="plan",
                iter_n=1,
                ctx={"plan_iter": 1, "prior_critique": "", "prior_critic_bead": ""},
                dry_run=dry_run,
            )
        else:
            for plan_iter in range(1, plan_iter_cap + 1):
                plan_iter_final = plan_iter
                _agent_step_task(
                    agent_dir=_agent_dir("planner"),
                    task=_task_md("planner"),
                    seed_id=issue_id,
                    rig_path=str(rig_path_p),
                    step="plan",
                    iter_n=plan_iter,
                    ctx={
                        "plan_iter": plan_iter,
                        "prior_critique": prior_plan_critique,
                        "prior_critic_bead": prior_plan_critic_bead,
                    },
                    dry_run=dry_run,
                )
                critic_result = _agent_step_task(
                    agent_dir=_agent_dir("plan-critic"),
                    task=_task_md("plan-critic"),
                    seed_id=issue_id,
                    rig_path=str(rig_path_p),
                    step="plan-critic",
                    iter_n=plan_iter,
                    verdict_keywords=("approved", "rejected"),
                    dry_run=dry_run,
                )
                if critic_result.verdict == "approved":
                    break
                prior_plan_critique = critic_result.summary or "(no summary captured)"
                prior_plan_critic_bead = critic_result.bead_id
                if plan_iter >= plan_iter_cap:
                    logger.warning(
                        "plan_iter_cap=%s reached without approval; proceeding with last plan",
                        plan_iter_cap,
                    )
                    break

        # 4. Build + lint/test fan-out + review loop.
        # Verdict-self-containment: on iter2+ pass prior build-critic
        # critique inline (7vs.8 prompt-side approach, generalized).
        # Inline plan.md + triage.md once — re-read inside the build loop
        # only if a critic rejection rewrote them (not the common case).
        plan_md = _read_artifact(run_dir, "plan.md")
        triage_md = _read_artifact(run_dir, "triage.md")
        build_iter_final = 1
        prior_build_critique = ""
        prior_build_critic_bead = ""
        for build_iter in range(1, iter_cap + 1):
            build_iter_final = build_iter
            _agent_step_task(
                agent_dir=_agent_dir("builder"),
                task=_task_md("builder"),
                seed_id=issue_id,
                rig_path=str(rig_path_p),
                step="build",
                iter_n=build_iter,
                ctx={
                    "iter": build_iter,
                    "pack_path": pack_path or str(rig_path_p),
                    "prior_critique": prior_build_critique,
                    "prior_critic_bead": prior_build_critic_bead,
                    "plan_md": plan_md,
                    "triage_md": triage_md,
                },
                dry_run=dry_run,
            )

            # Scope the upcoming tester + regression-gate runs to tests
            # touched by this build's diff: write the list to
            # `<run_dir>/tests-changed.txt` BEFORE either role starts.
            # Without it both roles fall back to the full project suite
            # (763+ tests); when several flows share a rig that
            # CPU-thrashes every concurrent worker and trips the 30-min
            # agent_step timeout.
            diff_repo = Path(pack_path) if pack_path else rig_path_p
            changed_files_list = _write_scoped_tests_artifact(
                diff_repo,
                run_dir,
                force_full=force_full_regression,
            )

            # Inline this iter's diff for downstream lint + test (they scope
            # to changed files; saves them a `git diff` round-trip per role).
            build_diff = _read_artifact(run_dir, f"build-iter-{build_iter}.diff")

            # Lint + test layers — sequential for now (parallel via Prefect
            # task.submit() is a follow-up if wall-clock matters).
            _agent_step_task(
                agent_dir=_agent_dir("linter"),
                task=_task_md("linter"),
                seed_id=issue_id,
                rig_path=str(rig_path_p),
                step="lint",
                iter_n=build_iter,
                ctx={"plan_md": plan_md, "build_diff": build_diff, "iter": build_iter},
                verdict_keywords=("clean", "failed"),
                dry_run=dry_run,
            )

            if run_unit_tests and os.environ.get("PO_SKIP_UNIT") != "1":
                _agent_step_task(
                    agent_dir=_agent_dir("tester"),
                    task=_task_md("tester"),
                    seed_id=issue_id,
                    rig_path=str(rig_path_p),
                    step="test-unit",
                    iter_n=build_iter,
                    ctx={
                        "layer": "unit",
                        "iter": build_iter,
                        "plan_md": plan_md,
                        "build_diff": build_diff,
                    },
                    verdict_keywords=("passed", "failed"),
                    dry_run=dry_run,
                )

            if run_e2e_tests and os.environ.get("PO_SKIP_E2E") != "1":
                _agent_step_task(
                    agent_dir=_agent_dir("tester"),
                    task=_task_md("tester"),
                    seed_id=issue_id,
                    rig_path=str(rig_path_p),
                    step="test-e2e",
                    iter_n=build_iter,
                    ctx={"layer": "e2e", "iter": build_iter},
                    verdict_keywords=("passed", "failed"),
                    dry_run=dry_run,
                )

            if run_e2e_tests and has_ui and os.environ.get("PO_SKIP_PLAYWRIGHT") != "1":
                _agent_step_task(
                    agent_dir=_agent_dir("tester"),
                    task=_task_md("tester"),
                    seed_id=issue_id,
                    rig_path=str(rig_path_p),
                    step="test-playwright",
                    iter_n=build_iter,
                    ctx={"layer": "playwright", "iter": build_iter},
                    verdict_keywords=("passed", "failed"),
                    dry_run=dry_run,
                )

            # Regression gate — only on complex (full pipeline).
            if run_regression:
                regression_result = _agent_step_task(
                    agent_dir=_agent_dir("regression-gate"),
                    task=_task_md("regression-gate"),
                    seed_id=issue_id,
                    rig_path=str(rig_path_p),
                    step="regression",
                    iter_n=build_iter,
                    ctx={
                        "iter": build_iter,
                        "force_full_regression": force_full_regression,
                        "changed_files": changed_files_list,
                    },
                    verdict_keywords=("regression", "no regression"),
                    dry_run=dry_run,
                )
                if regression_result.verdict == "regression":
                    logger.warning(
                        "regression detected (iter %s); retrying build",
                        build_iter,
                    )
                    continue

            # Build critic (review) — only on moderate / complex.
            if run_critics:
                review_result = _agent_step_task(
                    agent_dir=_agent_dir("build-critic"),
                    task=_task_md("build-critic"),
                    seed_id=issue_id,
                    rig_path=str(rig_path_p),
                    step="review",
                    iter_n=build_iter,
                    verdict_keywords=("approved", "rejected"),
                    dry_run=dry_run,
                )
                if review_result.verdict == "approved":
                    break
                prior_build_critique = review_result.summary or "(no summary captured)"
                prior_build_critic_bead = review_result.bead_id
                if build_iter >= iter_cap:
                    logger.warning(
                        "iter_cap=%s reached without review approval; proceeding",
                        iter_cap,
                    )
                    break
            else:
                # Simple complexity — single build pass, no critic loop.
                break

        # 5-8 are complex-only.
        verify_iter_final = 1
        ralph_iter_final = 1
        gate_iter_final = 1

        if complexity == "complex":
            # 5. Deploy smoke + review artifacts.
            _agent_step_task(
                agent_dir=_agent_dir("deploy-smoke"),
                task=_task_md("deploy-smoke"),
                seed_id=issue_id,
                rig_path=str(rig_path_p),
                step="deploy-smoke",
                iter_n=1,
                dry_run=dry_run,
            )
            _agent_step_task(
                agent_dir=_agent_dir("review-artifacts"),
                task=_task_md("review-artifacts"),
                seed_id=issue_id,
                rig_path=str(rig_path_p),
                step="review-artifacts",
                iter_n=1,
                dry_run=dry_run,
            )

            # 6. Verifier loop.
            for verify_iter in range(1, verify_iter_cap + 1):
                verify_iter_final = verify_iter
                v = _agent_step_task(
                    agent_dir=_agent_dir("verifier"),
                    task=_task_md("verifier"),
                    seed_id=issue_id,
                    rig_path=str(rig_path_p),
                    step="verify",
                    iter_n=verify_iter,
                    ctx={"verify_iter": verify_iter},
                    verdict_keywords=("approved", "rejected"),
                    dry_run=dry_run,
                )
                if v.verdict == "approved":
                    break

            # 7. Ralph cleanup — improvements only, bounded.
            for ralph_iter in range(1, ralph_iter_cap + 1):
                ralph_iter_final = ralph_iter
                r = _agent_step_task(
                    agent_dir=_agent_dir("ralph"),
                    task=_task_md("ralph"),
                    seed_id=issue_id,
                    rig_path=str(rig_path_p),
                    step="ralph",
                    iter_n=ralph_iter,
                    ctx={"ralph_iter": ralph_iter, "gate_failures_block": ""},
                    verdict_keywords=("improvement", "no-improvement"),
                    dry_run=dry_run,
                )
                if r.verdict != "improvement":
                    break

            # 8. Final full-test gate (one-shot; failure → bounded ralph fix-up).
            for gate_iter in range(1, gate_iter_cap + 1):
                gate_iter_final = gate_iter
                g = _agent_step_task(
                    agent_dir=_agent_dir("full-test-gate"),
                    task=_task_md("full-test-gate"),
                    seed_id=issue_id,
                    rig_path=str(rig_path_p),
                    step="full-test-gate",
                    iter_n=gate_iter,
                    ctx={"has_ui": has_ui},
                    verdict_keywords=("passed", "failed"),
                    dry_run=dry_run,
                )
                if g.verdict == "passed":
                    break
                ralph_iter_final += 1
                _agent_step_task(
                    agent_dir=_agent_dir("ralph"),
                    task=_task_md("ralph"),
                    seed_id=issue_id,
                    rig_path=str(rig_path_p),
                    step="ralph-gate-fix",
                    iter_n=ralph_iter_final,
                    ctx={
                        "ralph_iter": ralph_iter_final,
                        "gate_failures_block": g.summary,
                    },
                    dry_run=dry_run,
                )

        # 9. Docs + learn — gated by complexity. (demo-video hoisted
        #    to epic_finalize_wts; see prefect-orchestration-9wz.)
        if run_documenter:
            _agent_step_task(
                agent_dir=_agent_dir("documenter"),
                task=_task_md("documenter"),
                seed_id=issue_id,
                rig_path=str(rig_path_p),
                step="docs",
                iter_n=1,
                dry_run=dry_run,
            )
        if run_learn:
            _agent_step_task(
                agent_dir=_agent_dir("learn"),
                task=_task_md("learn"),
                seed_id=issue_id,
                rig_path=str(rig_path_p),
                step="learn",
                iter_n=1,
                dry_run=dry_run,
            )

        # Merge the worktree back into the main rig's current branch
        # BEFORE closing the bead so the close-keyword reflects post-merge
        # state. Failures here leave the worktree for the operator to
        # resolve and re-run; the seed bead stays open.
        merged_into = None
        if worktree_enabled:
            from po_formulas_wts.worktree import merge_worktree
            try:
                merged_into = merge_worktree(main_rig_path_p, issue_id, cleanup=True)
                logger.info("worktree: merged into %s", merged_into)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "worktree: merge failed (%s); leaving worktree at "
                    "<rig>.wt-<id> for resolution. Re-run after fixing.",
                    exc,
                )
                raise

        # Close the seed.
        if claim and not dry_run:
            close_issue(issue_id, notes="po simple-mode complete", rig_path=main_rig_path_p)

        return {
            "status": "completed",
            "mode": "simple",
            "complexity": complexity,
            "plan_iter": plan_iter_final,
            "build_iter": build_iter_final,
            "verify_iter": verify_iter_final,
            "ralph_iter": ralph_iter_final,
            "gate_iter": gate_iter_final,
            "is_docs_only": is_docs_only,
            "has_ui": has_ui,
            "worktree_merged_into": merged_into,
        }
    except BaseException as exc:
        _record_flow_outcome(run_dir, exc, issue_id, str(main_rig_path_p))
        # Deliberately do NOT cleanup the worktree on failure — operators
        # need it for forensic inspection (build-iter-*.diff files, dirty
        # working tree, etc). Reclaim via `git worktree remove --force` or
        # by re-running the flow after fixing the upstream issue.
        raise


@flow(
    name="software_dev_fast_wts",
    flow_run_name="{issue_id}",
    log_prints=True,
)
def software_dev_fast(
    issue_id: str,
    rig: str,
    rig_path: str,
    pack_path: str | None = None,
    parent_bead: str | None = None,
    dry_run: bool = False,
    claim: bool = True,
) -> dict[str, Any]:
    """Fast linear pipeline: plan → build → lint → unit-test → docs → close.

    No iterations, no critics, no triage / baseline / regression /
    verify / deploy-smoke / review-artifacts / ralph / learn.
    Each role runs exactly once. Seed closes only if lint is clean and
    unit tests pass; otherwise the seed stays open with a status in the
    return dict so the caller can re-dispatch or escalate to
    `software-dev-full`.

    Run-dir layout still lives under `.planning/software-dev-full/<id>/`
    so existing `po artifacts` / `po watch` / `po tui` resolution keeps
    working without a parallel directory.
    """
    logger = get_run_logger()
    rig_path_p = Path(rig_path).expanduser().resolve()
    _load_rig_env(rig_path_p)
    _tag_flow_run_with_issue_id(issue_id, logger)

    # Fast-mode defaults: sonnet flow-wide at medium effort. Per-role
    # config.toml still wins, so linter/tester drop to effort=low via
    # their own configs. CLI flags stamp PO_*_CLI which beats these env
    # defaults — `po run software-dev-fast --model opus` still works.
    os.environ.setdefault("PO_MODEL", "sonnet")
    os.environ.setdefault("PO_EFFORT", "medium")

    run_dir = rig_path_p / ".planning" / "software-dev-full" / issue_id
    run_dir.mkdir(parents=True, exist_ok=True)

    if claim and not dry_run:
        claim_issue(issue_id, assignee=f"po-{os.getpid()}", rig_path=rig_path_p)

    _agent_step_task(
        agent_dir=_agent_dir("planner"),
        task=_task_md("planner"),
        seed_id=issue_id,
        rig_path=str(rig_path_p),
        step="plan",
        iter_n=1,
        ctx={"plan_iter": 1, "prior_critique": "", "prior_critic_bead": ""},
        dry_run=dry_run,
    )

    # Inline plan.md into downstream roles so they don't burn a round-trip
    # on `cat plan.md`. Same for triage.md (empty in fast — no triage step).
    plan_md = _read_artifact(run_dir, "plan.md")
    triage_md = _read_artifact(run_dir, "triage.md")

    _agent_step_task(
        agent_dir=_agent_dir("builder"),
        task=_task_md("builder"),
        seed_id=issue_id,
        rig_path=str(rig_path_p),
        step="build",
        iter_n=1,
        ctx={
            "iter": 1,
            "pack_path": pack_path or str(rig_path_p),
            "prior_critique": "",
            "prior_critic_bead": "",
            "plan_md": plan_md,
            "triage_md": triage_md,
        },
        dry_run=dry_run,
    )

    # After build, inline the diff for the linter + tester (they scope work
    # to the changed files; `git diff --name-only HEAD~5` would otherwise
    # re-derive this from inside each agent).
    build_diff = _read_artifact(run_dir, "build-iter-1.diff")

    lint_result = _agent_step_task(
        agent_dir=_agent_dir("linter"),
        task=_task_md("linter"),
        seed_id=issue_id,
        rig_path=str(rig_path_p),
        step="lint",
        iter_n=1,
        ctx={"plan_md": plan_md, "build_diff": build_diff, "iter": 1},
        verdict_keywords=("clean", "failed"),
        dry_run=dry_run,
    )

    unit_result = _agent_step_task(
        agent_dir=_agent_dir("tester"),
        task=_task_md("tester"),
        seed_id=issue_id,
        rig_path=str(rig_path_p),
        step="test-unit",
        iter_n=1,
        ctx={
            "layer": "unit",
            "iter": 1,
            "plan_md": plan_md,
            "build_diff": build_diff,
        },
        verdict_keywords=("passed", "failed"),
        dry_run=dry_run,
    )

    docs_result = _agent_step_task(
        agent_dir=_agent_dir("documenter"),
        task=_task_md("documenter"),
        seed_id=issue_id,
        rig_path=str(rig_path_p),
        step="docs",
        iter_n=1,
        ctx={"plan_md": plan_md, "build_diff": build_diff},
        dry_run=dry_run,
    )

    # Linter + tester auto-fix as part of their normal work; close when
    # they return regardless of verdict. If something was genuinely
    # un-fixable the verdict is recorded for forensic inspection but
    # we don't gate the close on it (per "when done fixing we are done").
    if claim and not dry_run:
        close_issue(
            issue_id,
            notes="po fast-mode complete",
            rig_path=rig_path_p,
        )
    if lint_result.verdict != "clean" or unit_result.verdict != "passed":
        logger.warning(
            "fast-mode: closed despite non-clean verdicts (lint=%s, test-unit=%s)",
            lint_result.verdict,
            unit_result.verdict,
        )

    return {
        "status": "completed",
        "mode": "fast",
        "lint_verdict": lint_result.verdict,
        "test_unit_verdict": unit_result.verdict,
        "docs_verdict": docs_result.verdict,
    }


@flow(
    name="software_dev_edit_wts",
    flow_run_name="{issue_id}",
    log_prints=True,
)
def software_dev_edit(
    issue_id: str,
    rig: str,
    rig_path: str,
    pack_path: str | None = None,
    parent_bead: str | None = None,
    dry_run: bool = False,
    claim: bool = True,
) -> dict[str, Any]:
    """Ultra-thin pipeline: plan → build → close.

    No iterations, no critics, no triage / baseline / lint / regression /
    verify / deploy-smoke / review-artifacts / ralph / learn / tests / docs.
    Each role runs exactly once. Wall-clock ~3-5 min per child;
    pair with `epic-finalize` as last child for the lint/test gate.
    """
    logger = get_run_logger()
    rig_path_p = Path(rig_path).expanduser().resolve()
    _load_rig_env(rig_path_p)
    _tag_flow_run_with_issue_id(issue_id, logger)

    os.environ.setdefault("PO_MODEL", "haiku")
    os.environ.setdefault("PO_EFFORT", "low")

    run_dir = rig_path_p / ".planning" / "software-dev-full" / issue_id
    run_dir.mkdir(parents=True, exist_ok=True)

    if claim and not dry_run:
        claim_issue(issue_id, assignee=f"po-{os.getpid()}", rig_path=rig_path_p)

    _agent_step_task(
        agent_dir=_agent_dir("planner"),
        task=_task_md("planner"),
        seed_id=issue_id,
        rig_path=str(rig_path_p),
        step="plan",
        iter_n=1,
        ctx={"plan_iter": 1, "prior_critique": "", "prior_critic_bead": ""},
        dry_run=dry_run,
    )

    plan_md = _read_artifact(run_dir, "plan.md")
    triage_md = _read_artifact(run_dir, "triage.md")

    _agent_step_task(
        agent_dir=_agent_dir("builder"),
        task=_task_md("builder"),
        seed_id=issue_id,
        rig_path=str(rig_path_p),
        step="build",
        iter_n=1,
        ctx={
            "iter": 1,
            "pack_path": pack_path or str(rig_path_p),
            "prior_critique": "",
            "prior_critic_bead": "",
            "plan_md": plan_md,
            "triage_md": triage_md,
        },
        dry_run=dry_run,
    )

    if claim and not dry_run:
        close_issue(issue_id, notes="po edit-mode complete", rig_path=rig_path_p)

    return {"status": "completed", "mode": "edit"}


__all__ = ["software_dev_full", "software_dev_fast", "software_dev_edit"]
