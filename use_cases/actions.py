"""Trusted delivery actions shared by built-in use cases."""

from __future__ import annotations

try:
    from .. import _bootstrap  # noqa: F401
except ImportError:  # flat plugin-dir / unittest load
    import _bootstrap  # noqa: F401
from models import ActionSpec

SILENT = ActionSpec(
    name="silent",
    priority=0,
    instruction="Do not message the user.",
    max_sentences=0,
)

RECENTER = ActionSpec(
    name="recenter",
    priority=40,
    instruction=(
        "Gently nudge toward one clear next step or less fragmented attention; "
        "keep it to one short sentence."
    ),
    max_sentences=1,
)

WATER = ActionSpec(
    name="water",
    priority=50,
    instruction="Suggest a short pause and a glass of water in one warm sentence.",
    max_sentences=1,
)

LATE = ActionSpec(
    name="late",
    priority=60,
    instruction="Note the unusual hour gently in one short sentence, without lecturing.",
    max_sentences=1,
)

HOST = ActionSpec(
    name="host",
    priority=80,
    instruction=(
        "Mention the machine-health issue briefly using the compact facts; one concrete sentence."
    ),
    max_sentences=1,
)

GITHUB = ActionSpec(
    name="host",
    priority=70,
    instruction=("Mention the GitHub item briefly using the compact facts; one concrete sentence."),
    max_sentences=1,
)

SENSE_ACTIONS = {
    "silent": SILENT,
    "recenter": RECENTER,
    "water": WATER,
    "late": LATE,
}

SENSE_CHOICE_CRITERIA = {
    "silent": "No care nudge is warranted right now",
    "recenter": "Attention is fragmented, stuck, or needs a clear next step",
    "water": "A short pause or glass of water would help",
    "late": "The hour is unusually late or early for active work",
}

INCLUDE_ACTIONS = {
    "include": HOST,
    "silent": SILENT,
}

GITHUB_INCLUDE_ACTIONS = {
    "include": GITHUB,
    "silent": SILENT,
}
