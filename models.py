"""Stable contracts between heartbeat use cases and the engine."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

JsonObject = dict[str, Any]


@dataclass(frozen=True)
class ActionSpec:
    """Trusted delivery behavior selected by code or a semantic judgment."""

    name: str
    wake_agent: bool
    priority: int
    instruction: str = ""
    max_sentences: int = 0


SILENT = ActionSpec(name="silent", wake_agent=False, priority=0)


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
    decision: ActionSpec | JudgmentSpec
    initial_observation: Literal["baseline", "eligible"] = "baseline"
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

    collector: str
    fingerprint: str
    action: ActionSpec
    facts: JsonObject
    context: JsonObject = field(default_factory=dict)
    decision: JsonObject = field(default_factory=dict)

    def as_json(self) -> JsonObject:
        metadata = self.decision if isinstance(self.decision, Mapping) else {}
        source = metadata.get("source")
        return {
            "collector": self.collector,
            "fingerprint": self.fingerprint,
            "action": self.action.name,
            "priority": self.action.priority,
            "inputs": self.facts,
            "decision": {
                "action": self.action.name,
                "source": source or "fallback",
                **{
                    key: value for key, value in metadata.items() if key not in {"action", "source"}
                },
            },
            "context": self.context,
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
