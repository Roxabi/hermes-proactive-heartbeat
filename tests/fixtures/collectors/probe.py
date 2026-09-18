"""Minimal user collector used by plugin tests."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from models import ActionSpec, JudgmentSpec, Signal, Snapshot, TickContext

SILENT = ActionSpec(
    name="silent",
    wake_agent=False,
    priority=0,
    instruction="Do not message the user.",
    max_sentences=0,
)
INCLUDE = ActionSpec(
    name="include",
    wake_agent=True,
    priority=10,
    instruction="Mention the probe token in one sentence.",
    max_sentences=1,
)


class Collector:
    id = "probe"

    def __init__(self, config: Mapping[str, Any]) -> None:
        self._config = dict(config)

    def collect(self, context: TickContext, previous_state: dict[str, Any]) -> Snapshot:
        del context, previous_state
        token = self._config.get("token")
        if not isinstance(token, str) or not token:
            return Snapshot(state={"ok": False}, diagnostics={"error": "unconfigured"})
        return Snapshot(
            signals=(
                Signal(
                    fingerprint=f"probe:{token}",
                    facts={"token": token},
                    decision=JudgmentSpec(
                        question={
                            "id": "probe",
                            "type": "bool",
                            "instructions": "Include this probe?",
                        },
                        actions={"silent": SILENT, "include": INCLUDE},
                        fallback_label="include",
                    ),
                ),
            ),
            state={"ok": True, "token": token},
        )
