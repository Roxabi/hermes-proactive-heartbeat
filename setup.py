"""Idempotent shim + Hermes cron reconciliation for proactive-heartbeats."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
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

DEFAULT_PROMPT = (
    "Proactive heartbeat wake. The pre-run script stdout ends with a JSON object. "
    "When that object contains heartbeat_candidate, cover every entry in "
    "heartbeat_candidate.inputs. Each entry already includes facts and the judgment "
    "(decision). Follow heartbeat_candidate.delivery.instruction and respect "
    "heartbeat_candidate.delivery.max_sentences. Compose one short message that "
    "mentions every useful item; do not re-open whether to speak, and do not invent "
    "topics that are not in inputs. Ground the message in those inputs and "
    "heartbeat_candidate.context. If inputs is empty, reply with exactly [SILENT]."
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _config_mod() -> Any:
    try:
        from . import config as config_mod
    except ImportError:  # pragma: no cover
        import config as config_mod
    return config_mod


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
    hermes = shutil.which("hermes")
    if not hermes:
        raise RuntimeError("hermes executable not found on PATH")
    return hermes


def _shim_source(name: str) -> str:
    hermes = shlex.quote(resolve_hermes_executable())
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
        _run_hermes(["cron", "remove", _LEGACY_JOB])
    except RuntimeError:
        return


def _run_hermes(args: list[str]) -> subprocess.CompletedProcess[str]:
    hermes = resolve_hermes_executable()
    return subprocess.run(
        [hermes, *args],
        check=False,
        text=True,
        capture_output=True,
    )


def list_cron_jobs() -> list[dict[str, str]]:
    """Parse ``hermes cron list --all`` into id/name rows."""
    try:
        completed = _run_hermes(["cron", "list", "--all"])
    except RuntimeError:
        return []
    if completed.returncode != 0:
        return []
    text = _ANSI_RE.sub("", completed.stdout)
    jobs: list[dict[str, str]] = []
    current_id = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        id_match = re.match(r"^([0-9a-f]{12})\b", line)
        if id_match:
            current_id = id_match.group(1)
            continue
        name_match = re.match(r"^Name:\s*(.+)$", line)
        if name_match:
            jobs.append({"name": name_match.group(1).strip(), "id": current_id, "raw": line})
    return jobs


def find_cron_job(job_name: str) -> dict[str, str] | None:
    """Read-only locate of a job via ``hermes cron list --all``."""
    for job in list_cron_jobs():
        if job["name"] == job_name:
            return job
    return None


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


def _edit_args(
    job_ref: str,
    *,
    job_name: str,
    schedule: str,
    prompt: str,
    deliver: str,
    script_name: str,
    failure_deliver: str | None,
) -> list[str]:
    args = [
        "cron",
        "edit",
        job_ref,
        "--name",
        job_name,
        "--schedule",
        schedule,
        "--prompt",
        prompt,
        "--deliver",
        deliver,
        "--script",
        script_name,
        "--agent",
    ]
    if failure_deliver:
        args.extend(["--failure-deliver", failure_deliver])
    return args


def reconcile_cron_job(
    *,
    schedule: str,
    deliver: str,
    script_name: str,
    failure_deliver: str | None = None,
    prompt: str = DEFAULT_PROMPT,
    job_name: str,
) -> dict[str, Any]:
    """Create or update one job through public ``hermes cron`` commands.

    Always keeps ``no_agent=false`` (omit ``--no-agent`` on create; pass ``--agent`` on edit).
    Never writes jobs.json directly. Edit-first against the stable job name avoids duplicates.
    """
    edit_completed = _run_hermes(
        _edit_args(
            job_name,
            job_name=job_name,
            schedule=schedule,
            prompt=prompt,
            deliver=deliver,
            script_name=script_name,
            failure_deliver=failure_deliver,
        )
    )
    edit_blob = f"{edit_completed.stdout}\n{edit_completed.stderr}"
    if edit_completed.returncode == 0:
        job_id = ""
        match = re.search(r"Updated job:\s*([0-9a-f]{12})", edit_completed.stdout)
        if match:
            job_id = match.group(1)
        return {
            "action": "updated",
            "job_name": job_name,
            "job_id": job_id,
            "stdout": edit_completed.stdout.strip(),
        }
    if not re.search(r"Job not found", edit_blob, re.IGNORECASE):
        detail = (
            edit_completed.stderr or edit_completed.stdout or f"exit {edit_completed.returncode}"
        ).strip()
        raise RuntimeError(f"hermes cron edit failed: {detail}")

    create_args = [
        "cron",
        "create",
        schedule,
        prompt,
        "--name",
        job_name,
        "--deliver",
        deliver,
        "--script",
        script_name,
    ]
    if failure_deliver:
        create_args.extend(["--failure-deliver", failure_deliver])
    create_completed = _run_hermes(create_args)
    if create_completed.returncode == 0:
        job_id = ""
        match = re.search(r"Created job:\s*([0-9a-f]{12})", create_completed.stdout)
        if match:
            job_id = match.group(1)
        return {
            "action": "created",
            "job_name": job_name,
            "job_id": job_id,
            "stdout": create_completed.stdout.strip(),
        }

    retry = _run_hermes(
        _edit_args(
            job_name,
            job_name=job_name,
            schedule=schedule,
            prompt=prompt,
            deliver=deliver,
            script_name=script_name,
            failure_deliver=failure_deliver,
        )
    )
    if retry.returncode == 0:
        job_id = ""
        match = re.search(r"Updated job:\s*([0-9a-f]{12})", retry.stdout)
        if match:
            job_id = match.group(1)
        return {
            "action": "updated",
            "job_name": job_name,
            "job_id": job_id,
            "stdout": retry.stdout.strip(),
        }
    detail = (
        create_completed.stderr or create_completed.stdout or f"exit {create_completed.returncode}"
    ).strip()
    raise RuntimeError(f"hermes cron create failed: {detail}")


def run_setup(*, config_dir: str | None = None) -> dict[str, Any]:
    """Write root skeleton and reconcile one cron job per heartbeat file."""
    config_mod = _config_mod()
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
        result = reconcile_cron_job(
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
    for job in list_cron_jobs():
        leftover = _managed_heartbeat_name(job["name"])
        if leftover is None or leftover in desired:
            continue
        _run_hermes(["cron", "remove", job["name"]])
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
