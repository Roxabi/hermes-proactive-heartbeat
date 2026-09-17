"""Stable contracts between heartbeat use cases and the engine."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

JsonObject = dict[str, Any]


@dataclass(frozen=True)
class ActionSpec:
    """Trusted delivery behavior selected by code or a semantic judgment."""

    name: str
    priority: int
    instruction: str
    max_sentences: int = 1


@dataclass(frozen=True)
class JudgmentSpec:
    """One TypeSafe question and its answer-label-to-action mapping."""

    question: JsonObject
    actions: Mapping[str, ActionSpec]
    fallback_label: str = "silent"


@dataclass(frozen=True)
class Signal:
    """One currently active condition emitted by a use case."""

    fingerprint: str
    facts: JsonObject
    judgment: JudgmentSpec
    repeat_after_seconds: int | None = None


@dataclass(frozen=True)
class Snapshot:
    """A use case's full current state for edge detection and persistence."""

    signals: tuple[Signal, ...] = ()
    state: JsonObject = field(default_factory=dict)
    diagnostics: JsonObject = field(default_factory=dict)


@dataclass(frozen=True)
class Candidate:
    """A resolved signal eligible for one Hermes agent wake."""

    use_case: str
    fingerprint: str
    action: ActionSpec
    facts: JsonObject

    def as_json(self) -> JsonObject:
        return {
            "use_case": self.use_case,
            "fingerprint": self.fingerprint,
            "action": self.action.name,
            "priority": self.action.priority,
            "facts": self.facts,
            "delivery": {
                "instruction": self.action.instruction,
                "max_sentences": self.action.max_sentences,
            },
        }


@dataclass(frozen=True)
class TickContext:
    """Read-only context shared by every use case during one tick."""

    now: datetime
    settings: JsonObject


class HeartbeatUseCase(Protocol):
    """Deep use-case seam: collect a complete snapshot, nothing else."""

    id: str

    def collect(self, context: TickContext, previous_state: JsonObject) -> Snapshot: ...
