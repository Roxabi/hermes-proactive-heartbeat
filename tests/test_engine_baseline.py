from __future__ import annotations

from engine import HeartbeatEngine
from models import Snapshot
from tests.engine_fakes import (
    FakeTypeSafe,
    FakeUseCase,
    choice_signal,
    context,
    rule_signal,
)
from tests.isolation import IsolatedHomeTestCase


class HeartbeatBaselineTests(IsolatedHomeTestCase):
    def test_first_tick_is_a_silent_baseline_and_persists_active_fingerprints(self) -> None:
        typesafe = FakeTypeSafe({})
        use_case = FakeUseCase(
            "host",
            [Snapshot(signals=(choice_signal("disk:root"),), state={"sample": 1})],
        )

        result = HeartbeatEngine([use_case], typesafe=typesafe).tick(
            context(),
            previous_state=None,
        )

        self.assertIsNone(result.candidate)
        self.assertEqual(result.render(), '{"wakeAgent": false}')
        self.assertEqual(typesafe.calls, [])
        self.assertEqual(
            result.state,
            {
                "version": 2,
                "use_cases": {
                    "host": {"state": {"sample": 1}, "active": ["disk:root"]},
                },
                "delivered": {
                    "host:disk:root": {
                        "at": "2026-09-17T12:00:00Z",
                        "action": "baseline",
                    }
                },
                "pending": {},
            },
        )

    def test_initial_observation_eligible_can_wake_on_tick_one(self) -> None:
        default = FakeUseCase(
            "default",
            [Snapshot(signals=(rule_signal("default"),))],
        )
        eligible = FakeUseCase(
            "eligible",
            [Snapshot(signals=(rule_signal("eligible", initial_observation="eligible"),))],
        )

        default_result = HeartbeatEngine([default], typesafe=FakeTypeSafe({})).tick(
            context(),
            previous_state=None,
        )
        eligible_typesafe = FakeTypeSafe({})
        eligible_result = HeartbeatEngine(
            [eligible],
            typesafe=eligible_typesafe,
        ).tick(context(), previous_state=None)

        self.assertIsNone(default_result.candidate)
        self.assertIsNotNone(eligible_result.candidate)
        assert eligible_result.candidate is not None
        self.assertEqual(eligible_result.candidate.fingerprint, "eligible")
        self.assertEqual(eligible_result.candidate.decision["source"], "rule")
        self.assertEqual(eligible_typesafe.calls, [])

    def test_unchanged_active_signal_stays_silent_without_semantic_evaluation(self) -> None:
        typesafe = FakeTypeSafe({})
        use_case = FakeUseCase(
            "host",
            [
                Snapshot(signals=(choice_signal("disk:root"),), state={"sample": 1}),
                Snapshot(signals=(choice_signal("disk:root"),), state={"sample": 2}),
            ],
        )
        engine = HeartbeatEngine([use_case], typesafe=typesafe)
        baseline = engine.tick(context(), previous_state=None)

        result = engine.tick(context(), previous_state=baseline.state)

        self.assertIsNone(result.candidate)
        self.assertEqual(result.render(), '{"wakeAgent": false}')
        self.assertEqual(typesafe.calls, [])
        self.assertEqual(result.state["use_cases"]["host"]["active"], ["disk:root"])
        self.assertEqual(use_case.previous_states, [{}, {"sample": 1}])

    def test_direct_waking_rule_skips_typesafe(self) -> None:
        typesafe = FakeTypeSafe({})
        engine = HeartbeatEngine(
            [
                FakeUseCase(
                    "rules",
                    [Snapshot(), Snapshot(signals=(rule_signal("urgent", priority=90),))],
                )
            ],
            typesafe=typesafe,
        )
        baseline = engine.tick(context(), previous_state=None)

        result = engine.tick(context(), previous_state=baseline.state)

        self.assertIsNotNone(result.candidate)
        assert result.candidate is not None
        self.assertEqual(result.candidate.fingerprint, "urgent")
        self.assertEqual(result.candidate.decision["source"], "rule")
        self.assertEqual(typesafe.calls, [])

    def test_direct_silent_action_remains_active_without_wake(self) -> None:
        typesafe = FakeTypeSafe({})
        signal = rule_signal("record", wake_agent=False)
        engine = HeartbeatEngine(
            [FakeUseCase("rules", [Snapshot(), Snapshot(signals=(signal,))])],
            typesafe=typesafe,
        )
        baseline = engine.tick(context(), previous_state=None)

        result = engine.tick(context(), previous_state=baseline.state)

        self.assertIsNone(result.candidate)
        self.assertEqual(result.render(), '{"wakeAgent": false}')
        self.assertEqual(result.state["use_cases"]["rules"]["active"], ["record"])
        self.assertEqual(typesafe.calls, [])
