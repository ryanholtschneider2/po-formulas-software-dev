"""po-formulas-software-dev — actor-critic software development pipeline for po.

Exposes via `po.formulas` entry points:
  * `software-dev-full` — end-to-end plan/build/review/verify/ralph pipeline
  * `epic`              — fan out an epic's ready children into a DAG of sub-flows
  * `minimal-task`      — lightweight triage→plan→build→lint→close for fanout demos
"""

from po_formulas.epic import epic_run
from po_formulas.minimal_task import minimal_task
from po_formulas.software_dev import software_dev_full

__all__ = ["software_dev_full", "epic_run", "minimal_task"]
