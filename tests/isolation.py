from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path


class IsolatedHomeTestCase(unittest.TestCase):
    """Keep collector/plugin tests off the real HOME and HERMES_HOME."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        root = Path(self._tmpdir.name)
        self.home = root / "home"
        self.hermes_home = root / "hermes"
        self.home.mkdir()
        self.hermes_home.mkdir()
        self._old_environ = {
            key: os.environ.get(key)
            for key in ("HOME", "HERMES_HOME", "XDG_CONFIG_HOME", "TYPESAFE_API_KEY")
        }
        os.environ["HOME"] = str(self.home)
        os.environ["HERMES_HOME"] = str(self.hermes_home)
        os.environ["XDG_CONFIG_HOME"] = str(root / "xdg")
        os.environ.pop("TYPESAFE_API_KEY", None)

    def tearDown(self) -> None:
        for key, value in self._old_environ.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmpdir.cleanup()
