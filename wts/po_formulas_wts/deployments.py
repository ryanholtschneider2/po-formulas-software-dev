"""Prefect deployments for the software-dev formula pack.

Discovered by core's `po deploy` via the `po.deployments` entry-point group.
Add one entry to `register()` per deployment. Keep it pure — construct
objects, no I/O.
"""

from __future__ import annotations

from prefect.schedules import Cron

from po_formulas_wts.epic import epic_run
from po_formulas_wts.epic_finalize import epic_finalize
from po_formulas_wts.software_dev import (
    software_dev_edit,
    software_dev_fast,
    software_dev_full,
)


def register() -> list:
    """Return the RunnerDeployments this pack ships.

    - `epic-sr-8yu-nightly`: cron fan-out for epic-sr-8yu nightly 09:00 ET.
    - `software-dev-full-manual` / `software-dev-fast-manual`: no-schedule
      deployments used by `po run --at` / `po retry --at` / `po resume --at`
      to schedule a flow-run for a future time. Convention: `<formula>-manual`.
    """
    return [
        epic_run.to_deployment(
            name="epic-sr-8yu-nightly",
            schedule=Cron("0 9 * * *", timezone="America/New_York"),
            parameters={"epic_id": "sr-8yu"},
            work_pool_name="po",
        ),
        software_dev_full.to_deployment(
            name="software-dev-full-manual",
            work_pool_name="po",
        ),
        software_dev_fast.to_deployment(
            name="software-dev-fast-manual",
            work_pool_name="po",
        ),
        software_dev_edit.to_deployment(
            name="software-dev-edit-manual",
            work_pool_name="po",
        ),
        epic_finalize.to_deployment(
            name="epic-finalize-manual",
            work_pool_name="po",
        ),
    ]
