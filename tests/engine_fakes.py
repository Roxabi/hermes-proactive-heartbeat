from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from models import ActionSpec, JudgmentSpec, Signal, Snapshot, TickContext

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


def wake_inputs(result: Any) -> list[dict[str, Any]]:
    assert result.candidate is not None
    inputs = result.candidate.as_json()["inputs"]
    if not isinstance(inputs, list):
        raise AssertionError("inputs must be a list of observations")
    return inputs


def observation_ids(result: Any) -> list[tuple[str, str]]:
    return [
        (str(item.get("collector")), str(item.get("fingerprint"))) for item in wake_inputs(result)
    ]
