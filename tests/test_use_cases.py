from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime, timezone

from models import TickContext
from registry import build_registry
from tests.isolation import IsolatedHomeTestCase
from use_cases.github import DependabotAlertsUseCase, StalePullRequestsUseCase
from use_cases.host import HostHealthUseCase
from use_cases.sense import SenseUseCase

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


def make_runner(payloads: dict[tuple[str, ...], str]):
    calls: list[tuple[tuple[str, ...], float]] = []

    def run_command(command: Sequence[str], timeout: float) -> str:
        key = tuple(command)
        calls.append((key, timeout))
        if key not in payloads:
            raise AssertionError(f"unexpected command: {list(command)}")
        return payloads[key]

    run_command.calls = calls  # type: ignore[attr-defined]
    return run_command


class RegistryTests(IsolatedHomeTestCase):
    def test_empty_settings_build_an_empty_registry(self) -> None:
        registry = build_registry({})

        self.assertEqual(list(registry), [])

    def test_configured_enabled_use_cases_are_built_in_stable_order(self) -> None:
        settings = {
            "use_cases": {
                "sense": {
                    "enabled": True,
                    "command": ["sense-status"],
                },
                "host": {
                    "enabled": True,
                    "hosts": [{"name": "local", "command": ["host-status"]}],
                },
                "stale_prs": {
                    "enabled": True,
                    "owner": "acme",
                },
                "dependabot_alerts": {
                    "enabled": True,
                    "owner": "acme",
                },
            }
        }

        registry = build_registry(settings)
        ids = [use_case.id for use_case in registry]

        self.assertEqual(ids, ["sense", "host", "stale_prs", "dependabot_alerts"])


class HostHealthUseCaseTests(IsolatedHomeTestCase):
    def test_disk_threshold_emits_active_disk_signal(self) -> None:
        command = ["host-status", "--json"]
        runner = make_runner(
            {
                tuple(command): json.dumps(
                    {
                        "load1": 0.1,
                        "disk_pct": 91.0,
                        "failed_units": 0,
                        "gpu_free_mib": 2000,
                    }
                )
            }
        )
        use_case = HostHealthUseCase(
            {
                "enabled": True,
                "hosts": [{"name": "local", "command": command}],
                "disk_threshold_percent": 90,
                "gpu_free_threshold_mib": 1024,
            },
            run_command=runner,
        )
        context = TickContext(now=NOW, settings={"use_cases": {"host": {}}})

        snapshot = use_case.collect(context, {})

        fingerprints = [signal.fingerprint for signal in snapshot.signals]
        self.assertEqual(fingerprints, ["local:disk"])
        self.assertEqual(snapshot.signals[0].facts["disk_pct"], 91.0)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(runner.calls[0][0], tuple(command))


class SenseUseCaseTests(IsolatedHomeTestCase):
    def test_care_action_maps_water_signal(self) -> None:
        command = ["sense-status"]
        runner = make_runner(
            {
                tuple(command): json.dumps(
                    {
                        "signals": ["post_peak_idle"],
                        "shape": "care",
                        "current_stretch": {"app": "Code", "minutes": 12},
                        "last_away": {"ongoing": False, "minutes": 0},
                    }
                )
            }
        )
        use_case = SenseUseCase(
            {
                "enabled": True,
                "command": command,
            },
            run_command=runner,
        )
        context = TickContext(now=NOW, settings={"use_cases": {"sense": {}}})

        snapshot = use_case.collect(context, {})

        fingerprints = [signal.fingerprint for signal in snapshot.signals]
        self.assertIn("post_peak_idle", fingerprints)
        water = next(
            signal for signal in snapshot.signals if signal.fingerprint == "post_peak_idle"
        )
        self.assertEqual(water.judgment.fallback_label, "water")
        self.assertIn("water", water.judgment.actions)
        self.assertEqual(water.judgment.actions["water"].name, "water")


class StalePullRequestsUseCaseTests(IsolatedHomeTestCase):
    def test_stale_human_pr_is_emitted_and_dependabot_is_skipped(self) -> None:
        command = (
            "gh",
            "search",
            "prs",
            "--owner=acme",
            "--state=open",
            "--limit=20",
            "--json=number,title,url,createdAt,author,repository",
        )
        runner = make_runner(
            {
                command: json.dumps(
                    [
                        {
                            "number": 12,
                            "title": "Stale widget fix",
                            "createdAt": "2026-09-01T00:00:00Z",
                            "author": {"login": "alice"},
                            "repository": {"name": "widget"},
                        },
                        {
                            "number": 13,
                            "title": "bump deps",
                            "createdAt": "2026-09-01T00:00:00Z",
                            "author": {"login": "dependabot[bot]"},
                            "repository": {"name": "widget"},
                        },
                        {
                            "number": 14,
                            "title": "fresh",
                            "createdAt": "2026-09-16T00:00:00Z",
                            "author": {"login": "bob"},
                            "repository": {"name": "widget"},
                        },
                    ]
                )
            }
        )
        use_case = StalePullRequestsUseCase(
            {"enabled": True, "owner": "acme"},
            run_command=runner,
        )

        snapshot = use_case.collect(TickContext(now=NOW, settings={}), {})

        self.assertEqual(
            [signal.fingerprint for signal in snapshot.signals],
            ["pr:widget#12"],
        )
        self.assertEqual(snapshot.signals[0].facts["n"], 12)
        self.assertEqual(snapshot.signals[0].facts["age_d"], 16)
        self.assertEqual(snapshot.signals[0].facts["title"], "Stale widget fix")
        self.assertEqual(runner.calls[0][0], command)


class DependabotAlertsUseCaseTests(IsolatedHomeTestCase):
    def test_high_severity_alert_is_emitted_and_low_is_skipped(self) -> None:
        command = (
            "gh",
            "api",
            "orgs/acme/dependabot/alerts",
            "-f",
            "state=open",
            "-f",
            "per_page=10",
        )
        runner = make_runner(
            {
                command: json.dumps(
                    [
                        {
                            "created_at": "2026-09-01T00:00:00Z",
                            "security_advisory": {"severity": "critical"},
                            "repository": {"name": "widget"},
                            "dependency": {"package": {"name": "leftpad"}},
                        },
                        {
                            "created_at": "2026-09-01T00:00:00Z",
                            "security_advisory": {"severity": "low"},
                            "repository": {"name": "widget"},
                            "dependency": {"package": {"name": "lodash"}},
                        },
                    ]
                )
            }
        )
        use_case = DependabotAlertsUseCase(
            {"enabled": True, "owner": "acme"},
            run_command=runner,
        )

        snapshot = use_case.collect(TickContext(now=NOW, settings={}), {})

        self.assertEqual(
            [signal.fingerprint for signal in snapshot.signals],
            ["cve:widget:leftpad"],
        )
        self.assertEqual(snapshot.signals[0].facts["sev"], "critical")
        self.assertEqual(runner.calls[0][0], command)
