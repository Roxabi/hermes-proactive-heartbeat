"""Heartbeat tick engine: collect, gate, batch TypeSafe, select, persist."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

try:
    from . import _bootstrap  # noqa: F401
except ImportError:  # flat plugin-dir / unittest load
    import _bootstrap  # noqa: F401
from models import (
    ActionSpec,
    Candidate,
    HeartbeatUseCase,
    JsonObject,
    JudgmentSpec,
    Signal,
    Snapshot,
    TickContext,
)

STATE_VERSION = 2
_MAX_FACT_KEYS = 32
_MAX_FACT_DEPTH = 4
_MAX_FACT_STRING = 500
_MAX_FACT_LIST = 32
_INITIAL_OBSERVATIONS = frozenset({"baseline", "eligible"})


class SupportsEvaluate(Protocol):
    def evaluate(
        self,
        state: dict[str, Any],
        questions: Any,
    ) -> dict[str, Any] | None: ...


@dataclass(frozen=True)
class TickResult:
    """Immutable outcome of one heartbeat tick."""

    candidate: Candidate | None
    state: JsonObject
    diagnostics: JsonObject

    def render(self) -> str:
        """Return the exact one-line stdout contract for Hermes Cron."""

        if self.candidate is None:
            return '{"wakeAgent": false}'
        payload = {"heartbeat_candidate": self.candidate.as_json()}
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class HeartbeatEngine:
    """Run one deterministic heartbeat tick across registered use cases."""

    use_cases: tuple[HeartbeatUseCase, ...]
    typesafe_client: SupportsEvaluate | None = None

    def __init__(
        self,
        use_cases: Iterable[HeartbeatUseCase],
        typesafe_client: SupportsEvaluate | None = None,
        *,
        typesafe: SupportsEvaluate | None = None,
    ) -> None:
        object.__setattr__(
            self,
            "use_cases",
            tuple(sorted(use_cases, key=lambda use_case: use_case.id)),
        )
        client = typesafe_client if typesafe_client is not None else typesafe
        object.__setattr__(self, "typesafe_client", client)

    def tick(
        self,
        context: TickContext,
        previous: Mapping[str, Any] | None = None,
        *,
        previous_state: Mapping[str, Any] | None = None,
    ) -> TickResult:
        """Collect snapshots, gate due signals, optionally judge, and persist next state."""

        prior = _coerce_previous(previous_state if previous_state is not None else previous)
        is_baseline = prior is None
        previous_root = prior or _empty_state()
        previous_pending = _coerce_pending(previous_root.get("pending"))

        diagnostics: JsonObject = {}
        next_use_cases: dict[str, JsonObject] = {}
        due: list[_DueSignal] = []
        retained_pending: dict[str, JsonObject] = {}
        signal_index: dict[str, Signal] = {}

        for use_case in self.use_cases:
            previous_entry = _use_case_entry(previous_root, use_case.id)
            try:
                snapshot = use_case.collect(context, dict(previous_entry.get("state") or {}))
            except Exception as exc:  # noqa: BLE001 - isolate per use case
                diagnostics[use_case.id] = {
                    "error": exc.__class__.__name__,
                    "message": str(exc),
                }
                next_use_cases[use_case.id] = {
                    "state": dict(previous_entry.get("state") or {}),
                    "active": list(previous_entry.get("active") or []),
                }
                if isinstance(previous_entry.get("diagnostics"), dict):
                    next_use_cases[use_case.id]["diagnostics"] = dict(previous_entry["diagnostics"])
                retained_pending.update(
                    {
                        key: dict(record)
                        for key, record in previous_pending.items()
                        if key.startswith(f"{use_case.id}:")
                    }
                )
                continue

            if not isinstance(snapshot, Snapshot):
                diagnostics[use_case.id] = {
                    "error": "TypeError",
                    "message": "collect() must return Snapshot",
                }
                next_use_cases[use_case.id] = {
                    "state": dict(previous_entry.get("state") or {}),
                    "active": list(previous_entry.get("active") or []),
                }
                retained_pending.update(
                    {
                        key: dict(record)
                        for key, record in previous_pending.items()
                        if key.startswith(f"{use_case.id}:")
                    }
                )
                continue

            invalid = _first_invalid_signal(snapshot.signals)
            if invalid is not None:
                diagnostics[use_case.id] = {
                    "error": "TypeError",
                    "message": invalid,
                }
                next_use_cases[use_case.id] = {
                    "state": dict(previous_entry.get("state") or {}),
                    "active": list(previous_entry.get("active") or []),
                }
                retained_pending.update(
                    {
                        key: dict(record)
                        for key, record in previous_pending.items()
                        if key.startswith(f"{use_case.id}:")
                    }
                )
                continue

            active_fingerprints = [signal.fingerprint for signal in snapshot.signals]
            next_entry: JsonObject = {
                "state": dict(snapshot.state or {}),
                "active": list(active_fingerprints),
            }
            if snapshot.diagnostics:
                # Soft collector notes stay in use-case state only — they must not fail the tick.
                next_entry["diagnostics"] = _bound_facts(snapshot.diagnostics)
            next_use_cases[use_case.id] = next_entry

            previous_active = {
                str(item) for item in (previous_entry.get("active") or []) if item is not None
            }
            for signal in snapshot.signals:
                key = _delivery_key(use_case.id, signal.fingerprint)
                signal_index[key] = signal
                if is_baseline and signal.initial_observation != "eligible":
                    continue
                if (not is_baseline) and not _is_due(
                    use_case_id=use_case.id,
                    signal=signal,
                    previous_active=previous_active,
                    delivered=previous_root.get("delivered") or {},
                    now=context.now,
                    default_cooldown=_default_cooldown(context.settings),
                    pending=previous_pending,
                ):
                    continue
                due.append(
                    _DueSignal(
                        use_case_id=use_case.id,
                        signal=signal,
                        question_id=_question_id(use_case.id, signal.fingerprint),
                    )
                )

        delivered = dict(previous_root.get("delivered") or {})
        if is_baseline:
            for use_case_id, entry in next_use_cases.items():
                if use_case_id in diagnostics:
                    continue
                for fingerprint in entry.get("active") or []:
                    if not isinstance(fingerprint, str):
                        continue
                    key = _delivery_key(use_case_id, fingerprint)
                    signal = signal_index.get(key)
                    if signal is None or signal.initial_observation == "eligible":
                        continue
                    delivered[key] = {
                        "at": _iso(context.now),
                        "action": "baseline",
                    }

        if diagnostics:
            # Hard collector failures: fail closed — no wake, queue due signals for retry.
            queued = dict(retained_pending)
            for item in due:
                key = _delivery_key(item.use_case_id, item.signal.fingerprint)
                queued[key] = {
                    "queued_at": _iso(context.now),
                }
            return TickResult(
                candidate=None,
                state={
                    "version": STATE_VERSION,
                    "use_cases": next_use_cases,
                    "delivered": delivered,
                    "pending": _sorted_pending(queued),
                },
                diagnostics=diagnostics,
            )

        answers, judge_ids = self._evaluate_due(context, due, previous_pending)
        candidates = [
            candidate
            for item in due
            for candidate in [
                _resolve_candidate(
                    item,
                    answers,
                    context=context,
                    threshold=_threshold(context.settings),
                    previous_pending=previous_pending,
                    judge_ids=judge_ids,
                )
            ]
            if candidate is not None
        ]
        winner = _select_winner(candidates)

        if winner is not None:
            delivered[_delivery_key(winner.collector, winner.fingerprint)] = {
                "at": _iso(context.now),
                "action": winner.action.name,
            }

        winner_key = (
            _delivery_key(winner.collector, winner.fingerprint) if winner is not None else None
        )
        candidate_keys = {
            _delivery_key(candidate.collector, candidate.fingerprint) for candidate in candidates
        }
        for item in due:
            key = _delivery_key(item.use_case_id, item.signal.fingerprint)
            if key == winner_key or key in candidate_keys:
                continue
            # Silent / unresolved due signals get a stamp so cooldown applies.
            delivered[key] = {
                "at": _iso(context.now),
                "action": "silent",
            }

        next_pending = dict(retained_pending)
        for candidate in candidates:
            key = _delivery_key(candidate.collector, candidate.fingerprint)
            if key == winner_key:
                continue
            next_pending[key] = {
                "action": _action_as_json(candidate.action),
                "decision": dict(candidate.decision),
                "facts_digest": _facts_digest(candidate.facts),
                "queued_at": _iso(context.now),
            }

        return TickResult(
            candidate=winner,
            state={
                "version": STATE_VERSION,
                "use_cases": next_use_cases,
                "delivered": delivered,
                "pending": _sorted_pending(next_pending),
            },
            diagnostics=diagnostics,
        )

    def _evaluate_due(
        self,
        context: TickContext,
        due: list[_DueSignal],
        previous_pending: Mapping[str, JsonObject],
    ) -> tuple[dict[str, Any] | None, set[str]]:
        questions: dict[str, dict[str, Any]] = {}
        judge_ids: set[str] = set()
        for item in due:
            decision = item.signal.decision
            if not isinstance(decision, JudgmentSpec):
                continue
            key = _delivery_key(item.use_case_id, item.signal.fingerprint)
            pending_record = previous_pending.get(key)
            if _pending_reusable(pending_record, item.signal.facts):
                continue
            questions[item.question_id] = dict(decision.question)
            judge_ids.add(item.question_id)

        if self.typesafe_client is None or not questions:
            return None, judge_ids

        state = {
            "now": _iso(context.now),
            "signals": {
                item.question_id: {
                    "use_case": item.use_case_id,
                    "fingerprint": item.signal.fingerprint,
                    "facts": _bound_facts(item.signal.facts),
                }
                for item in due
                if item.question_id in judge_ids
            },
        }
        try:
            return self.typesafe_client.evaluate(state, questions), judge_ids
        except Exception:  # noqa: BLE001 - treat client failures as unavailable
            return None, judge_ids


@dataclass(frozen=True)
class _DueSignal:
    use_case_id: str
    signal: Signal
    question_id: str


def _coerce_previous(previous: Mapping[str, Any] | None) -> JsonObject | None:
    if previous is None:
        return None
    if not isinstance(previous, Mapping):
        return None
    version = previous.get("version")
    if version is None:
        return None
    try:
        if int(version) != STATE_VERSION:
            return None
    except (TypeError, ValueError):
        return None
    return dict(previous)


def _empty_state() -> JsonObject:
    return {"version": STATE_VERSION, "use_cases": {}, "delivered": {}, "pending": {}}


def _use_case_entry(state: Mapping[str, Any], use_case_id: str) -> JsonObject:
    use_cases = state.get("use_cases") or {}
    if not isinstance(use_cases, Mapping):
        return {"state": {}, "active": []}
    entry = use_cases.get(use_case_id) or {}
    if not isinstance(entry, Mapping):
        return {"state": {}, "active": []}
    result: JsonObject = {
        "state": dict(entry.get("state") or {}) if isinstance(entry.get("state"), Mapping) else {},
        "active": list(entry.get("active") or []) if isinstance(entry.get("active"), list) else [],
    }
    if isinstance(entry.get("diagnostics"), Mapping):
        result["diagnostics"] = dict(entry["diagnostics"])
    return result


def _default_cooldown(settings: Mapping[str, Any]) -> int:
    value = settings.get("default_cooldown_seconds", 14_400)
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 14_400


def _threshold(settings: Mapping[str, Any]) -> float:
    value = settings.get("typesafe_threshold", 0.65)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.65


def _question_id(use_case_id: str, fingerprint: str) -> str:
    return f"{use_case_id}:{fingerprint}"


def _delivery_key(use_case_id: str, fingerprint: str) -> str:
    return f"{use_case_id}:{fingerprint}"


def _is_due(
    *,
    use_case_id: str,
    signal: Signal,
    previous_active: set[str],
    delivered: Mapping[str, Any],
    pending: Mapping[str, Any],
    now: datetime,
    default_cooldown: int,
) -> bool:
    fingerprint = signal.fingerprint
    delivery_key = _delivery_key(use_case_id, fingerprint)
    if delivery_key in pending:
        return True
    if fingerprint not in previous_active:
        return True

    record = delivered.get(delivery_key)
    if not isinstance(record, Mapping):
        # Active but never delivered: stay eligible instead of silently suppressing forever.
        return True
    delivered_at = _parse_time(record.get("at"))
    if delivered_at is None:
        return True

    cooldown = (
        signal.repeat_after_seconds if signal.repeat_after_seconds is not None else default_cooldown
    )
    try:
        cooldown_seconds = max(0, int(cooldown))
    except (TypeError, ValueError):
        cooldown_seconds = default_cooldown

    elapsed = (now - delivered_at).total_seconds()
    return elapsed >= cooldown_seconds


def _resolve_candidate(
    item: _DueSignal,
    answers: Mapping[str, Any] | None,
    *,
    context: TickContext,
    threshold: float,
    previous_pending: Mapping[str, JsonObject],
    judge_ids: set[str],
) -> Candidate | None:
    decision = item.signal.decision
    facts = _bound_facts(item.signal.facts)
    candidate_context = _candidate_context(context)
    key = _delivery_key(item.use_case_id, item.signal.fingerprint)

    if isinstance(decision, ActionSpec):
        if not decision.wake_agent:
            return None
        return Candidate(
            collector=item.use_case_id,
            fingerprint=item.signal.fingerprint,
            action=decision,
            facts=facts,
            context=candidate_context,
            decision={"action": decision.name, "source": "rule"},
        )

    pending_record = previous_pending.get(key)
    if item.question_id not in judge_ids and _pending_reusable(pending_record, facts):
        action = _action_from_json(pending_record.get("action") if pending_record else None)
        decision_meta = (
            dict(pending_record.get("decision") or {})
            if isinstance(pending_record, Mapping)
            and isinstance(pending_record.get("decision"), Mapping)
            else {}
        )
        if action is None or not action.wake_agent:
            return None
        return Candidate(
            collector=item.use_case_id,
            fingerprint=item.signal.fingerprint,
            action=action,
            facts=facts,
            context=candidate_context,
            decision=decision_meta or {"action": action.name, "source": "fallback"},
        )

    raw_answer = answers.get(item.question_id) if isinstance(answers, Mapping) else None
    label = _label_for_answer(
        raw_answer,
        judgment=decision,
        threshold=threshold,
    )
    source = "typesafe" if label is not None else "fallback"
    if label is None:
        label = decision.fallback_label

    action = decision.actions.get(label)
    if action is None:
        action = decision.actions.get(decision.fallback_label)
        source = "fallback"
    if action is None or not action.wake_agent:
        return None

    return Candidate(
        collector=item.use_case_id,
        fingerprint=item.signal.fingerprint,
        action=action,
        facts=facts,
        context=candidate_context,
        decision=_decision_meta(label=label, action=action, answer=raw_answer, source=source),
    )


def _candidate_context(context: TickContext) -> JsonObject:
    extra = context.settings.get("context")
    merged: JsonObject = {}
    if isinstance(extra, Mapping):
        merged.update({key: value for key, value in extra.items() if value is not None})
    name = context.settings.get("name")
    if isinstance(name, str) and name:
        merged["heartbeat"] = name
    merged["now"] = _iso(context.now)
    return _bound_facts(merged)


def _decision_meta(
    *,
    label: str,
    action: ActionSpec,
    answer: Any,
    source: str,
) -> JsonObject:
    meta: JsonObject = {
        "label": label,
        "action": action.name,
        "source": source,
    }
    if isinstance(answer, Mapping):
        for key in ("type", "choice", "noul"):
            if key in answer:
                meta[key] = answer[key]
        probs = answer.get("probabilities")
        if isinstance(probs, Mapping):
            meta["probabilities"] = _bound_facts(probs)
    return meta


def _label_for_answer(
    answer: Any,
    *,
    judgment: JudgmentSpec,
    threshold: float,
) -> str | None:
    if answer is None:
        return None

    if isinstance(answer, Mapping):
        answer_type = answer.get("type")
        if answer_type == "choice" or "choice" in answer:
            choice = answer.get("choice")
            return choice if isinstance(choice, str) else None
        if answer_type == "noul" or "noul" in answer:
            noul = answer.get("noul")
            if isinstance(noul, bool) or not isinstance(noul, (int, float)):
                return None
            return _noul_label(float(noul), judgment=judgment, threshold=threshold)
        return None

    if isinstance(answer, str):
        return answer

    if isinstance(answer, bool):
        return None

    if isinstance(answer, (int, float)):
        return _noul_label(float(answer), judgment=judgment, threshold=threshold)

    return None


def _noul_label(probability: float, *, judgment: JudgmentSpec, threshold: float) -> str:
    if probability >= threshold:
        for key in ("include", "true", "notify", "yes"):
            if key in judgment.actions:
                return key
        for key, action in judgment.actions.items():
            if action.wake_agent and key != judgment.fallback_label:
                return key
    return judgment.fallback_label


def _select_winner(candidates: list[Candidate]) -> Candidate | None:
    if not candidates:
        return None
    return sorted(
        candidates,
        key=lambda candidate: (
            -candidate.action.priority,
            candidate.collector,
            candidate.fingerprint,
        ),
    )[0]


def _first_invalid_signal(signals: tuple[Signal, ...]) -> str | None:
    for signal in signals:
        if not isinstance(signal, Signal):
            return "snapshot signals must contain Signal values"
        if not isinstance(signal.fingerprint, str) or not signal.fingerprint:
            return "signal fingerprint must be a non-empty string"
        if not isinstance(signal.facts, Mapping):
            return "signal facts must be a mapping"
        if not isinstance(signal.decision, (ActionSpec, JudgmentSpec)):
            return "signal decision must be ActionSpec or JudgmentSpec"
        if signal.initial_observation not in _INITIAL_OBSERVATIONS:
            return "signal initial_observation must be 'baseline' or 'eligible'"
        if isinstance(signal.decision, JudgmentSpec):
            if not isinstance(signal.decision.question, Mapping):
                return "JudgmentSpec.question must be a mapping"
            if not isinstance(signal.decision.actions, Mapping) or not signal.decision.actions:
                return "JudgmentSpec.actions must be a non-empty mapping"
            if not all(
                isinstance(action, ActionSpec) for action in signal.decision.actions.values()
            ):
                return "JudgmentSpec.actions values must be ActionSpec"
            if (
                not isinstance(signal.decision.fallback_label, str)
                or not signal.decision.fallback_label
            ):
                return "JudgmentSpec.fallback_label must be a non-empty string"
    return None


def _coerce_pending(value: Any) -> dict[str, JsonObject]:
    if not isinstance(value, Mapping):
        return {}
    pending: dict[str, JsonObject] = {}
    for key, record in value.items():
        if not isinstance(key, str) or not isinstance(record, Mapping):
            continue
        pending[key] = dict(record)
    return pending


def _sorted_pending(pending: Mapping[str, JsonObject]) -> JsonObject:
    return {key: dict(pending[key]) for key in sorted(pending)}


def _pending_reusable(record: Mapping[str, Any] | None, facts: Mapping[str, Any]) -> bool:
    if not isinstance(record, Mapping):
        return False
    if not isinstance(record.get("action"), Mapping):
        return False
    if not isinstance(record.get("decision"), Mapping):
        return False
    digest = record.get("facts_digest")
    return isinstance(digest, str) and digest == _facts_digest(facts)


def _action_as_json(action: ActionSpec) -> JsonObject:
    return {
        "name": action.name,
        "wake_agent": action.wake_agent,
        "priority": action.priority,
        "instruction": action.instruction,
        "max_sentences": action.max_sentences,
    }


def _action_from_json(value: Any) -> ActionSpec | None:
    if not isinstance(value, Mapping):
        return None
    name = value.get("name")
    wake_agent = value.get("wake_agent")
    priority = value.get("priority")
    instruction = value.get("instruction", "")
    max_sentences = value.get("max_sentences", 0)
    if not isinstance(name, str) or not name:
        return None
    if not isinstance(wake_agent, bool):
        return None
    try:
        priority_value = int(priority)
    except (TypeError, ValueError):
        return None
    if not isinstance(instruction, str):
        instruction = str(instruction)
    try:
        max_sentences_value = int(max_sentences)
    except (TypeError, ValueError):
        max_sentences_value = 0
    return ActionSpec(
        name=name,
        wake_agent=wake_agent,
        priority=priority_value,
        instruction=instruction,
        max_sentences=max_sentences_value,
    )


def _facts_digest(facts: Mapping[str, Any] | None) -> str:
    payload = json.dumps(
        _bound_facts(facts),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _bound_facts(facts: Mapping[str, Any] | None) -> JsonObject:
    if not isinstance(facts, Mapping):
        return {}
    bounded = {
        str(key): _bound_value(value, depth=0)
        for key, value in list(facts.items())[:_MAX_FACT_KEYS]
    }
    return bounded if isinstance(bounded, dict) else {}


def _bound_value(value: Any, *, depth: int) -> Any:
    if depth >= _MAX_FACT_DEPTH:
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:_MAX_FACT_STRING]
    if isinstance(value, Mapping):
        return {
            str(key): _bound_value(item, depth=depth + 1)
            for key, item in list(value.items())[:_MAX_FACT_KEYS]
        }
    if isinstance(value, (list, tuple)):
        return [_bound_value(item, depth=depth + 1) for item in list(value)[:_MAX_FACT_LIST]]
    return str(value)[:_MAX_FACT_STRING]


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
