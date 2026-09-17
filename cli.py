"""Operator CLI for ``hermes proactive-heartbeat``."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

try:
    from . import _bootstrap  # noqa: F401
except ImportError:  # flat plugin-dir / unittest load
    import _bootstrap  # noqa: F401

from .models import TickContext

PLUGIN_NAME = "proactive-heartbeat"
STATE_KEY = "engine"

_SETTING_DEFAULTS: dict[str, Any] = {
    "typesafe_url": "https://api.typesafe.ai/v1/systemone",
    "typesafe_model": "jev-latest",
    "typesafe_threshold": 0.65,
    "default_cooldown_seconds": 14400,
    "use_cases": {},
    "delivery": {},
}

_DELIVERY_DEFAULTS: dict[str, Any] = {
    "schedule": "every 15m",
    "target": "origin",
}


def configure_parser(subparser: argparse.ArgumentParser) -> None:
    """Build ``hermes proactive-heartbeat <subcommand>``."""
    subs = subparser.add_subparsers(dest="proactive_heartbeat_command")
    subs.add_parser("tick", help="Collect signals once and emit the wake-gate stdout contract")
    subs.add_parser("status", help="Show concise operator status (no secrets)")
    subs.add_parser("doctor", help="Run local health checks (no secrets)")
    subs.add_parser("setup", help="Install the cron shim and reconcile the Hermes cron job")


def handle(ctx: Any, args: argparse.Namespace) -> int:
    command = getattr(args, "proactive_heartbeat_command", None)
    if command == "tick":
        return cmd_tick(ctx)
    if command == "status":
        return cmd_status(ctx)
    if command == "doctor":
        return cmd_doctor(ctx)
    if command == "setup":
        return cmd_setup(ctx)
    print(
        "Usage: hermes proactive-heartbeat {tick|status|doctor|setup}",
        file=sys.stderr,
    )
    return 2


def load_settings(ctx: Any) -> dict[str, Any]:
    settings: dict[str, Any] = {}
    for key, default in _SETTING_DEFAULTS.items():
        value = ctx.get_config(key, default=default)
        settings[key] = default if value is None else value
    delivery = settings.get("delivery")
    if not isinstance(delivery, Mapping):
        delivery = {}
    merged_delivery = dict(_DELIVERY_DEFAULTS)
    merged_delivery.update({k: v for k, v in delivery.items() if v is not None})
    settings["delivery"] = merged_delivery
    if not isinstance(settings.get("use_cases"), Mapping):
        settings["use_cases"] = {}
    return settings


def _import_tick_deps() -> tuple[Any, Any, Any]:
    """Lazy import engine/registry/typesafe (package or flat plugin-dir layout)."""
    try:
        from .engine import HeartbeatEngine
        from .registry import build_registry
        from .typesafe import TypeSafeClient
    except ImportError:  # pragma: no cover - flat sys.path plugin load
        from engine import HeartbeatEngine
        from registry import build_registry
        from typesafe import TypeSafeClient
    return HeartbeatEngine, build_registry, TypeSafeClient


def _setup_mod() -> Any:
    try:
        from . import setup as setup_mod
    except ImportError:  # pragma: no cover
        import setup as setup_mod
    return setup_mod


def cmd_tick(ctx: Any) -> int:
    """Run one tick and print only the engine stdout contract."""
    try:
        settings = load_settings(ctx)
        previous = ctx.state.get(STATE_KEY, default=None)
        if previous is not None and not isinstance(previous, Mapping):
            print(
                "proactive-heartbeat: persisted engine state is not a JSON object",
                file=sys.stderr,
            )
            return 1

        HeartbeatEngine, build_registry, TypeSafeClient = _import_tick_deps()
        client = TypeSafeClient(
            api_key=os.environ.get("TYPESAFE_API_KEY"),
            url=str(settings["typesafe_url"]),
            model=str(settings["typesafe_model"]),
        )
        engine = HeartbeatEngine(
            use_cases=build_registry(settings),
            typesafe=client,
        )
        result = engine.tick(
            TickContext(now=datetime.now(timezone.utc), settings=settings),
            previous_state=dict(previous) if isinstance(previous, Mapping) else None,
        )
        ctx.state.set(STATE_KEY, result.state)
        # Exact engine stdout contract only — no banners, no trailing chatter.
        rendered = result.render()
        sys.stdout.write(rendered if rendered.endswith("\n") else f"{rendered}\n")
        sys.stdout.flush()
        return 0
    except Exception as exc:  # noqa: BLE001 — operator CLI must surface unrecoverable local errors
        print(f"proactive-heartbeat tick failed: {exc}", file=sys.stderr)
        return 1


def cmd_status(ctx: Any) -> int:
    setup_mod = _setup_mod()
    settings = load_settings(ctx)
    delivery = settings["delivery"]
    home = setup_mod.resolve_hermes_home()
    shim = setup_mod.shim_path(home)
    previous = ctx.state.get(STATE_KEY, default=None)
    typesafe_key_set = bool(os.environ.get("TYPESAFE_API_KEY"))
    use_case_ids = sorted(str(k) for k in (settings.get("use_cases") or {}))

    print(f"plugin: {PLUGIN_NAME}")
    print(f"hermes_home: {home}")
    print(f"schedule: {delivery.get('schedule')}")
    print(f"delivery_target: {delivery.get('target')}")
    failure = delivery.get("failure_target")
    if failure:
        print(f"failure_target: {failure}")
    print(f"typesafe_key: {'set' if typesafe_key_set else 'missing'}")
    print(f"typesafe_model: {settings.get('typesafe_model')}")
    print(f"use_cases: {', '.join(use_case_ids) if use_case_ids else '(none configured)'}")
    print(f"shim: {shim} ({'present' if shim.is_file() else 'missing'})")
    print(f"engine_state: {'present' if previous is not None else 'empty'}")
    print(f"hermes_cli: {shutil.which('hermes') or 'not-on-path'}")
    return 0


def cmd_doctor(ctx: Any) -> int:
    setup_mod = _setup_mod()
    settings = load_settings(ctx)
    issues: list[str] = []
    warnings: list[str] = []

    hermes = shutil.which("hermes")
    if not hermes:
        issues.append("hermes executable not found on PATH")

    home = setup_mod.resolve_hermes_home()
    if not home.is_dir():
        issues.append(f"HERMES_HOME does not exist: {home}")

    shim = setup_mod.shim_path(home)
    if not shim.is_file():
        issues.append(f"cron shim missing: {shim} (run setup)")
    elif not os.access(shim, os.X_OK):
        issues.append(f"cron shim is not executable: {shim}")

    delivery = settings["delivery"]
    if not str(delivery.get("schedule") or "").strip():
        issues.append("delivery.schedule is empty")
    if not str(delivery.get("target") or "").strip():
        issues.append("delivery.target is empty")

    if hermes and home.is_dir():
        found = setup_mod.find_cron_job(setup_mod.JOB_NAME)
        if found is None:
            warnings.append(f"cron job {setup_mod.JOB_NAME!r} not found (run setup to create it)")
        else:
            print(f"cron_job: {found.get('id', '?')} ({found.get('name', setup_mod.JOB_NAME)})")

    if not os.environ.get("TYPESAFE_API_KEY"):
        warnings.append("TYPESAFE_API_KEY unset — deterministic fallbacks only")

    if issues:
        print("doctor: FAIL")
        for item in issues:
            print(f"  error: {item}")
        for item in warnings:
            print(f"  warn: {item}")
        return 1

    print("doctor: OK")
    for item in warnings:
        print(f"  warn: {item}")
    print(f"  hermes: {hermes}")
    print(f"  shim: {shim}")
    print(f"  schedule: {delivery.get('schedule')}")
    print(f"  delivery_target: {delivery.get('target')}")
    return 0


def cmd_setup(ctx: Any) -> int:
    try:
        setup_mod = _setup_mod()
        settings = load_settings(ctx)
        summary = setup_mod.run_setup(settings)
        print(f"setup: {summary['action']}")
        print(f"  hermes_home: {summary['hermes_home']}")
        print(f"  shim: {summary['shim']}")
        print(f"  job_name: {summary['job_name']}")
        print(f"  schedule: {summary['schedule']}")
        print(f"  deliver: {summary['deliver']}")
        if summary.get("job_id"):
            print(f"  job_id: {summary['job_id']}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"proactive-heartbeat setup failed: {exc}", file=sys.stderr)
        return 1
