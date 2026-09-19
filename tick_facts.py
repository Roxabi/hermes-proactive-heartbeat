"""Bound collector facts for persistence, TypeSafe, and digests."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

try:
    from . import _bootstrap  # noqa: F401
except ImportError:  # flat plugin-dir / unittest load
    import _bootstrap  # noqa: F401
from models import JsonObject

_MAX_FACT_KEYS = 32
_MAX_FACT_DEPTH = 4
_MAX_FACT_STRING = 500
_MAX_FACT_LIST = 32


def bound_facts(facts: Mapping[str, Any] | None) -> JsonObject:
    if not isinstance(facts, Mapping):
        return {}
    bounded = {
        str(key): _bound_value(value, depth=0)
        for key, value in list(facts.items())[:_MAX_FACT_KEYS]
    }
    return bounded if isinstance(bounded, dict) else {}


def facts_digest(facts: Mapping[str, Any] | None) -> str:
    payload = json.dumps(
        bound_facts(facts),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _bound_value(value: Any, *, depth: int) -> Any:
    if depth >= _MAX_FACT_DEPTH:
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:_MAX_FACT_STRING]
    if isinstance(value, Mapping):
        return {
            str(key): _bound_value(item, depth=depth + 1)
            for key, item in list(value.items())[:_MAX_FACT_KEYS]
        }
    if isinstance(value, (list, tuple)):
        return [_bound_value(item, depth=depth + 1) for item in list(value)[:_MAX_FACT_LIST]]
    return str(value)[:_MAX_FACT_STRING]
