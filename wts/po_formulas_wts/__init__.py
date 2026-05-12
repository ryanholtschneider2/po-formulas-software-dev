"""po-formulas-software-dev-wts — worktree-aware variant of
po-formulas-software-dev. Bootstrap copy at nanocorps-gi3; the actual
worktree-setup step lands in nanocorps-tbw.

Exposes via `po.formulas` entry points:
  * `software-dev-full-wts` — full pipeline (currently identical to non-wts)
  * `software-dev-fast-wts` — fast pipeline (currently identical to non-wts)

Other formulas (`epic`, `graph`, `minimal-task`, `epic-finalize`) are
NOT re-registered by this pack; consumers reuse the originals from
`po-formulas-software-dev`.
"""

from po_formulas_wts.software_dev import software_dev_fast, software_dev_full

__all__ = ["software_dev_full", "software_dev_fast"]
