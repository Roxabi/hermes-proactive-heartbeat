"""Configured GitHub stale PR and Dependabot alert collectors."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

try:
    from .. import _bootstrap  # noqa: F401
except ImportError:  # flat plugin-dir / unittest load
    import _bootstrap  # noqa: F401
from models import JudgmentSpec, Signal, Snapshot, TickContext
from use_cases._exec import (
    RunCommand,
    default_run_command,
    float_or_default,
    int_or_default,
    load_json_payload,
)
from use_cases.actions import GITHUB_INCLUDE_ACTIONS

JsonObject = dict[str, Any]


def _parse_age_days(created: Any, now: datetime) -> int | None:
    if not isinstance(created, str) or not created:
        return None
    try:
        created_at = datetime.fromisoformat(created.replace("Z", "+00:00"))
    except ValueError:
        return None
    current = now if now.tzinfo else now.astimezone()
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=current.tzinfo)
    return (current - created_at).days


def _github_judgment(*, kind: str) -> JudgmentSpec:
    if kind == "prs":
        instructions = "Should stale open GitHub pull requests be mentioned this tick?"
    else:
        instructions = "Should open high or critical Dependabot alerts be mentioned this tick?"
    return JudgmentSpec(
        question={
            "type": "noul",
            "instructions": instructions,
            "criteria": {
                "true": "The item is still actionable and worth a poke",
                "false": "Skip; already known or not worth interrupting",
            },
        },
        actions=dict(GITHUB_INCLUDE_ACTIONS),
        fallback_label="include",
    )


class StalePullRequestsUseCase:
    """Emit capped stale open PRs for a configured owner, excluding Dependabot."""

    id = "stale_prs"

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        run_command: RunCommand | None = None,
    ) -> None:
        self._config = dict(config)
        self._run_command = run_command or default_run_command

    def collect(self, context: TickContext, previous_state: JsonObject) -> Snapshot:
        owner = self._config.get("owner")
        if not isinstance(owner, str) or not owner.strip():
            return Snapshot(
                state={"ok": False, "items": []},
                diagnostics={"error": "unconfigured"},
            )
        owner = owner.strip()
        age_days = int_or_default(self._config.get("age_days"), 7)
        limit = int_or_default(self._config.get("limit"), 5)
        timeout = float_or_default(self._config.get("timeout_seconds"), 12.0)
        search_limit = max(limit, int_or_default(self._config.get("search_limit"), 20))
        repeat_after = self._config.get("repeat_after_seconds")
        repeat_after_seconds = int_or_default(repeat_after, 0) if repeat_after is not None else None

        argv = [
            "gh",
            "search",
            "prs",
            f"--owner={owner}",
            "--state=open",
            f"--limit={search_limit}",
            "--json=number,title,url,createdAt,author,repository",
        ]
        payload, error = load_json_payload(self._run_command, argv, timeout=timeout)
        if error:
            return Snapshot(
                state={"ok": False, "items": []},
                diagnostics={"error": error},
            )
        if not isinstance(payload, list):
            return Snapshot(
                state={"ok": False, "items": []},
                diagnostics={"error": "unparseable"},
            )

        items: list[JsonObject] = []
        signals: list[Signal] = []
        for pull in payload:
            if not isinstance(pull, Mapping):
                continue
            author = ""
            author_obj = pull.get("author")
            if isinstance(author_obj, Mapping):
                author = str(author_obj.get("login") or "")
            if "dependabot" in author.lower():
                continue
            age = _parse_age_days(pull.get("createdAt"), context.now)
            if age is None or age < age_days:
                continue
            repo = ""
            repo_obj = pull.get("repository")
            if isinstance(repo_obj, Mapping):
                repo = str(repo_obj.get("name") or "")
            number = pull.get("number")
            title = str(pull.get("title") or "")[:80]
            facts = {
                "repo": repo,
                "n": number,
                "age_d": age,
                "title": title,
            }
            items.append(facts)
            fingerprint = f"pr:{repo}#{number}"
            signals.append(
                Signal(
                    fingerprint=fingerprint,
                    facts=facts,
                    judgment=_github_judgment(kind="prs"),
                    repeat_after_seconds=repeat_after_seconds,
                )
            )
            if len(items) >= limit:
                break

        return Snapshot(
            signals=tuple(signals),
            state={"ok": True, "items": items, "owner": owner},
        )


class DependabotAlertsUseCase:
    """Emit capped high/critical Dependabot alerts for a configured owner."""

    id = "dependabot_alerts"

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        run_command: RunCommand | None = None,
    ) -> None:
        self._config = dict(config)
        self._run_command = run_command or default_run_command

    def collect(self, context: TickContext, previous_state: JsonObject) -> Snapshot:
        owner = self._config.get("owner")
        if not isinstance(owner, str) or not owner.strip():
            return Snapshot(
                state={"ok": False, "items": []},
                diagnostics={"error": "unconfigured"},
            )
        owner = owner.strip()
        age_days = int_or_default(self._config.get("age_days"), 3)
        limit = int_or_default(self._config.get("limit"), 5)
        timeout = float_or_default(self._config.get("timeout_seconds"), 12.0)
        per_page = max(limit, int_or_default(self._config.get("per_page"), 10))
        repeat_after = self._config.get("repeat_after_seconds")
        repeat_after_seconds = int_or_default(repeat_after, 0) if repeat_after is not None else None

        argv = [
            "gh",
            "api",
            f"orgs/{owner}/dependabot/alerts",
            "-f",
            "state=open",
            "-f",
            f"per_page={per_page}",
        ]
        payload, error = load_json_payload(self._run_command, argv, timeout=timeout)
        if error:
            return Snapshot(
                state={"ok": False, "items": []},
                diagnostics={"error": error},
            )
        if not isinstance(payload, list):
            return Snapshot(
                state={"ok": False, "items": []},
                diagnostics={"error": "unparseable"},
            )

        items: list[JsonObject] = []
        signals: list[Signal] = []
        for alert in payload:
            if not isinstance(alert, Mapping):
                continue
            advisory = alert.get("security_advisory") or {}
            severity = ""
            if isinstance(advisory, Mapping):
                severity = str(advisory.get("severity") or "").lower()
            if severity not in {"high", "critical"}:
                continue
            repo = ""
            repo_obj = alert.get("repository")
            if isinstance(repo_obj, Mapping):
                repo = str(repo_obj.get("name") or "")
            pkg = ""
            dependency = alert.get("dependency") or {}
            if isinstance(dependency, Mapping):
                package = dependency.get("package") or {}
                if isinstance(package, Mapping):
                    pkg = str(package.get("name") or "")
            age = _parse_age_days(alert.get("created_at"), context.now)
            if age is not None and age < age_days:
                continue
            facts = {"repo": repo, "pkg": pkg, "sev": severity, "age_d": age}
            items.append(facts)
            fingerprint = f"cve:{repo}:{pkg}"
            signals.append(
                Signal(
                    fingerprint=fingerprint,
                    facts=facts,
                    judgment=_github_judgment(kind="cves"),
                    repeat_after_seconds=repeat_after_seconds,
                )
            )
            if len(items) >= limit:
                break

        return Snapshot(
            signals=tuple(signals),
            state={"ok": True, "items": items, "owner": owner},
        )
