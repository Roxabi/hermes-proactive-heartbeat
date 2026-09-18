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
        self.delivered_views: list[dict[str, Any]] = []

    def collect(self, context: TickContext, previous_state: dict[str, Any]) -> Snapshot:
        self.previous_states.append(previous_state)
        self.delivered_views.append(dict(context.delivered))
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


def action(name: str, priority: int, *, wake_agent: bool = True) -> ActionSpec:
    return ActionSpec(
        name=name,
        wake_agent=wake_agent,
        priority=priority,
        instruction=f"Deliver {name}",
        max_sentences=2,
    )


def rule_signal(
    fingerprint: str,
    *,
    priority: int = 10,
    wake_agent: bool = True,
    initial_observation: str = "baseline",
    facts: dict[str, Any] | None = None,
) -> Signal:
    return Signal(
        fingerprint=fingerprint,
        facts=facts or {"fingerprint": fingerprint},
        decision=action(
            "notify" if wake_agent else "record_only",
            priority,
            wake_agent=wake_agent,
        ),
        initial_observation=initial_observation,
    )


def choice_signal(
    fingerprint: str,
    *,
    priority: int = 10,
    fallback_label: str = "silent",
    repeat_after_seconds: int | None = None,
    initial_observation: str = "baseline",
    facts: dict[str, Any] | None = None,
) -> Signal:
    notify = action("notify", priority)
    silent = action("silent", 0, wake_agent=False)
    return Signal(
        fingerprint=fingerprint,
        facts=facts or {"fingerprint": fingerprint},
        decision=JudgmentSpec(
            question={
                "type": "choice",
                "instructions": "Choose whether this signal warrants delivery.",
                "criteria": {"notify": "Deliver", "silent": "Do not deliver"},
            },
            actions={"notify": notify, "silent": silent},
            fallback_label=fallback_label,
        ),
        repeat_after_seconds=repeat_after_seconds,
        initial_observation=initial_observation,
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
        self.assertEqual(result.candidate.collector, "beta")
        self.assertEqual(result.candidate.fingerprint, "high")
        self.assertEqual(result.candidate.action.priority, 80)
        self.assertEqual(result.candidate.decision["source"], "typesafe")

    def test_mixed_rules_and_semantic_signals_share_ranking(self) -> None:
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
        self.assertEqual(result.candidate.collector, "alpha")
        self.assertEqual(result.candidate.fingerprint, "rule")
        self.assertEqual(result.candidate.action.priority, 90)
        self.assertEqual(result.candidate.decision, {"action": "notify", "source": "rule"})
        self.assertIn("beta:semantic", result.state["pending"])

    def test_semantic_loser_is_reused_without_a_second_typesafe_call(self) -> None:
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
        assert second.candidate is not None
        self.assertEqual(first.candidate.collector, "beta")
        self.assertIsInstance(first.state["pending"], dict)
        self.assertIn("alpha:low", first.state["pending"])
        self.assertEqual(second.candidate.collector, "alpha")
        self.assertEqual(second.state["pending"], {})
        self.assertEqual(len(typesafe.calls), 1)

    def test_changed_pending_facts_force_a_fresh_typesafe_call(self) -> None:
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
        assert second.candidate is not None
        self.assertEqual(first.candidate.collector, "beta")
        self.assertEqual(second.candidate.collector, "alpha")
        self.assertEqual(second.candidate.facts, {"sample": 2})
        self.assertEqual(len(typesafe.calls), 2)

    def test_disappeared_signal_is_removed_from_pending(self) -> None:
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

        self.assertIn("alpha:low", first.state["pending"])
        self.assertNotIn("alpha:low", second.state["pending"])
        self.assertEqual(len(typesafe.calls), 1)

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


class TickResultRenderingTests(IsolatedHomeTestCase):
    def test_silent_render_is_exactly_one_compact_gate_line(self) -> None:
        result = TickResult(candidate=None, state={}, diagnostics={})

        rendered = result.render()

        self.assertEqual(rendered, '{"wakeAgent": false}')
        self.assertNotIn("\n", rendered)

    def test_wake_render_is_one_compact_candidate_object_without_gate_line(self) -> None:
        candidate = Candidate(
            collector="sense",
            fingerprint="care:water",
            action=action("water", 70),
            facts={"plant": "fern"},
            decision={"action": "water", "source": "rule"},
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
                    "collector": "sense",
                    "fingerprint": "care:water",
                    "action": "water",
                    "priority": 70,
                    "inputs": {"plant": "fern"},
                    "decision": {
                        "action": "water",
                        "source": "rule",
                    },
                    "context": {},
                    "delivery": {
                        "instruction": "Deliver water",
                        "max_sentences": 2,
                    },
                }
            },
        )
