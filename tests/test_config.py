from __future__ import annotations

import json
from pathlib import Path

from config import (
    heartbeat_names,
    heartbeat_path,
    job_name,
    load_heartbeat,
    load_root,
    plugin_dir,
    root_path,
    shim_basename,
    state_key,
    validate_name,
    write_heartbeat_skeleton,
    write_root_skeleton,
)
from tests.isolation import IsolatedHomeTestCase


class ConfigCascadeTests(IsolatedHomeTestCase):
    def _write(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_missing_root_uses_defaults(self) -> None:
        root = load_root(self.hermes_home)

        self.assertNotIn("heartbeats", root)
        self.assertEqual(root["typesafe_threshold"], 0.65)
        expected_root = self.hermes_home / "proactive-heartbeats" / "proactive-heartbeats.json"
        self.assertEqual(root_path(self.hermes_home), expected_root)
        self.assertEqual(heartbeat_names(self.hermes_home), ())

    def test_config_directory_must_stay_below_hermes_home(self) -> None:
        for value in (".", "..", "../outside", "/tmp/heartbeats", "~/heartbeats"):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                plugin_dir(self.hermes_home, config_dir=value)

    def test_heartbeat_file_wins_delivery_and_is_discovered_by_glob(self) -> None:
        self._write(
            root_path(self.hermes_home),
            {"defaults": {"delivery": {"schedule": "every 1h", "target": "origin"}}},
        )
        self._write(
            heartbeat_path(self.hermes_home, "care"),
            {
                "delivery": {"target": "discord"},
                "collectors": {"sense": {"enabled": True, "command": ["sense-status"]}},
            },
        )

        settings = load_heartbeat(self.hermes_home, "care")

        self.assertEqual(heartbeat_names(self.hermes_home), ("care",))
        self.assertEqual(settings["name"], "care")
        self.assertEqual(settings["delivery"]["schedule"], "every 1h")
        self.assertEqual(settings["delivery"]["target"], "discord")
        self.assertEqual(settings["collectors"]["sense"]["enabled"], True)

    def test_heartbeat_context_overrides_root_context(self) -> None:
        self._write(
            root_path(self.hermes_home),
            {"context": {"who": "root", "channel": "home"}},
        )
        self._write(
            heartbeat_path(self.hermes_home, "care"),
            {"context": {"who": "care"}, "collectors": {}},
        )

        settings = load_heartbeat(self.hermes_home, "care")

        self.assertEqual(settings["context"], {"who": "care", "channel": "home"})

    def test_derived_names_are_per_heartbeat(self) -> None:
        self.assertEqual(job_name("care"), "proactive-heartbeats-care")
        self.assertEqual(shim_basename("care"), "proactive-heartbeats-care.sh")
        self.assertEqual(state_key("care"), "heartbeat:care")
        self.assertEqual(
            heartbeat_path(self.hermes_home, "care"),
            plugin_dir(self.hermes_home) / "heartbeats" / "care.json",
        )

    def test_invalid_name_is_rejected(self) -> None:
        with self.assertRaises(RuntimeError):
            validate_name("../etc")
        with self.assertRaises(RuntimeError):
            validate_name("Care")

    def test_missing_heartbeat_file_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            load_heartbeat(self.hermes_home, "care")

    def test_skeletons_do_not_overwrite(self) -> None:
        root = root_path(self.hermes_home)
        hb = heartbeat_path(self.hermes_home, "care")

        self.assertTrue(write_root_skeleton(root))
        self.assertTrue(write_heartbeat_skeleton(hb))
        root.write_text('{"typesafe_threshold": 0.9}', encoding="utf-8")
        hb.write_text('{"collectors": {}}', encoding="utf-8")

        self.assertFalse(write_root_skeleton(root))
        self.assertFalse(write_heartbeat_skeleton(hb))
        self.assertEqual(root.read_text(encoding="utf-8"), '{"typesafe_threshold": 0.9}')
        self.assertEqual(hb.read_text(encoding="utf-8"), '{"collectors": {}}')
