"""Prefect deployments for the software-dev formula pack.

Discovered by core's `po deploy` via the `po.deployments` entry-point group.
Add one entry to `register()` per deployment. Keep it pure — construct
objects, no I/O.
"""

from __future__ import annotations

from prefect.deployments.runner import EntrypointType
from prefect.schedules import Cron

from po_formulas.code_health import code_health_review
from po_formulas.epic import epic_run
from po_formulas.software_dev import software_dev_edit, software_dev_fast, software_dev_full


def register() -> list:
    """Return the RunnerDeployments this pack ships.

    - `epic-sr-8yu-nightly`: runs the `epic` fan-out for epic-sr-8yu every
      night at 09:00 America/New_York.
    - `epic-manual`, `software-dev-{full,fast,edit}-manual`: no-schedule
      deployments for `po run <formula> --at <when>` time-batching.
    """
    module_path = {"entrypoint_type": EntrypointType.MODULE_PATH}
    return [
        epic_run.to_deployment(
            name="epic-sr-8yu-nightly",
            schedule=Cron("0 9 * * *", timezone="America/New_York"),
            parameters={"epic_id": "sr-8yu"},
            **module_path,
        ),
        epic_run.to_deployment(name="epic-manual", **module_path),
        software_dev_full.to_deployment(
            name="software-dev-full-manual",
            **module_path,
        ),
        software_dev_fast.to_deployment(
            name="software-dev-fast-manual",
            **module_path,
        ),
        software_dev_edit.to_deployment(
            name="software-dev-edit-manual",
            **module_path,
        ),
        # Code-health review: weekly cron, but PAUSED on apply so users opt
        # in by unpausing in the Prefect UI (or `prefect deployment set-schedule
        # --active code_health_review/code-health-review-weekly`).
        # `parameters` is intentionally empty — operators set rig / rig_path
        # via UI / `prefect deployment run --param` to keep this rig-agnostic.
        code_health_review.to_deployment(
            name="code-health-review-weekly",
            schedule=Cron("0 8 * * 1", timezone="America/New_York"),
            paused=True,
            **module_path,
        ),
        code_health_review.to_deployment(
            name="code-health-review-manual",
            **module_path,
        ),
    ]
