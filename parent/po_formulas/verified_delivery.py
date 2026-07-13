"""Versioned, atomic delivery evidence for PO software-development runs."""

from __future__ import annotations

import copy
import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

ARTIFACT_NAME = "verified-delivery.json"
SCHEMA_NAME = "po.verified-delivery"
SCHEMA_VERSION = 1

_DEFAULTS: dict[str, Any] = {
    "schema": SCHEMA_NAME,
    "version": SCHEMA_VERSION,
    "revisions": {"base": None, "head": None, "integration": None},
    "pull_request": {"number": None, "url": None, "target": None},
    "acceptance_criteria": [],
    "changed_surfaces": [],
    "live_verification": {"plan": [], "results": []},
    "preview": {"url": None, "revision": None},
    "screenshots": [],
    "demo": {"path": None, "url": None},
    "deferrals": [],
    "terminal": {"state": "running", "reason": None},
    "provenance": {
        "formula": None,
        "backend": None,
        "provider": None,
        "account": None,
        "account_class": None,
        "model": None,
        "effort": None,
        "rig": None,
        "rig_path": None,
        "pack_path": None,
        "parent_epic": None,
        "flow_run_id": None,
        "dispatch_command": None,
    },
}

_LEGACY_PATHS = {
    "base_sha": ("revisions", "base"),
    "head_sha": ("revisions", "head"),
    "integration_sha": ("revisions", "integration"),
    "pr_number": ("pull_request", "number"),
    "pr_url": ("pull_request", "url"),
    "pr_target": ("pull_request", "target"),
    "preview_url": ("preview", "url"),
    "terminal_state": ("terminal", "state"),
    "terminal_reason": ("terminal", "reason"),
}
_LEGACY_PATHS.update({f"po.{key}": value for key, value in _LEGACY_PATHS.items()})


def artifact_path(run_dir: Path) -> Path:
    """Return the canonical delivery artifact path for *run_dir*."""
    return run_dir / ARTIFACT_NAME


def _deep_merge(base: dict[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _set_path(data: dict[str, Any], path: tuple[str, str], value: Any) -> None:
    parent, child = path
    section = data.setdefault(parent, {})
    if not isinstance(section, dict):
        section = {}
        data[parent] = section
    section.setdefault(child, value)


def normalize(payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Normalize v1 or legacy metadata-shaped input to the complete v1 shape.

    Unknown keys are retained so a newer producer can safely round-trip through
    an older consumer. Legacy flat keys only fill fields not already supplied in
    the structured representation.
    """
    incoming = copy.deepcopy(dict(payload or {}))
    metadata = incoming.pop("metadata", None)
    if isinstance(metadata, Mapping):
        embedded = metadata.get("po.verified_delivery")
        if isinstance(embedded, Mapping):
            incoming = _deep_merge(dict(embedded), incoming)
        for key in _LEGACY_PATHS:
            if key in metadata:
                incoming.setdefault(key, metadata[key])
    for legacy_key, destination in _LEGACY_PATHS.items():
        if legacy_key in incoming:
            _set_path(incoming, destination, incoming.pop(legacy_key))
    normalized = _deep_merge(_DEFAULTS, incoming)
    if normalized.get("schema") != SCHEMA_NAME:
        raise ValueError("unsupported verified-delivery schema")
    if normalized.get("version") != SCHEMA_VERSION:
        raise ValueError("unsupported verified-delivery version")
    return normalized


def read(run_dir: Path) -> dict[str, Any]:
    """Read and normalize the contract, returning defaults when absent."""
    path = artifact_path(run_dir)
    if not path.exists():
        return normalize()
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid verified-delivery JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError("verified-delivery artifact must contain a JSON object")
    return normalize(payload)


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def update(run_dir: Path, patch: Mapping[str, Any]) -> dict[str, Any]:
    """Deep-merge *patch* into the contract and atomically persist it.

    Dictionaries merge recursively; lists and scalar values replace their prior
    value. This gives each delivery phase ownership of its own fields without
    losing evidence produced by another phase.
    """
    contract = normalize(_deep_merge(read(run_dir), patch))
    _atomic_write(artifact_path(run_dir), contract)
    return contract


def initialize(
    run_dir: Path, *, provenance: Mapping[str, Any], base: str
) -> dict[str, Any]:
    """Create or enrich a running contract without discarding existing evidence."""
    return update(
        run_dir,
        {
            "revisions": {"base": base},
            "terminal": {"state": "running", "reason": None},
            "provenance": provenance,
        },
    )
