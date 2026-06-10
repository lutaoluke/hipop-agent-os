"""Tool manifest loader.

WS-162 keeps tool metadata in tools_registry.yaml as the single source of
truth. Runtime code projects only the fields it needs from this manifest.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - dependency is declared in requirements.txt
    yaml = None


_REGISTRY_YAML = Path(__file__).parent / "tools_registry.yaml"
_MANIFEST_CACHE: dict[str, Any] | None = None


def _load_manifest() -> dict[str, Any]:
    global _MANIFEST_CACHE
    if _MANIFEST_CACHE is not None:
        return _MANIFEST_CACHE
    if yaml is None:
        raise RuntimeError("PyYAML is required to load tools_registry.yaml")
    with open(_REGISTRY_YAML) as f:
        manifest = yaml.safe_load(f) or {}
    tools = manifest.get("tools")
    if not isinstance(tools, dict):
        raise ValueError("tools_registry.yaml must contain a top-level tools mapping")
    _MANIFEST_CACHE = manifest
    return manifest


def load_tool_registry() -> dict[str, dict[str, Any]]:
    """Return a deep copy of every tool's full manifest entry."""
    return deepcopy(_load_manifest()["tools"])


def get_tool_spec(tool_name: str) -> dict[str, Any]:
    """Return one tool's full manifest entry, or {} for unknown tools."""
    return deepcopy(_load_manifest()["tools"].get(tool_name) or {})


def load_tools_from_yaml() -> list[dict[str, Any]]:
    """Project manifest entries into Anthropic tool-use format."""
    projected = []
    for name, spec in _load_manifest()["tools"].items():
        projected.append({
            "name": name,
            "description": spec["description"],
            "input_schema": deepcopy(spec["input_schema"]),
        })
    return projected
