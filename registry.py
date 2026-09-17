"""Assemble explicitly enabled, configured heartbeat use cases."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

try:
    from . import _bootstrap  # noqa: F401
except ImportError:  # flat plugin-dir / unittest load
    import _bootstrap  # noqa: F401
from models import HeartbeatUseCase
from use_cases._exec import DEFAULT_HOST_PROBE, resolve_source
from use_cases.github import DependabotAlertsUseCase, StalePullRequestsUseCase
from use_cases.host import HostHealthUseCase
from use_cases.sense import SenseUseCase

JsonObject = dict[str, Any]


def _section(settings: Mapping[str, Any], key: str) -> JsonObject | None:
    use_cases = settings.get("use_cases")
    if not isinstance(use_cases, Mapping):
        return None
    section = use_cases.get(key)
    if not isinstance(section, Mapping):
        return None
    return dict(section)


def _explicitly_enabled(section: Mapping[str, Any] | None) -> bool:
    return bool(section is not None and section.get("enabled") is True)


def _sense_configured(section: Mapping[str, Any]) -> bool:
    return resolve_source(section) is not None


def _host_configured(section: Mapping[str, Any]) -> bool:
    hosts = section.get("hosts")
    if not isinstance(hosts, list) or not hosts:
        return False
    for host in hosts:
        if not isinstance(host, Mapping):
            continue
        if not host.get("name"):
            continue
        if resolve_source(host, default_command=DEFAULT_HOST_PROBE) is not None:
            return True
    return False


def _owner_configured(section: Mapping[str, Any]) -> bool:
    owner = section.get("owner")
    return isinstance(owner, str) and bool(owner.strip())


def build_registry(settings: Mapping[str, Any] | None) -> tuple[HeartbeatUseCase, ...]:
    """Return only explicitly enabled and configured built-in use cases."""

    if not isinstance(settings, Mapping):
        return ()

    cases: list[HeartbeatUseCase] = []

    sense = _section(settings, "sense")
    if _explicitly_enabled(sense) and sense is not None and _sense_configured(sense):
        cases.append(SenseUseCase(sense))

    host = _section(settings, "host")
    if _explicitly_enabled(host) and host is not None and _host_configured(host):
        cases.append(HostHealthUseCase(host))

    stale = _section(settings, "stale_prs")
    if _explicitly_enabled(stale) and stale is not None and _owner_configured(stale):
        cases.append(StalePullRequestsUseCase(stale))

    alerts = _section(settings, "dependabot_alerts")
    if _explicitly_enabled(alerts) and alerts is not None and _owner_configured(alerts):
        cases.append(DependabotAlertsUseCase(alerts))

    return tuple(cases)


def enabled_ids(settings: Mapping[str, Any] | None) -> Sequence[str]:
    """Helper for callers that only need the selected use-case ids."""

    return tuple(case.id for case in build_registry(settings))
