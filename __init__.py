"""Proactive heartbeat Hermes plugin — CLI registration only."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent
_ROOT_S = str(_ROOT)
if _ROOT_S not in sys.path:
    sys.path.insert(0, _ROOT_S)


def register(ctx: Any) -> None:
    """Register the ``proactive-heartbeat`` CLI subtree. No hooks, tools, or background work."""
    from . import cli

    def _handler(args: Any) -> int:
        return cli.handle(ctx, args)

    ctx.register_cli_command(
        name="proactive-heartbeat",
        help="Run and manage the proactive heartbeat tick pipeline",
        setup_fn=cli.configure_parser,
        handler_fn=_handler,
        description=(
            "Operator CLI for the proactive heartbeat plugin: tick once, "
            "inspect status, run doctor checks, and reconcile the Hermes cron job."
        ),
    )
