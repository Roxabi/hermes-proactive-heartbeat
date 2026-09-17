"""Configured local/SSH host health collector."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

try:
    from .. import _bootstrap  # noqa: F401
except ImportError:  # flat plugin-dir / unittest load
    import _bootstrap  # noqa: F401
from models import JudgmentSpec, Signal, Snapshot, TickContext
from use_cases._exec import (
    DEFAULT_HOST_PROBE,
    RunCommand,
    build_argv,
    default_run_command,
    float_or_default,
    int_or_default,
    load_json_payload,
    resolve_source,
)
from use_cases.actions import INCLUDE_ACTIONS

JsonObject = dict[str, Any]


def _host_judgment() -> JudgmentSpec:
    return JudgmentSpec(
        question={
            "type": "noul",
            "instructions": (
                "Should the caretaker mention this machine-health issue "
                "(failed units, disk pressure, or low GPU free memory)?"
            ),
            "criteria": {
                "true": "Something is actually wrong or resource-tight",
                "false": "Noise or already handled; omit",
            },
        },
        actions=dict(INCLUDE_ACTIONS),
        fallback_label="include",
    )


def _host_fingerprints(
    name: str,
    node: Mapping[str, Any],
    *,
    disk_threshold: float,
    gpu_free_threshold: float,
) -> list[tuple[str, JsonObject]]:
    bits: list[tuple[str, JsonObject]] = []
    failed = int_or_default(node.get("failed_units"), 0)
    if failed > 0:
        bits.append(
            (
                f"{name}:failed={failed}",
                {"host": name, "kind": "failed_units", "failed_units": failed},
            )
        )
    disk_pct = float_or_default(node.get("disk_pct"), 0.0)
    if disk_pct >= disk_threshold:
        bits.append(
            (
                f"{name}:disk",
                {"host": name, "kind": "disk", "disk_pct": disk_pct},
            )
        )
    free = node.get("gpu_free_mib")
    if free is not None and float_or_default(free, 0.0) < gpu_free_threshold:
        bits.append(
            (
                f"{name}:gpu",
                {
                    "host": name,
                    "kind": "gpu",
                    "gpu_free_mib": float_or_default(free, 0.0),
                    "gpu_used_mib": node.get("gpu_used_mib"),
                },
            )
        )
    return bits


class HostHealthUseCase:
    """Collect configured host probes and emit deterministic health fingerprints."""

    id = "host"

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        run_command: RunCommand | None = None,
    ) -> None:
        self._config = dict(config)
        self._run_command = run_command or default_run_command

    def collect(self, context: TickContext, previous_state: JsonObject) -> Snapshot:
        hosts = self._config.get("hosts")
        if not isinstance(hosts, list) or not hosts:
            return Snapshot(
                state={"ok": False, "hosts": {}, "fingerprints": []},
                diagnostics={"error": "unconfigured"},
            )

        disk_threshold = float_or_default(self._config.get("disk_threshold_percent"), 85.0)
        gpu_threshold = float_or_default(self._config.get("gpu_free_threshold_mib"), 800.0)
        default_timeout = float_or_default(self._config.get("timeout_seconds"), 10.0)
        repeat_after = self._config.get("repeat_after_seconds")
        repeat_after_seconds = int_or_default(repeat_after, 0) if repeat_after is not None else None

        host_state: JsonObject = {}
        diagnostics: JsonObject = {}
        signals: list[Signal] = []
        fingerprints: list[str] = []

        for index, raw_host in enumerate(hosts):
            if not isinstance(raw_host, Mapping):
                diagnostics[f"host_{index}"] = "invalid"
                continue
            name = str(raw_host.get("name") or f"host{index}")
            source = resolve_source(raw_host, default_command=DEFAULT_HOST_PROBE)
            if source is None:
                host_state[name] = {"error": "unconfigured"}
                diagnostics[name] = "unconfigured"
                continue
            argv = build_argv(source)
            if argv is None:
                host_state[name] = {"error": "unconfigured"}
                diagnostics[name] = "unconfigured"
                continue

            timeout = float_or_default(raw_host.get("timeout_seconds"), default_timeout)
            local_disk = float_or_default(
                raw_host.get("disk_threshold_percent"),
                disk_threshold,
            )
            local_gpu = float_or_default(
                raw_host.get("gpu_free_threshold_mib"),
                gpu_threshold,
            )
            payload, error = load_json_payload(self._run_command, argv, timeout=timeout)
            if error or not isinstance(payload, dict):
                host_state[name] = {"error": error or "unparseable"}
                diagnostics[name] = error or "unparseable"
                continue
            if payload.get("error"):
                err = str(payload.get("error"))
                host_state[name] = {"error": err}
                diagnostics[name] = err
                continue

            compact = {
                key: payload.get(key)
                for key in (
                    "load1",
                    "disk_pct",
                    "failed_units",
                    "gpu_used_mib",
                    "gpu_free_mib",
                )
                if key in payload
            }
            host_state[name] = compact
            for fingerprint, facts in _host_fingerprints(
                name,
                compact,
                disk_threshold=local_disk,
                gpu_free_threshold=local_gpu,
            ):
                fingerprints.append(fingerprint)
                signals.append(
                    Signal(
                        fingerprint=fingerprint,
                        facts=facts,
                        judgment=_host_judgment(),
                        repeat_after_seconds=repeat_after_seconds,
                    )
                )

        state = {
            "ok": not diagnostics,
            "hosts": host_state,
            "fingerprints": fingerprints,
        }
        return Snapshot(
            signals=tuple(signals),
            state=state,
            diagnostics=diagnostics,
        )
