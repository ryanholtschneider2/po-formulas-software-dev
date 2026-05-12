"""Stub Prefect flow for the worktree-aware `pr-writer` formula.

The operator-facing assets live in `software-dev-pack-wts/`. This
module registers the formula under `po.formulas` in the generic
worktree software-dev family instead of under `nanocorp-agents`.
"""

from __future__ import annotations

from prefect import flow


@flow(name="pr_writer_wts")
def pr_writer(
    bead_id: str | None = None,
    epic_id: str | None = None,
    rig: str = "",
    rig_path: str = "",
    branch: str | None = None,
) -> dict[str, str]:
    """Truthful stub until the runtime implementation is ported here."""
    raise NotImplementedError(
        "pr-writer assets now live in software-dev-pack-wts, but the runtime "
        "flow implementation has not been ported into po-formulas-software-dev-wts yet."
    )
