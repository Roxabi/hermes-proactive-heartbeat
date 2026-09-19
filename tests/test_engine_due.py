from __future__ import annotations

from datetime import timedelta
from typing import Any

from engine import HeartbeatEngine
from models import Snapshot
from tests.engine_fakes import (
    NOW,
    FakeTypeSafe,
    FakeUseCase,
    choice_answer,
    choice_signal,
    context,
    question_items,
    rule_signal,
)
from tests.isolation import IsolatedHomeTestCase


class HeartbeatDueTests(IsolatedHomeTestCase):
    def test_failed_collector_fails_closed_and_queues_healthy_dues(self) -> None:
        failing = FakeUseCase("broken", [RuntimeError("collector offline")])
        healthy = FakeUseCase(
            "healthy",
            [Snapshot(signals=(choice_signal("new", fallback_label="notify"),))],
        )
        previous = {
            "version": 2,
            "use_cases": {
                "broken": {"state": {"cursor": "keep"}, "active": ["old"]},
                "healthy": {"state": {}, "active": []},
            },
            "delivered": {},
            "pending": {},
        }

        result = HeartbeatEngine(
            [failing, healthy],
            typesafe=FakeTypeSafe(None),
        ).tick(context(), previous_state=previous)

        # Hard collector failures fail closed: no wake this tick; queue healthy dues.
        self.assertIsNone(result.candidate)
        self.assertEqual(result.render(), '{"wakeAgent": false}')
        self.assertEqual(
            result.state["use_cases"]["broken"],
            {"state": {"cursor": "keep"}, "active": ["old"]},
        )
        self.assertEqual(result.state["use_cases"]["healthy"]["active"], ["new"])
        self.assertIsInstance(result.state["pending"], dict)
        self.assertIn("healthy:new", result.state["pending"])
        self.assertIn("broken", result.diagnostics)

    def test_delivered_signal_stays_silent_inside_default_cooldown(self) -> None:
        signal = choice_signal("disk:root", fallback_label="notify")
        snapshot = Snapshot(signals=(signal,), state={"sample": 1})
        typesafe = FakeTypeSafe(None)
        engine = HeartbeatEngine(
            [FakeUseCase("host", [Snapshot(), snapshot, snapshot])],
            typesafe=typesafe,
        )
        baseline = engine.tick(context(), previous_state=None)
        woken = engine.tick(context(), previous_state=baseline.state)

        result = engine.tick(context(), previous_state=woken.state)

        self.assertIsNotNone(woken.candidate)
        self.assertIsNone(result.candidate)
        self.assertEqual(result.render(), '{"wakeAgent": false}')
        self.assertEqual(len(typesafe.calls), 1)

    def test_delivered_signal_is_due_again_after_default_cooldown(self) -> None:
        signal = choice_signal("disk:root", fallback_label="notify")
        snapshot = Snapshot(signals=(signal,), state={"sample": 1})
        typesafe = FakeTypeSafe(None)
        engine = HeartbeatEngine(
            [FakeUseCase("host", [Snapshot(), snapshot, snapshot])],
            typesafe=typesafe,
        )
        baseline = engine.tick(context(), previous_state=None)
        woken = engine.tick(context(), previous_state=baseline.state)

        result = engine.tick(
            context(now=NOW + timedelta(seconds=14_400)),
            previous_state=woken.state,
        )

        self.assertIsNotNone(result.candidate)
        assert result.candidate is not None
        self.assertEqual(result.candidate.fingerprint, "disk:root")
        self.assertEqual(len(typesafe.calls), 2)

    def test_signal_present_at_the_baseline_tick_waits_one_cooldown(self) -> None:
        snapshot = Snapshot(signals=(rule_signal("disk:root", priority=70),), state={"sample": 1})
        engine = HeartbeatEngine(
            [FakeUseCase("host", [snapshot, snapshot, snapshot])],
            typesafe=FakeTypeSafe({}),
        )

        baseline = engine.tick(context(), previous_state=None)
        inside = engine.tick(
            context(now=NOW + timedelta(seconds=14_399)),
            previous_state=baseline.state,
        )
        elapsed = engine.tick(
            context(now=NOW + timedelta(seconds=14_400)),
            previous_state=inside.state,
        )

        self.assertIsNone(baseline.candidate)
        self.assertIsNone(inside.candidate)
        self.assertIsNotNone(elapsed.candidate)
        assert elapsed.candidate is not None
        self.assertEqual(elapsed.candidate.fingerprint, "disk:root")
        self.assertEqual(elapsed.candidate.decision["source"], "rule")

    def test_active_signal_without_a_delivery_record_stays_eligible(self) -> None:
        signal = choice_signal("disk:root", fallback_label="notify")
        previous = {
            "version": 2,
            "use_cases": {"host": {"state": {}, "active": ["disk:root"]}},
            "delivered": {},
            "pending": {},
        }

        result = HeartbeatEngine(
            [FakeUseCase("host", [Snapshot(signals=(signal,), state={"sample": 1})])],
            typesafe=FakeTypeSafe(None),
        ).tick(context(), previous_state=previous)

        self.assertIsNotNone(result.candidate)
        assert result.candidate is not None
        self.assertEqual(result.candidate.fingerprint, "disk:root")

    def test_per_signal_repeat_after_gates_reeligibility(self) -> None:
        signal = choice_signal("disk:root", fallback_label="notify", repeat_after_seconds=60)
        snapshot = Snapshot(signals=(signal,), state={"sample": 1})
        typesafe = FakeTypeSafe(None)
        engine = HeartbeatEngine(
            [FakeUseCase("host", [Snapshot(), snapshot, snapshot, snapshot])],
            typesafe=typesafe,
        )
        baseline = engine.tick(context(), previous_state=None)
        woken = engine.tick(context(), previous_state=baseline.state)

        early = engine.tick(
            context(now=NOW + timedelta(seconds=59)),
            previous_state=woken.state,
        )
        due = engine.tick(
            context(now=NOW + timedelta(seconds=60)),
            previous_state=early.state,
        )

        self.assertIsNone(early.candidate)
        self.assertEqual(early.render(), '{"wakeAgent": false}')
        self.assertIsNotNone(due.candidate)
        assert due.candidate is not None
        self.assertEqual(due.candidate.fingerprint, "disk:root")

    def test_collector_sees_the_action_that_actually_woke_the_agent(self) -> None:
        snapshot = Snapshot(signals=(rule_signal("disk:root", priority=70),))
        use_case = FakeUseCase("host", [Snapshot(), snapshot, snapshot])
        engine = HeartbeatEngine([use_case], typesafe=FakeTypeSafe({}))
        baseline = engine.tick(context(), previous_state=None)
        woken = engine.tick(context(), previous_state=baseline.state)

        engine.tick(context(), previous_state=woken.state)

        assert woken.candidate is not None
        self.assertEqual(woken.candidate.action.name, "notify")
        self.assertEqual(use_case.delivered_views[0], {})
        self.assertEqual(use_case.delivered_views[1], {})
        self.assertEqual(
            use_case.delivered_views[2],
            {"disk:root": {"at": "2026-09-17T12:00:00Z", "action": "notify"}},
        )

    def test_due_signal_that_lost_the_tick_is_visible_as_silent(self) -> None:
        quiet = FakeUseCase(
            "alpha",
            [Snapshot(), Snapshot(signals=(choice_signal("low", priority=20),)), Snapshot()],
        )
        loud = FakeUseCase(
            "beta",
            [Snapshot(), Snapshot(signals=(rule_signal("high", priority=80),)), Snapshot()],
        )

        def answer_silent(questions: Any) -> dict[str, Any]:
            return {
                question_id: choice_answer("silent")
                for question_id, _question in question_items(questions)
            }

        engine = HeartbeatEngine([quiet, loud], typesafe=FakeTypeSafe(answer_silent))
        baseline = engine.tick(context(), previous_state=None)
        first = engine.tick(context(), previous_state=baseline.state)

        engine.tick(context(), previous_state=first.state)

        assert first.candidate is not None
        self.assertEqual(first.candidate.collector, "beta")
        self.assertEqual(
            loud.delivered_views[2],
            {"high": {"at": "2026-09-17T12:00:00Z", "action": "notify"}},
        )
        self.assertEqual(
            quiet.delivered_views[2],
            {"low": {"at": "2026-09-17T12:00:00Z", "action": "silent"}},
        )

    def test_baselined_fingerprint_is_visible_as_baseline(self) -> None:
        snapshot = Snapshot(signals=(choice_signal("disk:root"),))
        use_case = FakeUseCase("host", [snapshot, snapshot])
        engine = HeartbeatEngine([use_case], typesafe=FakeTypeSafe({}))
        baseline = engine.tick(context(), previous_state=None)

        engine.tick(context(), previous_state=baseline.state)

        self.assertEqual(
            use_case.delivered_views,
            [
                {},
                {"disk:root": {"at": "2026-09-17T12:00:00Z", "action": "baseline"}},
            ],
        )
