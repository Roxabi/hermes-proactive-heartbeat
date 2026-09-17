"""Heartbeat tick engine: collect, gate, batch TypeSafe, select, persist."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

try:
    from . import _bootstrap  # noqa: F401
except ImportError:  # flat plugin-dir / unittest load
    import _bootstrap  # noqa: F401
from models import Candidate, HeartbeatUseCase, JsonObject, Signal, Snapshot, TickContext

STATE_VERSION = 1
_MAX_FACT_KEYS = 32
_MAX_FACT_DEPTH = 4
_MAX_FACT_STRING = 500
_MAX_FACT_LIST = 32


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
        previous_pending = {
            str(item) for item in (previous_root.get("pending") or []) if isinstance(item, str)
        }

        diagnostics: JsonObject = {}
        next_use_cases: dict[str, JsonObject] = {}
        due: list[_DueSignal] = []
        retained_pending: set[str] = set()

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
                    key for key in previous_pending if key.startswith(f"{use_case.id}:")
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
                    key for key in previous_pending if key.startswith(f"{use_case.id}:")
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

            if is_baseline:
                continue

            previous_active = {
                str(item) for item in (previous_entry.get("active") or []) if item is not None
            }
            for signal in snapshot.signals:
                if _is_due(
                    use_case_id=use_case.id,
                    signal=signal,
                    previous_active=previous_active,
                    delivered=previous_root.get("delivered") or {},
                    now=context.now,
                    default_cooldown=_default_cooldown(context.settings),
                    pending=previous_pending,
                ):
                    due.append(
                        _DueSignal(
                            use_case_id=use_case.id,
                            signal=signal,
                            question_id=_question_id(use_case.id, signal.fingerprint),
                        )
                    )

        delivered = dict(previous_root.get("delivered") or {})
        if is_baseline:
            # Stamp baseline observations so cooldown can expire and re-evaluate later.
            for use_case_id, entry in next_use_cases.items():
                for fingerprint in entry.get("active") or []:
                    if not isinstance(fingerprint, str):
                        continue
                    delivered[_delivery_key(use_case_id, fingerprint)] = {
                        "at": _iso(context.now),
                        "action": "baseline",
                    }
            return TickResult(
                candidate=None,
                state={
                    "version": STATE_VERSION,
                    "use_cases": next_use_cases,
                    "delivered": delivered,
                    "pending": sorted(retained_pending),
                },
                diagnostics=diagnostics,
            )

        if diagnostics:
            # Hard collector failures: fail closed — no wake, queue due signals for retry.
            queued = retained_pending | {
                _delivery_key(item.use_case_id, item.signal.fingerprint) for item in due
            }
            return TickResult(
                candidate=None,
                state={
                    "version": STATE_VERSION,
                    "use_cases": next_use_cases,
                    "delivered": delivered,
                    "pending": sorted(queued),
                },
                diagnostics=diagnostics,
            )

        answers = self._evaluate_due(context, due) if due else None
        candidates = [
            candidate
            for item in due
            for candidate in [
                _resolve_candidate(
                    item,
                    answers,
                    context=context,
                    threshold=_threshold(context.settings),
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

        next_pending = retained_pending | {key for key in candidate_keys if key != winner_key}
        return TickResult(
            candidate=winner,
            state={
                "version": STATE_VERSION,
                "use_cases": next_use_cases,
                "delivered": delivered,
                "pending": sorted(next_pending),
            },
            diagnostics=diagnostics,
        )

    def _evaluate_due(
        self,
        context: TickContext,
        due: list[_DueSignal],
    ) -> dict[str, Any] | None:
        if self.typesafe_client is None or not due:
            return None

        questions = {item.question_id: dict(item.signal.judgment.question) for item in due}
        state = {
            "now": _iso(context.now),
            "signals": {
                item.question_id: {
                    "use_case": item.use_case_id,
                    "fingerprint": item.signal.fingerprint,
                    "facts": _bound_facts(item.signal.facts),
                }
                for item in due
            },
        }
        try:
            return self.typesafe_client.evaluate(state, questions)
        except Exception:  # noqa: BLE001 - treat client failures as unavailable
            return None


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
    return {"version": STATE_VERSION, "use_cases": {}, "delivered": {}, "pending": []}


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
    pending: set[str],
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
) -> Candidate | None:
    judgment = item.signal.judgment
    raw_answer = answers.get(item.question_id) if isinstance(answers, Mapping) else None
    label = _label_for_answer(
        raw_answer,
        judgment=judgment,
        threshold=threshold,
    )
    if label is None:
        label = judgment.fallback_label

    action = judgment.actions.get(label)
    if action is None:
        action = judgment.actions.get(judgment.fallback_label)
    if action is None:
        return None
    if action.name == "silent" or label == "silent":
        return None

    return Candidate(
        collector=item.use_case_id,
        fingerprint=item.signal.fingerprint,
        action=action,
        facts=_bound_facts(item.signal.facts),
        context=_candidate_context(context),
        judgment=_judgment_meta(label=label, action=action, answer=raw_answer),
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


def _judgment_meta(*, label: str, action: Any, answer: Any) -> JsonObject:
    meta: JsonObject = {
        "label": label,
        "action": action.name,
        "source": "typesafe" if answer is not None else "fallback",
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
    judgment: Any,
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


def _noul_label(probability: float, *, judgment: Any, threshold: float) -> str:
    if probability >= threshold:
        for key in ("include", "true", "notify", "yes"):
            if key in judgment.actions:
                return key
        for key, action in judgment.actions.items():
            if action.name != "silent" and key != judgment.fallback_label:
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


def _bound_facts(facts: Mapping[str, Any] | None) -> JsonObject:
    if not isinstance(facts, Mapping):
        return {}
    bounded = _bound_value(dict(facts), depth=0)
    return bounded if isinstance(bounded, dict) else {}


def _bound_value(value: Any, *, depth: int) -> Any:
    if depth >= _MAX_FACT_DEPTH:
        return None
    if isinstance(value, Mapping):
        items = list(value.items())[:_MAX_FACT_KEYS]
        return {
            str(key)[:_MAX_FACT_STRING]: _bound_value(item, depth=depth + 1) for key, item in items
        }
    if isinstance(value, list):
        return [_bound_value(item, depth=depth + 1) for item in value[:_MAX_FACT_LIST]]
    if isinstance(value, tuple):
        return [_bound_value(item, depth=depth + 1) for item in value[:_MAX_FACT_LIST]]
    if isinstance(value, str):
        return value if len(value) <= _MAX_FACT_STRING else value[:_MAX_FACT_STRING]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:_MAX_FACT_STRING]


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
