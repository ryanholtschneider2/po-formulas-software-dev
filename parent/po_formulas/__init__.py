"""po-formulas-software-dev — actor-critic software development pipeline for po.

Exposes via `po.formulas` entry points:
  * `software-dev-full` — end-to-end plan/build/review/verify/ralph pipeline
  * `epic`              — fan out an epic's ready children into a DAG of sub-flows
  * `minimal-task`      — lightweight triage→plan→build→lint→close for fanout demos
  * `software-dev-agentic` — agent-owned plan→build→lint→test + machine gates + 1 reviewer
"""

from po_formulas.agentic import software_dev_agentic
from po_formulas.epic import epic_run
from po_formulas.minimal_task import minimal_task
from po_formulas.software_dev import software_dev_full

__all__ = ["software_dev_full", "epic_run", "minimal_task", "software_dev_agentic"]
