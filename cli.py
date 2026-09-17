"""Operator CLI for ``hermes proactive-heartbeats``."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from . import _bootstrap  # noqa: F401
except ImportError:  # flat plugin-dir / unittest load
    import _bootstrap  # noqa: F401

from .models import TickContext

PLUGIN_NAME = "proactive-heartbeats"


def configure_parser(subparser: argparse.ArgumentParser) -> None:
    """Build ``hermes proactive-heartbeats <subcommand>``."""
    subs = subparser.add_subparsers(dest="proactive_heartbeats_command")
    tick = subs.add_parser(
        "tick",
        help="Collect signals once for one heartbeat and emit the wake-gate stdout",
    )
    tick.add_argument(
        "--name",
        required=True,
        help="Heartbeat id; loads heartbeats/<name>.json",
    )
    subs.add_parser("status", help="Show concise operator status (no secrets)")
    subs.add_parser("doctor", help="Run local health checks (no secrets)")
    subs.add_parser("setup", help="Write config skeletons and reconcile one cron job per heartbeat")


def handle(ctx: Any, args: argparse.Namespace) -> int:
    command = getattr(args, "proactive_heartbeats_command", None)
    if command == "tick":
        return cmd_tick(ctx, args)
    if command == "status":
        return cmd_status(ctx)
    if command == "doctor":
        return cmd_doctor(ctx)
    if command == "setup":
        return cmd_setup(ctx)
    print(
        "Usage: hermes proactive-heartbeats {tick|status|doctor|setup}",
        file=sys.stderr,
    )
    return 2


def _config_mod() -> Any:
    try:
        from . import config as config_mod
    except ImportError:  # pragma: no cover
        import config as config_mod
    return config_mod


def _setup_mod() -> Any:
    try:
        from . import setup as setup_mod
    except ImportError:  # pragma: no cover
        import setup as setup_mod
    return setup_mod


def _import_tick_deps() -> tuple[Any, Any, Any]:
    try:
        from .engine import HeartbeatEngine
        from .registry import build_registry
        from .typesafe import TypeSafeClient
    except ImportError:  # pragma: no cover - flat sys.path plugin load
        from engine import HeartbeatEngine
        from registry import build_registry
        from typesafe import TypeSafeClient
    return HeartbeatEngine, build_registry, TypeSafeClient


def config_dir_setting(ctx: Any) -> str | None:
    config_mod = _config_mod()
    raw = ctx.get_config(config_mod.CONFIG_DIR_KEY, default=config_mod.PLUGIN_DIRNAME)
    if not isinstance(raw, str) or not raw.strip():
        return config_mod.PLUGIN_DIRNAME
    return raw.strip()


def resolve_home() -> Path:
    return _setup_mod().resolve_hermes_home()


def cmd_tick(ctx: Any, args: argparse.Namespace) -> int:
    """Run one named heartbeat tick and print only the engine stdout contract."""
    try:
        config_mod = _config_mod()
        name = config_mod.validate_name(getattr(args, "name", "") or "")
        home = resolve_home()
        directory = config_dir_setting(ctx)
        settings = config_mod.load_heartbeat(home, name, config_dir=directory)
        key = config_mod.state_key(name)
        previous = ctx.state.get(key, default=None)
        if previous is not None and not isinstance(previous, Mapping):
            print(
                "proactive-heartbeats: persisted engine state is not a JSON object",
                file=sys.stderr,
            )
            return 1

        HeartbeatEngine, build_registry, TypeSafeClient = _import_tick_deps()
        client = TypeSafeClient(
            api_key=os.environ.get("TYPESAFE_API_KEY"),
            model=str(settings["typesafe_model"]),
        )
        engine = HeartbeatEngine(
            use_cases=build_registry(
                settings,
                collectors_dir=config_mod.collectors_dir(home, config_dir=directory),
            ),
            typesafe=client,
        )
        result = engine.tick(
            TickContext(now=datetime.now(timezone.utc), settings=settings),
            previous_state=dict(previous) if isinstance(previous, Mapping) else None,
        )
        ctx.state.set(key, result.state)
        if result.diagnostics:
            failed = ", ".join(sorted(str(item) for item in result.diagnostics))
            print(f"proactive-heartbeats: collector failure: {failed}", file=sys.stderr)
            return 1
        rendered = result.render()
        sys.stdout.write(rendered if rendered.endswith("\n") else f"{rendered}\n")
        sys.stdout.flush()
        return 0
    except Exception as exc:  # noqa: BLE001 — operator CLI must surface unrecoverable local errors
        print(f"proactive-heartbeats tick failed: {exc}", file=sys.stderr)
        return 1


def cmd_status(ctx: Any) -> int:
    config_mod = _config_mod()
    setup_mod = _setup_mod()
    home = resolve_home()
    directory = config_dir_setting(ctx)
    root_file = config_mod.root_path(home, config_dir=directory)
    root = config_mod.load_root(home, config_dir=directory)
    names = config_mod.heartbeat_names(home, config_dir=directory)
    typesafe_key_set = bool(os.environ.get("TYPESAFE_API_KEY"))

    print(f"plugin: {PLUGIN_NAME}")
    print(f"hermes_home: {home}")
    print(f"root: {root_file} ({'present' if root_file.is_file() else 'missing'})")
    print(f"typesafe_key: {'set' if typesafe_key_set else 'missing'}")
    print(f"typesafe_model: {root.get('typesafe_model')}")
    print(f"heartbeats: {', '.join(names) if names else '(none)'}")
    print(f"hermes_cli: {shutil.which('hermes') or 'not-on-path'}")
    for name in names:
        path = config_mod.heartbeat_path(home, name, config_dir=directory)
        shim = setup_mod.shim_path(home, name)
        key = config_mod.state_key(name)
        previous = ctx.state.get(key, default=None)
        try:
            settings = config_mod.load_heartbeat(home, name, root=root, config_dir=directory)
            delivery = settings["delivery"]
            collectors = sorted(str(item) for item in (settings.get("collectors") or {}))
            print(f"  {name}:")
            print(f"    config: {path} ({'present' if path.is_file() else 'missing'})")
            print(f"    schedule: {delivery.get('schedule')}")
            print(f"    delivery_target: {delivery.get('target')}")
            print(
                "    collectors: " + (", ".join(collectors) if collectors else "(none configured)")
            )
            print(f"    job: {config_mod.job_name(name)}")
            print(f"    shim: {shim} ({'present' if shim.is_file() else 'missing'})")
            print(f"    engine_state: {'present' if previous is not None else 'empty'}")
        except Exception as exc:  # noqa: BLE001
            print(f"  {name}: error ({exc})")
    return 0


def cmd_doctor(ctx: Any) -> int:
    config_mod = _config_mod()
    setup_mod = _setup_mod()
    issues: list[str] = []
    warnings: list[str] = []

    hermes = shutil.which("hermes")
    if not hermes:
        issues.append("hermes executable not found on PATH")

    home = resolve_home()
    if not home.is_dir():
        issues.append(f"HERMES_HOME does not exist: {home}")

    directory = config_dir_setting(ctx)
    root_file = config_mod.root_path(home, config_dir=directory)
    if not root_file.is_file():
        warnings.append(f"root config missing: {root_file} (run setup)")

    try:
        root = config_mod.load_root(home, config_dir=directory)
    except Exception as exc:  # noqa: BLE001
        issues.append(f"root config invalid: {root_file} ({exc})")
        root = {}

    names = config_mod.heartbeat_names(home, config_dir=directory)
    if not names:
        warnings.append("no heartbeat files in heartbeats/")

    for name in names:
        path = config_mod.heartbeat_path(home, name, config_dir=directory)
        if not path.is_file():
            issues.append(f"heartbeat config missing: {path} (run setup)")
            continue
        try:
            settings = config_mod.load_heartbeat(home, name, root=root, config_dir=directory)
        except Exception as exc:  # noqa: BLE001
            issues.append(f"heartbeat {name!r} invalid: {exc}")
            continue
        try:
            _, build_registry, _ = _import_tick_deps()
            build_registry(
                settings,
                collectors_dir=config_mod.collectors_dir(home, config_dir=directory),
            )
        except Exception as exc:  # noqa: BLE001
            issues.append(f"{name}: collector load failed ({exc})")
            continue
        delivery = settings["delivery"]
        if not str(delivery.get("schedule") or "").strip():
            issues.append(f"{name}: delivery.schedule is empty")
        if not str(delivery.get("target") or "").strip():
            issues.append(f"{name}: delivery.target is empty")
        elif str(delivery.get("target") or "").strip() == "origin":
            warnings.append(
                f"{name}: delivery.target is 'origin' — standalone CLI jobs need a concrete "
                "Hermes --deliver target (or a home channel) or Cron will not deliver"
            )
        shim = setup_mod.shim_path(home, name)
        if not shim.is_file():
            issues.append(f"{name}: cron shim missing: {shim} (run setup)")
        elif not os.access(shim, os.X_OK):
            issues.append(f"{name}: cron shim is not executable: {shim}")
        if hermes and home.is_dir():
            job = config_mod.job_name(name)
            found = setup_mod.find_cron_job(job)
            if found is None:
                warnings.append(f"{name}: cron job {job!r} not found (run setup)")
            else:
                print(f"cron_job: {found.get('id', '?')} ({found.get('name', job)})")

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
    print(f"  root: {root_file}")
    print(f"  heartbeats: {', '.join(names) if names else '(none)'}")
    return 0


def cmd_setup(ctx: Any) -> int:
    try:
        setup_mod = _setup_mod()
        directory = config_dir_setting(ctx)
        summary = setup_mod.run_setup(config_dir=directory)
        print(f"setup: {summary['action']}")
        print(f"  hermes_home: {summary['hermes_home']}")
        print(f"  root: {summary['root']} ({summary['root_status']})")
        for row in summary["heartbeats"]:
            print(
                f"  {row['name']}: job={row['job_name']} "
                f"config={row['config_status']} shim={row['shim']} "
                f"cron={row['action']} schedule={row['schedule']} deliver={row['deliver']}"
            )
            if row.get("job_id"):
                print(f"    job_id: {row['job_id']}")
        if not summary["heartbeats"]:
            print("  heartbeats: (none — add heartbeats/<name>.json and re-run setup)")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"proactive-heartbeats setup failed: {exc}", file=sys.stderr)
        return 1
