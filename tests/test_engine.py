from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from engine import HeartbeatEngine, TickResult
from models import ActionSpec, Candidate, JudgmentSpec, Signal, Snapshot, TickContext
from tests.isolation import IsolatedHomeTestCase

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
Answers = dict[str, Any] | Callable[[Any], dict[str, Any]] | None


class FakeUseCase:
    def __init__(self, use_case_id: str, snapshots: list[Snapshot | Exception]) -> None:
        self.id = use_case_id
        self._snapshots = iter(snapshots)
        self.previous_states: list[dict[str, Any]] = []

    def collect(self, context: TickContext, previous_state: dict[str, Any]) -> Snapshot:
        self.previous_states.append(previous_state)
        result = next(self._snapshots)
        if isinstance(result, Exception):
            raise result
        return result


class FakeTypeSafe:
    def __init__(self, answers: Answers) -> None:
        self.answers = answers
        self.calls: list[tuple[dict[str, Any], Any]] = []

    def evaluate(self, state: dict[str, Any], questions: Any) -> dict[str, Any] | None:
        self.calls.append((state, questions))
        if self.answers is None:
            return None
        if callable(self.answers):
            return self.answers(questions)
        return self.answers


def action(name: str, priority: int) -> ActionSpec:
    return ActionSpec(
        name=name,
        priority=priority,
        instruction=f"Deliver {name}",
        max_sentences=2,
    )


def choice_signal(
    fingerprint: str,
    *,
    priority: int = 10,
    fallback_label: str = "silent",
    repeat_after_seconds: int | None = None,
) -> Signal:
    notify = action("notify", priority)
    silent = action("silent", 0)
    return Signal(
        fingerprint=fingerprint,
        facts={"fingerprint": fingerprint},
        judgment=JudgmentSpec(
            question={
                "type": "choice",
                "instructions": "Choose whether this signal warrants delivery.",
                "criteria": {"notify": "Deliver", "silent": "Do not deliver"},
            },
            actions={"notify": notify, "silent": silent},
            fallback_label=fallback_label,
        ),
        repeat_after_seconds=repeat_after_seconds,
    )


def choice_answer(label: str) -> dict[str, Any]:
    return {
        "type": "choice",
        "choice": label,
        "probabilities": {},
        "confidence": 1.0,
    }


def question_items(questions: Any) -> list[tuple[str, dict[str, Any]]]:
    if not isinstance(questions, dict):
        raise AssertionError(f"expected mapping of questions, got {type(questions)!r}")
    return list(questions.items())


def context(*, now: datetime = NOW) -> TickContext:
    return TickContext(
        now=now,
        settings={
            "typesafe_threshold": 0.65,
            "default_cooldown_seconds": 14_400,
        },
    )


class HeartbeatEngineTests(IsolatedHomeTestCase):
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
                "version": 1,
                "use_cases": {
                    "host": {"state": {"sample": 1}, "active": ["disk:root"]},
                },
                "delivered": {},
            },
        )

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
        self.assertEqual(len(typesafe.calls), 1)

    def test_all_due_questions_are_batched_and_highest_priority_candidate_wins(self) -> None:
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
        self.assertEqual(result.candidate.use_case, "beta")
        self.assertEqual(result.candidate.fingerprint, "high")
        self.assertEqual(result.candidate.action.priority, 80)

    def test_failed_collector_does_not_block_other_use_cases(self) -> None:
        failing = FakeUseCase("broken", [RuntimeError("collector offline")])
        healthy = FakeUseCase(
            "healthy",
            [Snapshot(signals=(choice_signal("new", fallback_label="notify"),))],
        )
        previous = {
            "version": 1,
            "use_cases": {
                "broken": {"state": {"cursor": "keep"}, "active": ["old"]},
                "healthy": {"state": {}, "active": []},
            },
            "delivered": {},
        }

        result = HeartbeatEngine(
            [failing, healthy],
            typesafe=FakeTypeSafe(None),
        ).tick(context(), previous_state=previous)

        self.assertIsNotNone(result.candidate)
        assert result.candidate is not None
        self.assertEqual(result.candidate.use_case, "healthy")
        self.assertEqual(
            result.state["use_cases"]["broken"],
            {"state": {"cursor": "keep"}, "active": ["old"]},
        )
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


class TickResultRenderingTests(IsolatedHomeTestCase):
    def test_silent_render_is_exactly_one_compact_gate_line(self) -> None:
        result = TickResult(candidate=None, state={}, diagnostics={})

        rendered = result.render()

        self.assertEqual(rendered, '{"wakeAgent": false}')
        self.assertNotIn("\n", rendered)

    def test_wake_render_is_one_compact_candidate_object_without_gate_line(self) -> None:
        candidate = Candidate(
            use_case="sense",
            fingerprint="care:water",
            action=action("water", 70),
            facts={"plant": "fern"},
        )
        result = TickResult(candidate=candidate, state={}, diagnostics={})

        rendered = result.render()

        self.assertNotIn("\n", rendered)
        self.assertNotIn("wakeAgent", rendered)
        self.assertNotIn(": ", rendered)
        self.assertEqual(
            json.loads(rendered),
            {
                "heartbeat_candidate": {
                    "use_case": "sense",
                    "fingerprint": "care:water",
                    "action": "water",
                    "priority": 70,
                    "facts": {"plant": "fern"},
                    "delivery": {
                        "instruction": "Deliver water",
                        "max_sentences": 2,
                    },
                }
            },
        )
