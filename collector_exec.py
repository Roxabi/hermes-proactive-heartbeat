"""Command execution helpers shared by collectors."""

from __future__ import annotations

import json
import shlex
import subprocess
from collections.abc import Callable, Mapping, Sequence
from typing import Any

try:
    from . import _bootstrap  # noqa: F401
except ImportError:  # flat plugin-dir / unittest load
    import _bootstrap  # noqa: F401

JsonObject = dict[str, Any]
RunCommand = Callable[[Sequence[str], float], str]

DEFAULT_SSH_OPTIONS: tuple[str, ...] = (
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=8",
)


def default_run_command(argv: Sequence[str], timeout: float) -> str:
    """Run a local argv list and return stdout on success, else empty string."""

    try:
        completed = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return completed.stdout if completed.returncode == 0 else ""


def as_str_list(value: Any) -> list[str] | None:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        items = [str(item) for item in value]
        return items if items else None
    return None


def resolve_source(config: Mapping[str, Any], *, default_command: Any = None) -> JsonObject | None:
    """Normalize local/SSH source config from nested or flat keys."""

    raw = config.get("source")
    source: JsonObject
    if isinstance(raw, Mapping):
        source = dict(raw)
    else:
        source = {
            key: config[key]
            for key in ("transport", "command", "host", "ssh_options")
            if key in config
        }
    if "command" not in source and default_command is not None:
        source["command"] = default_command
    if "command" not in source:
        return None
    transport = str(source.get("transport") or "local").lower()
    source["transport"] = transport
    if transport == "ssh" and not source.get("host"):
        return None
    return source


def build_argv(source: Mapping[str, Any]) -> list[str] | None:
    """Build a local argv list for a configured local or SSH source."""

    command = source.get("command")
    transport = str(source.get("transport") or "local").lower()
    if transport == "local":
        if isinstance(command, str):
            return ["bash", "-lc", command]
        argv = as_str_list(command)
        return list(argv) if argv else None

    host = source.get("host")
    if not isinstance(host, str) or not host.strip():
        return None
    options = as_str_list(source.get("ssh_options")) or list(DEFAULT_SSH_OPTIONS)
    if isinstance(command, str):
        remote = command
    else:
        argv = as_str_list(command)
        if not argv:
            return None
        remote = " ".join(shlex.quote(part) for part in argv)
    return ["ssh", *options, host.strip(), remote]


def load_json_payload(
    run_command: RunCommand,
    argv: Sequence[str],
    *,
    timeout: float,
) -> tuple[Any | None, str | None]:
    """Return (payload, error). error is set when the collector should not emit facts."""

    raw = run_command(argv, timeout).strip()
    if not raw:
        return None, "unavailable"
    try:
        return json.loads(raw), None
    except json.JSONDecodeError:
        return None, "unparseable"


def float_or_default(value: Any, default: float) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def int_or_default(value: Any, default: int) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default
