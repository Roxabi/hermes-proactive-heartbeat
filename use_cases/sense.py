"""Sense care-brief collector and action mapping."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

try:
    from .. import _bootstrap  # noqa: F401
except ImportError:  # flat plugin-dir / unittest load
    import _bootstrap  # noqa: F401
from models import JudgmentSpec, Signal, Snapshot, TickContext
from use_cases._exec import (
    RunCommand,
    build_argv,
    default_run_command,
    float_or_default,
    int_or_default,
    load_json_payload,
    resolve_source,
)
from use_cases.actions import SENSE_ACTIONS, SENSE_CHOICE_CRITERIA

JsonObject = dict[str, Any]


def _stretch_crossed(
    current: Mapping[str, Any] | None,
    previous: Mapping[str, Any] | None,
    *,
    threshold_minutes: float,
) -> bool:
    if not current:
        return False
    minutes = float_or_default(current.get("minutes"), 0.0)
    if minutes < threshold_minutes:
        return False
    if not previous:
        return True
    prev_minutes = float_or_default(previous.get("minutes"), 0.0)
    return prev_minutes < threshold_minutes or previous.get("app") != current.get("app")


def _preferred_action(
    *,
    signals: Sequence[str],
    stretch_new: bool,
    last_away: Mapping[str, Any] | None,
    away_minutes: float,
) -> str:
    if "late_night" in signals:
        return "late"
    pause = last_away or {}
    if pause.get("ongoing") and float_or_default(pause.get("minutes"), 0.0) >= away_minutes:
        return "water"
    if "post_peak_idle" in signals:
        return "water"
    if "long_focus" in signals or stretch_new:
        return "recenter"
    if "high_switch_rate" in signals:
        return "recenter"
    if signals:
        return "recenter"
    return "silent"


def _action_for_fingerprint(fingerprint: str) -> str:
    if fingerprint == "late_night" or fingerprint.startswith("late:"):
        return "late"
    if fingerprint in {"post_peak_idle", "away"} or fingerprint.startswith("water:"):
        return "water"
    return "recenter"


def _sense_judgment(fallback_label: str) -> JudgmentSpec:
    return JudgmentSpec(
        question={
            "type": "choice",
            "instructions": (
                "Choose one caretaker action for the user right now based only on "
                "these care facts. Prefer silent when a nudge would not help."
            ),
            "criteria": dict(SENSE_CHOICE_CRITERIA),
        },
        actions=dict(SENSE_ACTIONS),
        fallback_label=fallback_label,
    )


def _compact_stretch(value: Any) -> JsonObject | None:
    if not isinstance(value, Mapping):
        return None
    app = value.get("app")
    minutes = value.get("minutes")
    out: JsonObject = {}
    if app is not None:
        out["app"] = app
    if minutes is not None:
        out["minutes"] = minutes
    return out or None


def _compact_away(value: Any) -> JsonObject | None:
    if not isinstance(value, Mapping):
        return None
    out: JsonObject = {}
    if "ongoing" in value:
        out["ongoing"] = bool(value.get("ongoing"))
    if "minutes" in value:
        out["minutes"] = value.get("minutes")
    return out or None


class SenseUseCase:
    """Collect a configured Sense JSON care brief and map it to care actions."""

    id = "sense"

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        run_command: RunCommand | None = None,
    ) -> None:
        self._config = dict(config)
        self._run_command = run_command or default_run_command

    def collect(self, context: TickContext, previous_state: JsonObject) -> Snapshot:
        source = resolve_source(self._config)
        if source is None:
            return Snapshot(
                state={"ok": False},
                diagnostics={"error": "unconfigured"},
            )
        argv = build_argv(source)
        if argv is None:
            return Snapshot(
                state={"ok": False},
                diagnostics={"error": "unconfigured"},
            )

        timeout = float_or_default(self._config.get("timeout_seconds"), 15.0)
        payload, error = load_json_payload(self._run_command, argv, timeout=timeout)
        if error or not isinstance(payload, dict):
            return Snapshot(
                state={
                    "ok": False,
                    "signals": [],
                    "current_stretch": None,
                },
                diagnostics={"error": error or "unparseable"},
            )
        if payload.get("error"):
            return Snapshot(
                state={
                    "ok": False,
                    "signals": [],
                    "current_stretch": None,
                },
                diagnostics={"error": str(payload.get("error"))},
            )

        stretch_minutes = float_or_default(self._config.get("stretch_minutes"), 40.0)
        away_minutes = float_or_default(self._config.get("away_minutes"), 10.0)
        repeat_after = self._config.get("repeat_after_seconds")
        repeat_after_seconds = int_or_default(repeat_after, 0) if repeat_after is not None else None

        raw_signals = payload.get("signals") or []
        signals = [str(item) for item in raw_signals] if isinstance(raw_signals, list) else []
        current_stretch = _compact_stretch(payload.get("current_stretch"))
        last_away = _compact_away(payload.get("last_away"))
        previous_stretch = previous_state.get("current_stretch")
        if not isinstance(previous_stretch, Mapping):
            previous_stretch = None
        stretch_new = _stretch_crossed(
            current_stretch,
            previous_stretch,
            threshold_minutes=stretch_minutes,
        )

        preferred = _preferred_action(
            signals=signals,
            stretch_new=stretch_new,
            last_away=last_away,
            away_minutes=away_minutes,
        )

        facts: JsonObject = {
            "signals": signals,
            "shape": payload.get("shape"),
            "current_stretch": current_stretch,
            "last_away": last_away,
            "stretch_new": stretch_new,
            "preferred_action": preferred,
        }

        emitted: list[Signal] = []
        seen: set[str] = set()

        def add_signal(fingerprint: str, fallback: str) -> None:
            if fingerprint in seen or fallback == "silent":
                return
            seen.add(fingerprint)
            emitted.append(
                Signal(
                    fingerprint=fingerprint,
                    facts=facts,
                    judgment=_sense_judgment(fallback),
                    repeat_after_seconds=repeat_after_seconds,
                )
            )

        for name in signals:
            add_signal(name, _action_for_fingerprint(name))
        if (
            last_away
            and last_away.get("ongoing")
            and float_or_default(last_away.get("minutes"), 0.0) >= away_minutes
        ):
            add_signal("away", "water")
        if stretch_new and current_stretch is not None:
            app = str(current_stretch.get("app") or "unknown")
            add_signal(f"stretch:{app}", "recenter")

        # Ensure a plausible preferred care action still surfaces even when only
        # derived conditions (away/stretch) produced it.
        if preferred != "silent" and not emitted:
            add_signal(f"care:{preferred}", preferred)

        state = {
            "ok": True,
            "signals": signals,
            "current_stretch": current_stretch,
            "preferred_action": preferred,
        }
        return Snapshot(signals=tuple(emitted), state=state)
