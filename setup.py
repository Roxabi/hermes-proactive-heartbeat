"""Idempotent shim + Hermes cron reconciliation for proactive-heartbeats."""

from __future__ import annotations

import os
import shlex
import textwrap
from collections.abc import Mapping
from pathlib import Path
from typing import Any

try:
    from . import _bootstrap  # noqa: F401
except ImportError:  # flat plugin-dir / unittest load
    import _bootstrap  # noqa: F401

_LEGACY_SHIMS = ("proactive-heartbeat.sh", "proactive-heartbeat-tick.py")
_LEGACY_JOB = "proactive-heartbeat"


def _config_mod() -> Any:
    try:
        from . import config as config_mod
    except ImportError:  # pragma: no cover
        import config as config_mod
    return config_mod


def _cron_mod() -> Any:
    try:
        from . import cron as cron_mod
    except ImportError:  # pragma: no cover
        import cron as cron_mod
    return cron_mod


def resolve_hermes_home() -> Path:
    raw = (os.environ.get("HERMES_HOME") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path.home() / ".hermes").resolve()


def shim_path(home: Path | None, name: str) -> Path:
    root = home or resolve_hermes_home()
    config_mod = _config_mod()
    return root / "scripts" / config_mod.shim_basename(name)


def resolve_hermes_executable() -> str:
    return _cron_mod().resolve_hermes_executable()


def find_cron_job(job_name: str) -> dict[str, str] | None:
    return _cron_mod().find_cron_job(job_name)


def _shim_source(name: str) -> str:
    hermes = shlex.quote(_cron_mod().resolve_hermes_executable())
    quoted = shlex.quote(name)
    return textwrap.dedent(
        f"""\
        #!/usr/bin/env bash
        # Contained cron shim: absolute hermes executable, named tick.
        set -euo pipefail
        exec {hermes} proactive-heartbeats tick --name {quoted}
        """
    )


def write_shim(home: Path | None, name: str) -> Path:
    """Create or overwrite the per-heartbeat shim under HERMES_HOME/scripts/."""
    root = home or resolve_hermes_home()
    scripts = root / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    path = shim_path(root, name)
    path.write_text(_shim_source(name), encoding="utf-8")
    path.chmod(0o755)
    return path


def _cleanup_legacy_shims(home: Path) -> None:
    scripts = home / "scripts"
    for basename in _LEGACY_SHIMS:
        legacy = scripts / basename
        if legacy.is_file():
            legacy.unlink()
    try:
        _cron_mod().run_hermes(["cron", "remove", _LEGACY_JOB])
    except RuntimeError:
        return


def _managed_heartbeat_name(job_name: str) -> str | None:
    prefix = "proactive-heartbeats-"
    if job_name.startswith(prefix) and job_name != prefix:
        return job_name[len(prefix) :]
    return None


def _delivery_kwargs(delivery: Mapping[str, Any]) -> tuple[str, str, str | None]:
    schedule = str(delivery.get("schedule") or "every 15m").strip() or "every 15m"
    deliver = str(delivery.get("target") or "local").strip() or "local"
    failure = delivery.get("failure_target")
    failure_target = str(failure).strip() if failure else None
    return schedule, deliver, failure_target or None


def run_setup(*, config_dir: str | None = None) -> dict[str, Any]:
    """Write root skeleton and reconcile one cron job per heartbeat file."""
    config_mod = _config_mod()
    cron_mod = _cron_mod()
    home = resolve_hermes_home()
    _cleanup_legacy_shims(home)
    root_file = config_mod.root_path(home, config_dir=config_dir)
    root_created = config_mod.write_root_skeleton(root_file)
    config_mod.collectors_dir(home, config_dir=config_dir).mkdir(parents=True, exist_ok=True)
    (config_mod.plugin_dir(home, config_dir=config_dir) / config_mod.HEARTBEATS_DIRNAME).mkdir(
        parents=True, exist_ok=True
    )
    root = config_mod.load_root(home, config_dir=config_dir)
    names = config_mod.heartbeat_names(home, config_dir=config_dir)
    rows: list[dict[str, Any]] = []
    for name in names:
        settings = config_mod.load_heartbeat(home, name, root=root, config_dir=config_dir)
        shim = write_shim(home, name)
        schedule, deliver, failure_deliver = _delivery_kwargs(settings["delivery"])
        result = cron_mod.reconcile_cron_job(
            schedule=schedule,
            deliver=deliver,
            script_name=config_mod.shim_basename(name),
            failure_deliver=failure_deliver,
            job_name=config_mod.job_name(name),
        )
        rows.append(
            {
                "name": name,
                "job_name": result["job_name"],
                "job_id": result.get("job_id") or "",
                "action": result["action"],
                "shim": str(shim),
                "schedule": schedule,
                "deliver": deliver,
                "config_status": "present",
            }
        )

    desired = set(names)
    for job in cron_mod.list_cron_jobs():
        leftover = _managed_heartbeat_name(job["name"])
        if leftover is None or leftover in desired:
            continue
        cron_mod.run_hermes(["cron", "remove", job["name"]])
        shim = shim_path(home, leftover)
        if shim.is_file():
            shim.unlink()
        rows.append(
            {
                "name": leftover,
                "job_name": job["name"],
                "job_id": job.get("id") or "",
                "action": "removed",
                "shim": str(shim),
                "schedule": "",
                "deliver": "",
                "config_status": "missing",
            }
        )

    return {
        "action": "reconciled",
        "hermes_home": str(home),
        "root": str(root_file),
        "root_status": "created" if root_created else "present",
        "heartbeats": rows,
    }
