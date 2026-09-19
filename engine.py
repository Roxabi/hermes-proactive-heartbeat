"""Heartbeat tick engine: collect, gate, batch TypeSafe, pack, persist."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

try:
    from . import _bootstrap  # noqa: F401
except ImportError:  # flat plugin-dir / unittest load
    import _bootstrap  # noqa: F401
from models import Candidate, HeartbeatUseCase, JsonObject, TickContext
from tick_collect import collect_use_cases, stamp_baseline
from tick_decide import evaluate_due, resolve_candidate
from tick_facts import iso
from tick_pack import pack_wake
from tick_state import (
    STATE_VERSION,
    coerce_pending,
    coerce_previous,
    delivery_key,
    empty_state,
    retained_delivered,
    sorted_pending,
    threshold,
)


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

        prior = coerce_previous(previous_state if previous_state is not None else previous)
        is_baseline = prior is None
        previous_root = prior or empty_state()
        previous_pending = coerce_pending(previous_root.get("pending"))
        collected = collect_use_cases(
            self.use_cases,
            context,
            previous_root,
            previous_pending,
            is_baseline,
        )
        delivered = dict(previous_root.get("delivered") or {})
        if is_baseline:
            stamp_baseline(delivered, collected, context.now)
        if collected.diagnostics:
            return _fail_closed(collected, delivered, context)
        answers, judge_ids = evaluate_due(
            self.typesafe_client,
            context,
            collected.due,
            previous_pending,
        )
        return _settle(collected, delivered, answers, judge_ids, context, previous_pending)


def _fail_closed(collected: Any, delivered: dict[str, Any], context: TickContext) -> TickResult:
    # Hard collector failures: fail closed — no wake, queue due signals for retry.
    queued = dict(collected.retained_pending)
    for item in collected.due:
        queued[delivery_key(item.use_case_id, item.signal.fingerprint)] = {
            "queued_at": iso(context.now),
        }
    return TickResult(
        candidate=None,
        state={
            "version": STATE_VERSION,
            "use_cases": collected.next_use_cases,
            "delivered": retained_delivered(
                delivered,
                use_cases=collected.next_use_cases,
                diagnostics=collected.diagnostics,
            ),
            "pending": sorted_pending(queued),
        },
        diagnostics=collected.diagnostics,
    )


def _settle(
    collected: Any,
    delivered: dict[str, Any],
    answers: Mapping[str, Any] | None,
    judge_ids: set[str],
    context: TickContext,
    previous_pending: Mapping[str, JsonObject],
) -> TickResult:
    candidates = [
        candidate
        for item in collected.due
        for candidate in [
            resolve_candidate(
                item,
                answers,
                context=context,
                threshold=threshold(context.settings),
                previous_pending=previous_pending,
                judge_ids=judge_ids,
            )
        ]
        if candidate is not None
    ]
    packed = pack_wake(candidates, context)
    delivered_keys = {
        delivery_key(candidate.collector, candidate.fingerprint) for candidate in candidates
    }
    for candidate in candidates:
        delivered[delivery_key(candidate.collector, candidate.fingerprint)] = {
            "at": iso(context.now),
            "action": candidate.action.name,
        }

    for item in collected.due:
        key = delivery_key(item.use_case_id, item.signal.fingerprint)
        if key in delivered_keys:
            continue
        delivered[key] = {"at": iso(context.now), "action": "silent"}

    return TickResult(
        candidate=packed,
        state={
            "version": STATE_VERSION,
            "use_cases": collected.next_use_cases,
            "delivered": retained_delivered(
                delivered,
                use_cases=collected.next_use_cases,
                diagnostics=collected.diagnostics,
            ),
            "pending": sorted_pending(collected.retained_pending),
        },
        diagnostics=collected.diagnostics,
    )
