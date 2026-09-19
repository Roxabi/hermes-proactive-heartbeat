"""Pack every waking observation into one heartbeat candidate."""

from __future__ import annotations

from collections.abc import Mapping

try:
    from . import _bootstrap  # noqa: F401
except ImportError:  # flat plugin-dir / unittest load
    import _bootstrap  # noqa: F401
from models import ActionSpec, Candidate, JsonObject, TickContext
from tick_facts import bound_facts, iso


def pack_wake(candidates: list[Candidate], context: TickContext) -> Candidate | None:
    """Pack every waking candidate into one stdout candidate. Order is priority only."""
    if not candidates:
        return None
    ordered = sorted(
        candidates,
        key=lambda candidate: (
            -candidate.action.priority,
            candidate.collector,
            candidate.fingerprint,
        ),
    )
    observations = tuple(_observation(candidate) for candidate in ordered)
    action = _bundle_action(ordered)
    packed_context = _candidate_context(context)
    if len(ordered) == 1:
        lead = ordered[0]
        return Candidate(
            collector=lead.collector,
            fingerprint=lead.fingerprint,
            action=lead.action,
            facts=lead.facts,
            context=packed_context,
            decision=lead.decision,
            observations=observations,
        )
    sources = {
        str(candidate.decision.get("source") or "fallback")
        for candidate in ordered
        if isinstance(candidate.decision, Mapping)
    }
    source = sources.pop() if len(sources) == 1 else "bundle"
    return Candidate(
        collector="bundle",
        fingerprint="tick",
        action=action,
        facts={},
        context=packed_context,
        decision={"action": action.name, "source": source, "count": len(ordered)},
        observations=observations,
    )


def _observation(candidate: Candidate) -> JsonObject:
    decision = dict(candidate.decision) if isinstance(candidate.decision, Mapping) else {}
    return {
        "collector": candidate.collector,
        "fingerprint": candidate.fingerprint,
        "facts": candidate.facts,
        "decision": decision,
    }


def _bundle_action(candidates: list[Candidate]) -> ActionSpec:
    if len(candidates) == 1:
        return candidates[0].action
    instructions = [
        candidate.action.instruction.strip()
        for candidate in candidates
        if candidate.action.instruction.strip()
    ]
    joined = " ".join(instructions)
    return ActionSpec(
        name="bundle",
        wake_agent=True,
        priority=max(candidate.action.priority for candidate in candidates),
        instruction=(
            "Cover every observation in inputs. Each entry already includes facts "
            "and a judgment. Compose one short message that mentions every item. "
            "Do not add topics that are not in inputs. Do not re-open whether to speak."
            + ((" " + joined) if joined else "")
        ),
        max_sentences=sum(max(1, candidate.action.max_sentences) for candidate in candidates),
    )


def _candidate_context(context: TickContext) -> JsonObject:
    extra = context.settings.get("context")
    merged: JsonObject = {}
    if isinstance(extra, Mapping):
        merged.update({key: value for key, value in extra.items() if value is not None})
    name = context.settings.get("name")
    if isinstance(name, str) and name:
        merged["heartbeat"] = name
    merged["now"] = iso(context.now)
    return bound_facts(merged)
