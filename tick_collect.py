"""Collect snapshots, validate signals, and assemble the due set."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

try:
    from . import _bootstrap  # noqa: F401
except ImportError:  # flat plugin-dir / unittest load
    import _bootstrap  # noqa: F401
from models import (
    ActionSpec,
    HeartbeatUseCase,
    JsonObject,
    JudgmentSpec,
    Signal,
    Snapshot,
    TickContext,
)
from tick_decide import DueSignal, is_due
from tick_facts import bound_facts, iso
from tick_state import default_cooldown, delivered_view, delivery_key, use_case_entry

_INITIAL_OBSERVATIONS = frozenset({"baseline", "eligible"})


@dataclass
class CollectPass:
    diagnostics: JsonObject
    next_use_cases: dict[str, JsonObject]
    due: list[DueSignal]
    retained_pending: dict[str, JsonObject]
    signal_index: dict[str, Signal]


def collect_use_cases(
    use_cases: Iterable[HeartbeatUseCase],
    context: TickContext,
    previous_root: Mapping[str, Any],
    previous_pending: Mapping[str, JsonObject],
    is_baseline: bool,
) -> CollectPass:
    diagnostics: JsonObject = {}
    next_use_cases: dict[str, JsonObject] = {}
    due: list[DueSignal] = []
    retained_pending: dict[str, JsonObject] = {}
    signal_index: dict[str, Signal] = {}

    for use_case in use_cases:
        previous_entry = use_case_entry(previous_root, use_case.id)
        collector_context = replace(
            context,
            delivered=delivered_view(previous_root.get("delivered"), use_case.id),
        )
        try:
            snapshot = use_case.collect(
                collector_context,
                dict(previous_entry.get("state") or {}),
            )
        except Exception as exc:  # noqa: BLE001 - isolate per use case
            _retain_failed(
                use_case.id,
                previous_entry,
                previous_pending,
                diagnostics,
                next_use_cases,
                retained_pending,
                error=exc.__class__.__name__,
                message=str(exc),
            )
            continue

        if not isinstance(snapshot, Snapshot):
            _retain_failed(
                use_case.id,
                previous_entry,
                previous_pending,
                diagnostics,
                next_use_cases,
                retained_pending,
                error="TypeError",
                message="collect() must return Snapshot",
            )
            continue

        invalid = first_invalid_signal(snapshot.signals)
        if invalid is not None:
            _retain_failed(
                use_case.id,
                previous_entry,
                previous_pending,
                diagnostics,
                next_use_cases,
                retained_pending,
                error="TypeError",
                message=invalid,
            )
            continue

        active_fingerprints = [signal.fingerprint for signal in snapshot.signals]
        next_entry: JsonObject = {
            "state": dict(snapshot.state or {}),
            "active": list(active_fingerprints),
        }
        if snapshot.diagnostics:
            # Soft collector notes stay in use-case state only — they must not fail the tick.
            next_entry["diagnostics"] = bound_facts(snapshot.diagnostics)
        next_use_cases[use_case.id] = next_entry

        previous_active = {
            str(item) for item in (previous_entry.get("active") or []) if item is not None
        }
        for signal in snapshot.signals:
            key = delivery_key(use_case.id, signal.fingerprint)
            signal_index[key] = signal
            if is_baseline and signal.initial_observation != "eligible":
                continue
            if (not is_baseline) and not is_due(
                use_case_id=use_case.id,
                signal=signal,
                previous_active=previous_active,
                delivered=previous_root.get("delivered") or {},
                now=context.now,
                default_cooldown=default_cooldown(context.settings),
                pending=previous_pending,
            ):
                continue
            due.append(
                DueSignal(
                    use_case_id=use_case.id,
                    signal=signal,
                    question_id=delivery_key(use_case.id, signal.fingerprint),
                )
            )

    return CollectPass(
        diagnostics=diagnostics,
        next_use_cases=next_use_cases,
        due=due,
        retained_pending=retained_pending,
        signal_index=signal_index,
    )


def stamp_baseline(delivered: dict[str, Any], collected: CollectPass, now: datetime) -> None:
    for use_case_id, entry in collected.next_use_cases.items():
        if use_case_id in collected.diagnostics:
            continue
        for fingerprint in entry.get("active") or []:
            if not isinstance(fingerprint, str):
                continue
            key = delivery_key(use_case_id, fingerprint)
            signal = collected.signal_index.get(key)
            if signal is None or signal.initial_observation == "eligible":
                continue
            delivered[key] = {"at": iso(now), "action": "baseline"}


def first_invalid_signal(signals: tuple[Signal, ...]) -> str | None:
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


def _retain_failed(
    use_case_id: str,
    previous_entry: Mapping[str, Any],
    previous_pending: Mapping[str, JsonObject],
    diagnostics: JsonObject,
    next_use_cases: dict[str, JsonObject],
    retained_pending: dict[str, JsonObject],
    *,
    error: str,
    message: str,
) -> None:
    diagnostics[use_case_id] = {"error": error, "message": message}
    next_use_cases[use_case_id] = {
        "state": dict(previous_entry.get("state") or {}),
        "active": list(previous_entry.get("active") or []),
    }
    if isinstance(previous_entry.get("diagnostics"), dict):
        next_use_cases[use_case_id]["diagnostics"] = dict(previous_entry["diagnostics"])
    retained_pending.update(
        {
            key: dict(record)
            for key, record in previous_pending.items()
            if key.startswith(f"{use_case_id}:")
        }
    )
