"""Resolve named heartbeats from $HERMES_HOME/proactive-heartbeats/.

The root JSON holds shared TypeSafe, cooldown, delivery, and context defaults.
Each heartbeat lives in ``heartbeats/{name}.json`` and resolves to:

    {
      "delivery": {"schedule": "...", "target": "..."},
      "collectors": {"<id>": {"enabled": bool, ...}},
      "context": {...}
    }

The plugin interprets only ``collectors.<id>.enabled``. Every remaining
collector setting is opaque and passed to the matching user-owned module under
``collectors/{id}.py``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

try:
    from . import _bootstrap  # noqa: F401
except ImportError:  # flat plugin-dir / unittest load
    import _bootstrap  # noqa: F401

JsonObject = dict[str, Any]

PLUGIN_DIRNAME = "proactive-heartbeats"
ROOT_BASENAME = "proactive-heartbeats.json"
HEARTBEATS_DIRNAME = "heartbeats"
COLLECTORS_DIRNAME = "collectors"
CONFIG_DIR_KEY = "config_dir"
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")

ROOT_SETTING_DEFAULTS: JsonObject = {
    "typesafe_model": "jev-latest",
    "typesafe_threshold": 0.65,
    "default_cooldown_seconds": 14400,
}

DELIVERY_DEFAULTS: JsonObject = {
    "schedule": "every 15m",
    "target": "local",
}

_ROOT_SKELETON: JsonObject = {
    "typesafe_threshold": 0.65,
    "default_cooldown_seconds": 14400,
    "defaults": {
        "delivery": {
            "schedule": "every 15m",
            "target": "local",
        }
    },
    "context": {},
}

_HEARTBEAT_SKELETON: JsonObject = {
    "delivery": {
        "schedule": "every 15m",
        "target": "local",
    },
    "collectors": {},
    "context": {},
}


def validate_name(name: str) -> str:
    cleaned = str(name or "").strip()
    if not NAME_RE.fullmatch(cleaned):
        raise RuntimeError(f"invalid heartbeat name {name!r}: expected {NAME_RE.pattern}")
    return cleaned


def plugin_dir(home: Path, *, config_dir: str | None = None) -> Path:
    root = home.expanduser().resolve()
    relative = (config_dir or PLUGIN_DIRNAME).strip() or PLUGIN_DIRNAME
    requested = Path(relative)
    if requested.is_absolute() or relative.startswith("~"):
        raise RuntimeError("config_dir must be relative to HERMES_HOME")
    resolved = (root / requested).resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise RuntimeError("config_dir must resolve below HERMES_HOME")
    return resolved


def collectors_dir(home: Path, *, config_dir: str | None = None) -> Path:
    return plugin_dir(home, config_dir=config_dir) / COLLECTORS_DIRNAME


def root_path(home: Path, *, config_dir: str | None = None) -> Path:
    return plugin_dir(home, config_dir=config_dir) / ROOT_BASENAME


def heartbeat_path(home: Path, name: str, *, config_dir: str | None = None) -> Path:
    return (
        plugin_dir(home, config_dir=config_dir) / HEARTBEATS_DIRNAME / f"{validate_name(name)}.json"
    )


def job_name(name: str) -> str:
    return f"proactive-heartbeats-{validate_name(name)}"


def shim_basename(name: str) -> str:
    return f"proactive-heartbeats-{validate_name(name)}.sh"


def state_key(name: str) -> str:
    return f"heartbeat:{validate_name(name)}"


def read_json_object(path: Path) -> JsonObject:
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"plugin config is not valid JSON: {path}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, Mapping):
        raise RuntimeError(f"plugin config must be a JSON object: {path}")
    return dict(loaded)


def write_json_skeleton(path: Path, payload: Mapping[str, Any]) -> bool:
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(payload), indent=2) + "\n", encoding="utf-8")
    return True


def write_root_skeleton(path: Path) -> bool:
    return write_json_skeleton(path, _ROOT_SKELETON)


def write_heartbeat_skeleton(path: Path) -> bool:
    return write_json_skeleton(path, _HEARTBEAT_SKELETON)


def load_root(home: Path, *, config_dir: str | None = None) -> JsonObject:
    loaded = read_json_object(root_path(home, config_dir=config_dir))
    root: JsonObject = {}
    for key, default in ROOT_SETTING_DEFAULTS.items():
        value = loaded.get(key, default)
        root[key] = default if value is None else value
    defaults = loaded.get("defaults")
    root["defaults"] = dict(defaults) if isinstance(defaults, Mapping) else {}
    context = loaded.get("context")
    root["context"] = dict(context) if isinstance(context, Mapping) else {}
    return root


def heartbeat_names(home: Path, *, config_dir: str | None = None) -> tuple[str, ...]:
    """Heartbeat ids are the stems of heartbeats/*.json."""
    directory = plugin_dir(home, config_dir=config_dir) / HEARTBEATS_DIRNAME
    if not directory.is_dir():
        return ()
    names: list[str] = []
    for path in sorted(directory.glob("*.json")):
        stem = path.stem
        if NAME_RE.fullmatch(stem):
            names.append(stem)
    return tuple(names)


def _merge_delivery(root: Mapping[str, Any], file_delivery: Mapping[str, Any] | None) -> JsonObject:
    merged = dict(DELIVERY_DEFAULTS)
    defaults = root.get("defaults")
    if isinstance(defaults, Mapping):
        nested = defaults.get("delivery")
        if isinstance(nested, Mapping):
            merged.update({k: v for k, v in nested.items() if v is not None})
    if isinstance(file_delivery, Mapping):
        merged.update({k: v for k, v in file_delivery.items() if v is not None})
    return merged


def _merge_context(root: Mapping[str, Any], file_context: Any) -> JsonObject:
    merged: JsonObject = {}
    root_context = root.get("context")
    if isinstance(root_context, Mapping):
        merged.update({k: v for k, v in root_context.items() if v is not None})
    defaults = root.get("defaults")
    if isinstance(defaults, Mapping):
        nested = defaults.get("context")
        if isinstance(nested, Mapping):
            merged.update({k: v for k, v in nested.items() if v is not None})
    if isinstance(file_context, Mapping):
        merged.update({k: v for k, v in file_context.items() if v is not None})
    return merged


def load_heartbeat(
    home: Path,
    name: str,
    *,
    root: Mapping[str, Any] | None = None,
    config_dir: str | None = None,
) -> JsonObject:
    """Return engine settings for one heartbeat (globals + delivery + collectors)."""

    cleaned = validate_name(name)
    resolved_root = (
        dict(root) if isinstance(root, Mapping) else load_root(home, config_dir=config_dir)
    )
    path = heartbeat_path(home, cleaned, config_dir=config_dir)
    if not path.is_file():
        raise RuntimeError(f"heartbeat config missing: {path}")
    loaded = read_json_object(path)
    collectors = loaded.get("collectors")
    settings: JsonObject = {key: resolved_root[key] for key in ROOT_SETTING_DEFAULTS}
    settings["name"] = cleaned
    settings["delivery"] = _merge_delivery(resolved_root, loaded.get("delivery"))
    settings["collectors"] = dict(collectors) if isinstance(collectors, Mapping) else {}
    settings["context"] = _merge_context(resolved_root, loaded.get("context"))
    return settings
