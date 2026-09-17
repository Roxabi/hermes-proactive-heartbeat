"""Ensure the plugin root is importable for flat and package loads."""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parent if _HERE.name == "use_cases" else _HERE
_ROOT = str(ROOT)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
