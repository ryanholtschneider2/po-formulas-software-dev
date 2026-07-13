"""Structured sizing judgment for ``software-dev-agentic``.

The agent decides what the work means. This module only validates, persists,
and applies the operator's mechanical iteration-budget boundary.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SIZING_FILE = "sizing.json"
MIN_ITERATIONS = 1
MAX_ITERATIONS = 4
_DECISIONS = {"proceed", "decompose"}
_SIZES = {"trivial", "small", "medium", "large", "oversized"}
_RISKS = {"low", "medium", "high"}
_SURFACE_TYPES = {
    "api",
    "cli",
    "code",
    "data",
    "docs",
    "infrastructure",
    "service",
    "ui",
    "workflow",
}
_LIVE_SURFACE_TYPES = _SURFACE_TYPES - {"code", "docs"}
_DEPLOY_SURFACE_TYPES = {"api", "data", "infrastructure", "service", "ui"}


class SizingContractError(ValueError):
    """The sizing agent did not produce a structurally valid judgment."""


class DecompositionRequiredError(RuntimeError):
    """The model judged the seed too broad for one agentic worker loop."""


@dataclass(frozen=True)
class SizingDecision:
    decision: str
    size: str
    risk: str
    surfaces: tuple[str, ...]
    iteration_budget: int
    rationale: str
    decomposition_reason: str
    surface_types: tuple[str, ...] = ("code",)

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "size": self.size,
            "risk": self.risk,
            "surfaces": list(self.surfaces),
            "iteration_budget": self.iteration_budget,
            "rationale": self.rationale,
            "decomposition_reason": self.decomposition_reason,
            "surface_types": list(self.surface_types),
        }


@dataclass(frozen=True)
class DeliveryPlan:
    """Mechanically derived proof phases for a model-classified delivery."""

    review_artifacts: bool
    live_verifier: bool
    deploy_smoke: bool
    demo: bool

    def as_dict(self) -> dict[str, bool]:
        return {
            "review_artifacts": self.review_artifacts,
            "live_verifier": self.live_verifier,
            "deploy_smoke": self.deploy_smoke,
            "demo": self.demo,
        }

    def steps(self) -> list[str]:
        return [name for name, enabled in self.as_dict().items() if enabled]


def read_sizing(run_dir: Path) -> SizingDecision:
    """Read and structurally validate the model-authored sizing artifact."""
    path = run_dir / SIZING_FILE
    try:
        raw = json.loads(path.read_text())
    except OSError as exc:
        raise SizingContractError(f"sizing agent wrote no {path}") from exc
    except json.JSONDecodeError as exc:
        raise SizingContractError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise SizingContractError(f"{path} must contain a JSON object")

    decision = _enum(raw, "decision", _DECISIONS)
    size = _enum(raw, "size", _SIZES)
    risk = _enum(raw, "risk", _RISKS)
    iteration_budget = raw.get("iteration_budget")
    if isinstance(iteration_budget, bool) or not isinstance(iteration_budget, int):
        raise SizingContractError("iteration_budget must be an integer")
    if not MIN_ITERATIONS <= iteration_budget <= MAX_ITERATIONS:
        raise SizingContractError(
            f"iteration_budget must be between {MIN_ITERATIONS} and {MAX_ITERATIONS}"
        )

    surfaces_raw = raw.get("surfaces")
    if (
        not isinstance(surfaces_raw, list)
        or not surfaces_raw
        or not all(
            isinstance(surface, str) and surface.strip() for surface in surfaces_raw
        )
    ):
        raise SizingContractError("surfaces must be a list of non-empty strings")
    rationale = _text(raw, "rationale")
    decomposition_reason = raw.get("decomposition_reason", "")
    if not isinstance(decomposition_reason, str):
        raise SizingContractError("decomposition_reason must be a string")
    surface_types_raw = raw.get("surface_types", ["code"])
    if (
        not isinstance(surface_types_raw, list)
        or not surface_types_raw
        or not all(
            isinstance(value, str) and value in _SURFACE_TYPES
            for value in surface_types_raw
        )
    ):
        options = ", ".join(sorted(_SURFACE_TYPES))
        raise SizingContractError(
            f"surface_types must be a non-empty list containing only: {options}"
        )

    return SizingDecision(
        decision=decision,
        size=size,
        risk=risk,
        surfaces=tuple(surface.strip() for surface in surfaces_raw),
        iteration_budget=iteration_budget,
        rationale=rationale,
        decomposition_reason=decomposition_reason.strip(),
        surface_types=tuple(dict.fromkeys(surface_types_raw)),
    )


def apply_operator_cap(
    decision: SizingDecision, iter_cap: int | None
) -> SizingDecision:
    """Apply an optional operator ceiling to the model-selected budget."""
    if iter_cap is None:
        return decision
    if isinstance(iter_cap, bool) or not isinstance(iter_cap, int):
        raise ValueError("iter_cap must be an integer or None")
    if not MIN_ITERATIONS <= iter_cap <= MAX_ITERATIONS:
        raise ValueError(
            f"iter_cap must be between {MIN_ITERATIONS} and {MAX_ITERATIONS}"
        )
    payload = deepcopy(decision.as_dict())
    payload["iteration_budget"] = min(decision.iteration_budget, iter_cap)
    return SizingDecision(
        decision=payload["decision"],
        size=payload["size"],
        risk=payload["risk"],
        surfaces=tuple(payload["surfaces"]),
        iteration_budget=payload["iteration_budget"],
        rationale=payload["rationale"],
        decomposition_reason=payload["decomposition_reason"],
        surface_types=tuple(payload["surface_types"]),
    )


def delivery_plan(decision: SizingDecision, *, demo_enabled: bool) -> DeliveryPlan:
    """Translate model judgment into the phases required by operator policy.

    The model owns semantic classification through ``risk`` and
    ``surface_types``. This function only applies declared proof policy: live
    surfaces and medium/high-risk changes require live verification; deployable
    surfaces require a smoke exercise; UI always requires live artifacts and
    gets a demo when the rig enables demo capture.
    """
    kinds = set(decision.surface_types)
    needs_live = bool(kinds & _LIVE_SURFACE_TYPES) or decision.risk != "low"
    return DeliveryPlan(
        review_artifacts=needs_live,
        live_verifier=needs_live,
        deploy_smoke=bool(kinds & _DEPLOY_SURFACE_TYPES),
        demo="ui" in kinds and demo_enabled,
    )


def apply_proof_mode(plan: DeliveryPlan, mode: str) -> DeliveryPlan:
    """Apply the operator's proof-mode policy without reclassifying the work.

    ``adaptive`` preserves the model-classified plan. ``strict`` requires a
    reviewer package and live verification for every delivery; deploy smoke and
    demo remain tied to the classified surface and the existing demo knob.
    """
    if mode == "adaptive":
        return plan
    if mode != "strict":
        raise ValueError("proof mode must be 'adaptive' or 'strict'")
    return DeliveryPlan(
        review_artifacts=True,
        live_verifier=True,
        deploy_smoke=plan.deploy_smoke,
        demo=plan.demo,
    )


def decomposition_message(issue_id: str, decision: SizingDecision) -> str:
    reason = decision.decomposition_reason or decision.rationale
    return (
        f"software-dev-agentic refused {issue_id}: {reason}. "
        "Decompose it with `po run agentic-epic --epic-id "
        f"{issue_id} --rig <name> --rig-path <path>`."
    )


def _enum(raw: dict[str, Any], key: str, allowed: set[str]) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or value not in allowed:
        options = ", ".join(sorted(allowed))
        raise SizingContractError(f"{key} must be one of: {options}")
    return value


def _text(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SizingContractError(f"{key} must be a non-empty string")
    return value.strip()


__all__ = [
    "DecompositionRequiredError",
    "DeliveryPlan",
    "MAX_ITERATIONS",
    "MIN_ITERATIONS",
    "SIZING_FILE",
    "SizingContractError",
    "SizingDecision",
    "apply_operator_cap",
    "apply_proof_mode",
    "decomposition_message",
    "delivery_plan",
    "read_sizing",
]
