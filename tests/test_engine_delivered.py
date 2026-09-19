from __future__ import annotations

from datetime import timedelta

from engine import HeartbeatEngine
from models import Snapshot
from tests.engine_fakes import NOW, FakeTypeSafe, FakeUseCase, context, rule_signal
from tests.isolation import IsolatedHomeTestCase


class HeartbeatDeliveredTests(IsolatedHomeTestCase):
    def test_collector_never_sees_a_sibling_collectors_fingerprints(self) -> None:
        alpha = FakeUseCase(
            "alpha",
            [Snapshot(signals=(rule_signal("cve:repo:pkg"),)), Snapshot()],
        )
        beta = FakeUseCase(
            "beta",
            [Snapshot(signals=(rule_signal("beta:cve:repo:pkg"),)), Snapshot()],
        )
        engine = HeartbeatEngine([alpha, beta], typesafe=FakeTypeSafe({}))

        baseline = engine.tick(context(), previous_state=None)
        engine.tick(context(), previous_state=baseline.state)

        self.assertEqual(
            alpha.delivered_views[1],
            {"cve:repo:pkg": {"at": "2026-09-17T12:00:00Z", "action": "baseline"}},
        )
        self.assertEqual(
            beta.delivered_views[1],
            {"beta:cve:repo:pkg": {"at": "2026-09-17T12:00:00Z", "action": "baseline"}},
        )

    def test_delivery_record_of_a_vanished_fingerprint_is_not_persisted(self) -> None:
        previous = {
            "version": 2,
            "use_cases": {"host": {"state": {}, "active": ["disk:root", "disk:old"]}},
            "delivered": {
                "host:disk:root": {"at": "2026-09-17T12:00:00Z", "action": "notify"},
                "host:disk:old": {"at": "2026-09-17T08:00:00Z", "action": "notify"},
            },
            "pending": {},
        }

        result = HeartbeatEngine(
            [FakeUseCase("host", [Snapshot(signals=(rule_signal("disk:root"),))])],
            typesafe=FakeTypeSafe({}),
        ).tick(context(), previous_state=previous)

        self.assertEqual(
            result.state["delivered"],
            {"host:disk:root": {"at": "2026-09-17T12:00:00Z", "action": "notify"}},
        )

    def test_cooldown_still_suppresses_a_retained_active_fingerprint(self) -> None:
        signal = rule_signal("disk:root", priority=70)
        previous = {
            "version": 2,
            "use_cases": {"host": {"state": {}, "active": ["disk:root"]}},
            "delivered": {
                "host:disk:root": {"at": "2026-09-17T12:00:00Z", "action": "notify"},
                "host:disk:gone": {"at": "2026-09-17T08:00:00Z", "action": "notify"},
            },
            "pending": {},
        }
        engine = HeartbeatEngine(
            [FakeUseCase("host", [Snapshot(signals=(signal,)), Snapshot(signals=(signal,))])],
            typesafe=FakeTypeSafe({}),
        )

        first = engine.tick(context(now=NOW + timedelta(seconds=1)), previous_state=previous)
        second = engine.tick(
            context(now=NOW + timedelta(seconds=2)),
            previous_state=first.state,
        )

        self.assertIsNone(first.candidate)
        self.assertIsNone(second.candidate)
        self.assertEqual(second.render(), '{"wakeAgent": false}')
        self.assertEqual(
            second.state["delivered"],
            {"host:disk:root": {"at": "2026-09-17T12:00:00Z", "action": "notify"}},
        )

    def test_failed_collector_keeps_all_of_its_delivery_records(self) -> None:
        failing = FakeUseCase("broken", [RuntimeError("collector offline")])
        healthy = FakeUseCase("healthy", [Snapshot(signals=(rule_signal("new"),))])
        previous = {
            "version": 2,
            "use_cases": {
                "broken": {"state": {}, "active": ["old"]},
                "healthy": {"state": {}, "active": ["healthy:gone"]},
            },
            "delivered": {
                "broken:old": {"at": "2026-09-17T11:00:00Z", "action": "notify"},
                "broken:ancient": {"at": "2026-09-16T11:00:00Z", "action": "notify"},
                "healthy:gone": {"at": "2026-09-17T11:00:00Z", "action": "notify"},
            },
            "pending": {},
        }

        result = HeartbeatEngine(
            [failing, healthy],
            typesafe=FakeTypeSafe({}),
        ).tick(context(), previous_state=previous)

        self.assertIn("broken", result.diagnostics)
        self.assertEqual(
            result.state["delivered"],
            {
                "broken:old": {"at": "2026-09-17T11:00:00Z", "action": "notify"},
                "broken:ancient": {"at": "2026-09-16T11:00:00Z", "action": "notify"},
            },
        )

    def test_records_of_a_collector_not_loaded_this_tick_survive(self) -> None:
        previous = {
            "version": 2,
            "use_cases": {"host": {"state": {}, "active": []}},
            "delivered": {
                "retired:cve:repo:pkg": {"at": "2026-09-17T11:00:00Z", "action": "notify"},
                "host:disk:old": {"at": "2026-09-17T11:00:00Z", "action": "notify"},
            },
            "pending": {},
        }

        result = HeartbeatEngine(
            [FakeUseCase("host", [Snapshot()])],
            typesafe=FakeTypeSafe({}),
        ).tick(context(), previous_state=previous)

        self.assertEqual(
            result.state["delivered"],
            {"retired:cve:repo:pkg": {"at": "2026-09-17T11:00:00Z", "action": "notify"}},
        )

    def test_a_fingerprint_containing_a_colon_is_matched_to_its_own_collector(self) -> None:
        alpha = FakeUseCase("alpha", [Snapshot(signals=(rule_signal("cve:repo:pkg"),))])
        beta = FakeUseCase("beta", [Snapshot()])
        previous = {
            "version": 2,
            "use_cases": {
                "alpha": {"state": {}, "active": ["cve:repo:pkg"]},
                "beta": {"state": {}, "active": ["cve:repo:pkg"]},
            },
            "delivered": {
                "alpha:cve:repo:pkg": {"at": "2026-09-17T12:00:00Z", "action": "notify"},
                "alpha:cve:repo:old": {"at": "2026-09-17T08:00:00Z", "action": "notify"},
                "beta:cve:repo:pkg": {"at": "2026-09-17T12:00:00Z", "action": "notify"},
            },
            "pending": {},
        }

        result = HeartbeatEngine([alpha, beta], typesafe=FakeTypeSafe({})).tick(
            context(),
            previous_state=previous,
        )

        self.assertEqual(
            result.state["delivered"],
            {"alpha:cve:repo:pkg": {"at": "2026-09-17T12:00:00Z", "action": "notify"}},
        )
