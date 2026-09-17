from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from shutil import copy2

from models import TickContext
from registry import build_registry, enabled_ids
from tests.isolation import IsolatedHomeTestCase

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "collectors" / "probe.py"


class RegistryLoadTests(IsolatedHomeTestCase):
    def _collectors_dir(self) -> Path:
        directory = self.hermes_home / "proactive-heartbeats" / "collectors"
        directory.mkdir(parents=True)
        copy2(FIXTURE, directory / "probe.py")
        return directory

    def test_disabled_collectors_are_skipped_but_enabled_missing_collectors_fail(self) -> None:
        collectors_dir = self._collectors_dir()
        registry = build_registry(
            {"collectors": {"probe": {"enabled": False, "token": "x"}}},
            collectors_dir=collectors_dir,
        )
        self.assertEqual(list(registry), [])

        with self.assertRaisesRegex(RuntimeError, "enabled collector 'unknown' is missing"):
            enabled_ids(
                {"collectors": {"unknown": {"enabled": True}}},
                collectors_dir=collectors_dir,
            )

    def test_collector_id_cannot_escape_collectors_directory(self) -> None:
        collectors_dir = self._collectors_dir()
        with self.assertRaisesRegex(RuntimeError, "invalid collector id"):
            build_registry(
                {"collectors": {"../probe": {"enabled": True}}},
                collectors_dir=collectors_dir,
            )

    def test_enabled_user_collector_is_loaded_from_hermes_home(self) -> None:
        collectors_dir = self._collectors_dir()
        settings = {"collectors": {"probe": {"enabled": True, "token": "abc"}}}
        registry = build_registry(settings, collectors_dir=collectors_dir)

        self.assertEqual([case.id for case in registry], ["probe"])
        snapshot = registry[0].collect(
            TickContext(now=datetime(2026, 9, 17, tzinfo=timezone.utc), settings={}),
            {},
        )
        self.assertEqual([signal.fingerprint for signal in snapshot.signals], ["probe:abc"])
