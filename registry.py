"""Load enabled collectors from $HERMES_HOME, not from this plugin."""

from __future__ import annotations

import importlib.util
import inspect
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

try:
    from . import _bootstrap  # noqa: F401
except ImportError:  # flat plugin-dir / unittest load
    import _bootstrap  # noqa: F401
from models import HeartbeatUseCase

JsonObject = dict[str, Any]
_PLUGIN_ROOT = Path(__file__).resolve().parent
_COLLECTOR_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


def _explicitly_enabled(section: Mapping[str, Any] | None) -> bool:
    return bool(section is not None and section.get("enabled") is True)


def _ensure_import_path(path: Path) -> None:
    resolved = str(path.resolve())
    if resolved not in sys.path:
        sys.path.insert(0, resolved)


def collector_path(collectors_dir: Path, collector_id: str) -> Path:
    if not _COLLECTOR_ID_RE.fullmatch(collector_id):
        raise RuntimeError(
            f"invalid collector id {collector_id!r}: expected {_COLLECTOR_ID_RE.pattern}"
        )
    root = collectors_dir.resolve()
    candidate = (root / f"{collector_id}.py").resolve()
    if not candidate.is_relative_to(root):
        raise RuntimeError(f"collector {collector_id!r} resolves outside {root}")
    return candidate


def _load_module(path: Path, collector_id: str) -> ModuleType:
    _ensure_import_path(_PLUGIN_ROOT)
    _ensure_import_path(path.parent)
    spec = importlib.util.spec_from_file_location(
        f"proactive_heartbeats_collector_{collector_id}",
        path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load collector {collector_id!r} from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _collector_class(module: ModuleType, collector_id: str) -> type:
    named = getattr(module, "Collector", None)
    if inspect.isclass(named) and callable(getattr(named, "collect", None)):
        return named
    matches: list[type] = []
    for value in vars(module).values():
        if not inspect.isclass(value) or not callable(getattr(value, "collect", None)):
            continue
        class_id = getattr(value, "id", None)
        if class_id in (None, collector_id):
            matches.append(value)
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise RuntimeError(f"collector {collector_id!r} has no Collector class or collect() type")
    raise RuntimeError(f"collector {collector_id!r} is ambiguous ({len(matches)} collect types)")


def _instantiate(cls: type, collector_id: str, config: Mapping[str, Any]) -> HeartbeatUseCase:
    instance = cls(config)
    if not getattr(instance, "id", None):
        instance.id = collector_id
    if instance.id != collector_id:
        raise RuntimeError(f"collector file {collector_id!r} declared id {instance.id!r}")
    if not callable(getattr(instance, "collect", None)):
        raise RuntimeError(f"collector {collector_id!r} has no collect()")
    return instance


def build_registry(
    settings: Mapping[str, Any] | None,
    *,
    collectors_dir: Path | None = None,
) -> tuple[HeartbeatUseCase, ...]:
    """Return enabled collectors whose modules exist under collectors_dir."""

    if not isinstance(settings, Mapping):
        return ()
    directory = collectors_dir
    if directory is None:
        raw = settings.get("collectors_dir")
        if isinstance(raw, str) and raw.strip():
            directory = Path(raw)
    if directory is None:
        return ()

    sections = settings.get("collectors")
    if not isinstance(sections, Mapping):
        return ()

    cases: list[HeartbeatUseCase] = []
    for collector_id, section in sections.items():
        if not isinstance(collector_id, str) or not collector_id.strip():
            continue
        if not isinstance(section, Mapping) or not _explicitly_enabled(section):
            continue
        path = collector_path(directory, collector_id)
        if not path.is_file():
            raise RuntimeError(f"enabled collector {collector_id!r} is missing: {path}")
        module = _load_module(path, collector_id)
        cls = _collector_class(module, collector_id)
        cases.append(_instantiate(cls, collector_id, section))
    return tuple(cases)


def enabled_ids(
    settings: Mapping[str, Any] | None,
    *,
    collectors_dir: Path | None = None,
) -> Sequence[str]:
    return tuple(case.id for case in build_registry(settings, collectors_dir=collectors_dir))
