from __future__ import annotations

from typing import Any

from engine import HeartbeatEngine
from models import Snapshot
from tests.engine_fakes import (
    FakeTypeSafe,
    FakeUseCase,
    choice_answer,
    choice_signal,
    context,
    observation_ids,
    question_items,
    rule_signal,
)
from tests.isolation import IsolatedHomeTestCase


class HeartbeatTypeSafeTests(IsolatedHomeTestCase):
    def test_new_signal_uses_its_fallback_when_typesafe_is_unavailable(self) -> None:
        notify = choice_signal("disk:root", fallback_label="notify")
        use_case = FakeUseCase(
            "host",
            [
                Snapshot(state={"sample": 1}),
                Snapshot(signals=(notify,), state={"sample": 2}),
            ],
        )
        typesafe = FakeTypeSafe(None)
        engine = HeartbeatEngine([use_case], typesafe=typesafe)
        baseline = engine.tick(context(), previous_state=None)

        result = engine.tick(context(), previous_state=baseline.state)

        self.assertIsNotNone(result.candidate)
        assert result.candidate is not None
        self.assertEqual(result.candidate.fingerprint, "disk:root")
        self.assertEqual(result.candidate.action.name, "notify")
        self.assertEqual(result.candidate.decision["source"], "fallback")
        self.assertEqual(len(typesafe.calls), 1)

    def test_all_due_questions_are_batched_and_packed_into_one_wake(self) -> None:
        low = FakeUseCase(
            "alpha",
            [Snapshot(), Snapshot(signals=(choice_signal("low", priority=20),))],
        )
        high = FakeUseCase(
            "beta",
            [Snapshot(), Snapshot(signals=(choice_signal("high", priority=80),))],
        )

        def answer_notify(questions: Any) -> dict[str, Any]:
            return {
                question_id: choice_answer("notify")
                for question_id, _question in question_items(questions)
            }

        typesafe = FakeTypeSafe(answer_notify)
        engine = HeartbeatEngine([high, low], typesafe=typesafe)
        baseline = engine.tick(context(), previous_state=None)

        result = engine.tick(context(), previous_state=baseline.state)

        self.assertEqual(len(typesafe.calls), 1)
        _, questions = typesafe.calls[0]
        items = question_items(questions)
        self.assertEqual(len(items), 2)
        question_ids = [question_id for question_id, _question in items]
        self.assertEqual(len(set(question_ids)), 2)
        self.assertTrue(any(question_id.startswith("alpha:") for question_id in question_ids))
        self.assertTrue(any(question_id.startswith("beta:") for question_id in question_ids))
        self.assertIsNotNone(result.candidate)
        assert result.candidate is not None
        self.assertEqual(result.candidate.collector, "bundle")
        self.assertEqual(result.candidate.fingerprint, "tick")
        self.assertEqual(result.candidate.action.name, "bundle")
        self.assertEqual(result.candidate.action.priority, 80)
        self.assertEqual(observation_ids(result), [("beta", "high"), ("alpha", "low")])
        self.assertEqual(result.state["pending"], {})
        self.assertIn("beta:high", result.state["delivered"])
        self.assertIn("alpha:low", result.state["delivered"])

    def test_mixed_rules_and_semantic_signals_are_packed_together(self) -> None:
        direct = FakeUseCase(
            "alpha",
            [Snapshot(), Snapshot(signals=(rule_signal("rule", priority=90),))],
        )
        semantic = FakeUseCase(
            "beta",
            [Snapshot(), Snapshot(signals=(choice_signal("semantic", priority=80),))],
        )

        def answer_notify(questions: Any) -> dict[str, Any]:
            return {
                question_id: choice_answer("notify")
                for question_id, _question in question_items(questions)
            }

        typesafe = FakeTypeSafe(answer_notify)
        engine = HeartbeatEngine([semantic, direct], typesafe=typesafe)
        baseline = engine.tick(context(), previous_state=None)

        result = engine.tick(context(), previous_state=baseline.state)

        self.assertEqual(len(typesafe.calls), 1)
        _, questions = typesafe.calls[0]
        self.assertEqual(
            [question_id for question_id, _question in question_items(questions)],
            ["beta:semantic"],
        )
        self.assertIsNotNone(result.candidate)
        assert result.candidate is not None
        self.assertEqual(result.candidate.collector, "bundle")
        self.assertEqual(
            observation_ids(result),
            [("alpha", "rule"), ("beta", "semantic")],
        )
        self.assertEqual(result.state["pending"], {})
        self.assertEqual(result.state["delivered"]["alpha:rule"]["action"], "notify")
        self.assertEqual(result.state["delivered"]["beta:semantic"]["action"], "notify")

    def test_packed_wake_stamps_every_observation_so_cooldown_applies(self) -> None:
        low_signal = choice_signal("low", priority=20)
        high_signal = choice_signal("high", priority=80)
        low = FakeUseCase(
            "alpha",
            [Snapshot(), Snapshot(signals=(low_signal,)), Snapshot(signals=(low_signal,))],
        )
        high = FakeUseCase(
            "beta",
            [Snapshot(), Snapshot(signals=(high_signal,)), Snapshot(signals=(high_signal,))],
        )

        def answer_notify(questions: Any) -> dict[str, Any]:
            return {
                question_id: choice_answer("notify")
                for question_id, _question in question_items(questions)
            }

        typesafe = FakeTypeSafe(answer_notify)
        engine = HeartbeatEngine([high, low], typesafe=typesafe)
        baseline = engine.tick(context(), previous_state=None)
        first = engine.tick(context(), previous_state=baseline.state)
        second = engine.tick(context(), previous_state=first.state)

        assert first.candidate is not None
        self.assertEqual(observation_ids(first), [("beta", "high"), ("alpha", "low")])
        self.assertEqual(first.state["pending"], {})
        self.assertIsNone(second.candidate)
        self.assertEqual(second.state["pending"], {})
        self.assertEqual(len(typesafe.calls), 1)

    def test_packed_wake_does_not_reopen_a_delivered_observation_when_a_sibling_drops(self) -> None:
        low_before = choice_signal("low", priority=20, facts={"sample": 1})
        low_after = choice_signal("low", priority=20, facts={"sample": 2})
        high = choice_signal("high", priority=80)
        low_use_case = FakeUseCase(
            "alpha",
            [Snapshot(), Snapshot(signals=(low_before,)), Snapshot(signals=(low_after,))],
        )
        high_use_case = FakeUseCase(
            "beta",
            [Snapshot(), Snapshot(signals=(high,)), Snapshot()],
        )

        def answer_notify(questions: Any) -> dict[str, Any]:
            return {
                question_id: choice_answer("notify")
                for question_id, _question in question_items(questions)
            }

        typesafe = FakeTypeSafe(answer_notify)
        engine = HeartbeatEngine([high_use_case, low_use_case], typesafe=typesafe)
        baseline = engine.tick(context(), previous_state=None)
        first = engine.tick(context(), previous_state=baseline.state)
        second = engine.tick(context(), previous_state=first.state)

        assert first.candidate is not None
        self.assertEqual(observation_ids(first), [("beta", "high"), ("alpha", "low")])
        self.assertIsNone(second.candidate)
        self.assertEqual(len(typesafe.calls), 1)

    def test_delivered_observations_are_not_queued_as_pending(self) -> None:
        low = choice_signal("low", priority=20)
        high = choice_signal("high", priority=80)
        low_use_case = FakeUseCase(
            "alpha",
            [Snapshot(), Snapshot(signals=(low,)), Snapshot()],
        )
        high_use_case = FakeUseCase(
            "beta",
            [Snapshot(), Snapshot(signals=(high,)), Snapshot(signals=(high,))],
        )

        def answer_notify(questions: Any) -> dict[str, Any]:
            return {
                question_id: choice_answer("notify")
                for question_id, _question in question_items(questions)
            }

        typesafe = FakeTypeSafe(answer_notify)
        engine = HeartbeatEngine([high_use_case, low_use_case], typesafe=typesafe)
        baseline = engine.tick(context(), previous_state=None)
        first = engine.tick(context(), previous_state=baseline.state)
        second = engine.tick(context(), previous_state=first.state)

        self.assertEqual(first.state["pending"], {})
        self.assertNotIn("alpha:low", first.state["pending"])
        self.assertNotIn("alpha:low", second.state["pending"])
        self.assertEqual(len(typesafe.calls), 1)
