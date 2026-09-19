"""Persisted heartbeat state: use-case entries, delivered, pending."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

try:
    from . import _bootstrap  # noqa: F401
except ImportError:  # flat plugin-dir / unittest load
    import _bootstrap  # noqa: F401
from models import JsonObject
from tick_facts import facts_digest

STATE_VERSION = 2


def coerce_previous(previous: Mapping[str, Any] | None) -> JsonObject | None:
    if previous is None:
        return None
    if not isinstance(previous, Mapping):
        return None
    version = previous.get("version")
    if version is None:
        return None
    try:
        if int(version) != STATE_VERSION:
            return None
    except (TypeError, ValueError):
        return None
    return dict(previous)


def empty_state() -> JsonObject:
    return {"version": STATE_VERSION, "use_cases": {}, "delivered": {}, "pending": {}}


def use_case_entry(state: Mapping[str, Any], use_case_id: str) -> JsonObject:
    use_cases = state.get("use_cases") or {}
    if not isinstance(use_cases, Mapping):
        return {"state": {}, "active": []}
    entry = use_cases.get(use_case_id) or {}
    if not isinstance(entry, Mapping):
        return {"state": {}, "active": []}
    result: JsonObject = {
        "state": dict(entry.get("state") or {}) if isinstance(entry.get("state"), Mapping) else {},
        "active": list(entry.get("active") or []) if isinstance(entry.get("active"), list) else [],
    }
    if isinstance(entry.get("diagnostics"), Mapping):
        result["diagnostics"] = dict(entry["diagnostics"])
    return result


def default_cooldown(settings: Mapping[str, Any]) -> int:
    value = settings.get("default_cooldown_seconds", 14_400)
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 14_400


def threshold(settings: Mapping[str, Any]) -> float:
    value = settings.get("typesafe_threshold", 0.65)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.65


def delivery_key(use_case_id: str, fingerprint: str) -> str:
    return f"{use_case_id}:{fingerprint}"


def delivered_view(delivered: Any, use_case_id: str) -> JsonObject:
    if not isinstance(delivered, Mapping):
        return {}
    prefix = delivery_key(use_case_id, "")
    return {
        key[len(prefix) :]: dict(record)
        for key, record in delivered.items()
        if isinstance(key, str) and key.startswith(prefix) and isinstance(record, Mapping)
    }


def retained_delivered(
    delivered: Mapping[str, Any],
    *,
    use_cases: Mapping[str, Any],
    diagnostics: Mapping[str, Any],
) -> JsonObject:
    """Keep active fingerprints; retain failed/unloaded collectors untouched.

    Dropping an inactive fingerprint cannot change due-ness: ``is_due`` already
    returns True when the fingerprint is absent from the previous active set,
    before it consults ``delivered``.
    """

    retained: JsonObject = {}
    for key, record in delivered.items():
        if not isinstance(key, str):
            retained[key] = record
            continue
        use_case_id, separator, fingerprint = key.partition(":")
        entry = use_cases.get(use_case_id)
        if not separator or not isinstance(entry, Mapping) or use_case_id in diagnostics:
            retained[key] = record
            continue
        active = entry.get("active")
        if isinstance(active, list) and fingerprint in active:
            retained[key] = record
    return retained


def coerce_pending(value: Any) -> dict[str, JsonObject]:
    if not isinstance(value, Mapping):
        return {}
    pending: dict[str, JsonObject] = {}
    for key, record in value.items():
        if not isinstance(key, str) or not isinstance(record, Mapping):
            continue
        pending[key] = dict(record)
    return pending


def sorted_pending(pending: Mapping[str, JsonObject]) -> JsonObject:
    return {key: dict(pending[key]) for key in sorted(pending)}


def pending_reusable(record: Mapping[str, Any] | None, facts: Mapping[str, Any]) -> bool:
    if not isinstance(record, Mapping):
        return False
    if not isinstance(record.get("action"), Mapping):
        return False
    if not isinstance(record.get("decision"), Mapping):
        return False
    digest = record.get("facts_digest")
    return isinstance(digest, str) and digest == facts_digest(facts)
