"""Hermes cron list / edit / create for proactive-heartbeats jobs."""

from __future__ import annotations

import re
import shutil
import subprocess
from typing import Any

try:
    from . import _bootstrap  # noqa: F401
except ImportError:  # flat plugin-dir / unittest load
    import _bootstrap  # noqa: F401

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


def resolve_hermes_executable() -> str:
    hermes = shutil.which("hermes")
    if not hermes:
        raise RuntimeError("hermes executable not found on PATH")
    return hermes


def run_hermes(args: list[str]) -> subprocess.CompletedProcess[str]:
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
        completed = run_hermes(["cron", "list", "--all"])
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
    edit_completed = run_hermes(
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
    create_completed = run_hermes(create_args)
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

    retry = run_hermes(
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
