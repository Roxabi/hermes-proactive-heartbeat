"""Built-in heartbeat use cases."""

from __future__ import annotations

try:
    from .. import _bootstrap  # noqa: F401
except ImportError:  # flat plugin-dir / unittest load
    import _bootstrap  # noqa: F401
from use_cases.github import DependabotAlertsUseCase, StalePullRequestsUseCase
from use_cases.host import HostHealthUseCase
from use_cases.sense import SenseUseCase

__all__ = [
    "DependabotAlertsUseCase",
    "HostHealthUseCase",
    "SenseUseCase",
    "StalePullRequestsUseCase",
]
