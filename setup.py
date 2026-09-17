"""Idempotent shim + Hermes cron reconciliation for proactive-heartbeat."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import textwrap
from collections.abc import Mapping
from pathlib import Path
from typing import Any

JOB_NAME = "proactive-heartbeat"
SHIM_BASENAME = "proactive-heartbeat.sh"
_LEGACY_SHIM_BASENAME = "proactive-heartbeat-tick.py"

# Generic agent prompt: consumes heartbeat_candidate.delivery from the prerun script stdout.
DEFAULT_PROMPT = (
    "Proactive heartbeat wake. The pre-run script stdout ends with a JSON object. "
    "When that object contains heartbeat_candidate, follow "
    "heartbeat_candidate.delivery.instruction and respect "
    "heartbeat_candidate.delivery.max_sentences. Use heartbeat_candidate.facts only as "
    "supporting context. Do not invent extra work. If nothing actionable remains, reply "
    "with exactly [SILENT]."
)

_SHIM_SOURCE = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    # Contained cron shim: forward to `hermes proactive-heartbeat tick`.
    set -euo pipefail
    if ! command -v hermes >/dev/null 2>&1; then
      echo "hermes executable not found on PATH" >&2
      exit 127
    fi
    exec hermes proactive-heartbeat tick
    """
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def resolve_hermes_home() -> Path:
    raw = (os.environ.get("HERMES_HOME") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path.home() / ".hermes").resolve()


def shim_path(home: Path | None = None) -> Path:
    root = home or resolve_hermes_home()
    return root / "scripts" / SHIM_BASENAME


def resolve_hermes_executable() -> str:
    hermes = shutil.which("hermes")
    if not hermes:
        raise RuntimeError("hermes executable not found on PATH")
    return hermes


def write_shim(home: Path | None = None) -> Path:
    """Create or overwrite the contained executable shim under HERMES_HOME/scripts/."""
    root = home or resolve_hermes_home()
    scripts = root / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    path = scripts / SHIM_BASENAME
    path.write_text(_SHIM_SOURCE, encoding="utf-8")
    path.chmod(0o755)
    legacy = scripts / _LEGACY_SHIM_BASENAME
    if legacy.is_file():
        legacy.unlink()
    return path


def _run_hermes(args: list[str]) -> subprocess.CompletedProcess[str]:
    hermes = resolve_hermes_executable()
    return subprocess.run(
        [hermes, *args],
        check=False,
        text=True,
        capture_output=True,
    )


def find_cron_job(job_name: str = JOB_NAME) -> dict[str, str] | None:
    """Read-only locate of our job via ``hermes cron list --all`` (ANSI-stripped)."""
    try:
        completed = _run_hermes(["cron", "list", "--all"])
    except RuntimeError:
        return None
    if completed.returncode != 0:
        return None
    text = _ANSI_RE.sub("", completed.stdout)
    current_id = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        id_match = re.match(r"^([0-9a-f]{12})\b", line)
        if id_match:
            current_id = id_match.group(1)
            continue
        name_match = re.match(r"^Name:\s*(.+)$", line)
        if name_match and name_match.group(1).strip() == job_name:
            return {"name": job_name, "id": current_id, "raw": line}
    return None


def _delivery_kwargs(delivery: Mapping[str, Any]) -> tuple[str, str, str | None]:
    schedule = str(delivery.get("schedule") or "every 15m").strip() or "every 15m"
    deliver = str(delivery.get("target") or "origin").strip() or "origin"
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
    job_name: str = JOB_NAME,
) -> dict[str, Any]:
    """Create or update the standard job through public ``hermes cron`` commands.

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

    # Race: job appeared between edit-miss and create — one more edit against the name.
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


def run_setup(settings: Mapping[str, Any]) -> dict[str, Any]:
    """Write the shim and reconcile the cron job from plugin delivery settings."""
    home = resolve_hermes_home()
    shim = write_shim(home)
    delivery = settings.get("delivery") if isinstance(settings.get("delivery"), Mapping) else {}
    schedule, deliver, failure_deliver = _delivery_kwargs(delivery)
    result = reconcile_cron_job(
        schedule=schedule,
        deliver=deliver,
        script_name=SHIM_BASENAME,
        failure_deliver=failure_deliver,
    )
    return {
        "action": result["action"],
        "hermes_home": str(home),
        "shim": str(shim),
        "job_name": result["job_name"],
        "job_id": result.get("job_id") or "",
        "schedule": schedule,
        "deliver": deliver,
    }
