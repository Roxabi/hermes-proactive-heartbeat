from __future__ import annotations

import json
from types import SimpleNamespace
from unittest import mock

from tests.isolation import IsolatedHomeTestCase


def _cron_list(*names: str) -> SimpleNamespace:
    lines = []
    for index, name in enumerate(names, start=1):
        lines.append(f"{index:012x}")
        lines.append(f"Name: {name}")
    return SimpleNamespace(returncode=0, stdout="\n".join(lines) + "\n", stderr="")


class SetupReconcileTests(IsolatedHomeTestCase):
    def test_two_heartbeat_files_get_two_shims_and_job_names(self) -> None:
        from setup import run_setup

        root = self.hermes_home / "proactive-heartbeats"
        (root / "heartbeats").mkdir(parents=True)
        for name in ("care", "infra"):
            (root / "heartbeats" / f"{name}.json").write_text(
                json.dumps({"delivery": {"target": "origin"}, "collectors": {}}),
                encoding="utf-8",
            )

        calls: list[list[str]] = []

        def fake_run(args: list[str]) -> SimpleNamespace:
            calls.append(list(args))
            if args[:2] == ["cron", "list"]:
                return _cron_list("proactive-heartbeats-stale")
            if args[:2] == ["cron", "edit"]:
                return SimpleNamespace(returncode=1, stdout="", stderr="Job not found")
            return SimpleNamespace(returncode=0, stdout="Created job: 0123456789ab\n", stderr="")

        mock_hermes = "/mock/bin/hermes"
        with (
            mock.patch("setup._run_hermes", side_effect=fake_run),
            mock.patch("setup.resolve_hermes_executable", return_value=mock_hermes),
        ):
            summary = run_setup()

        names = {row["name"]: row for row in summary["heartbeats"]}
        self.assertEqual(names["care"]["job_name"], "proactive-heartbeats-care")
        self.assertEqual(names["infra"]["job_name"], "proactive-heartbeats-infra")
        self.assertEqual(names["stale"]["action"], "removed")

        scripts = self.hermes_home / "scripts"
        care = (scripts / "proactive-heartbeats-care.sh").read_text(encoding="utf-8")
        infra = (scripts / "proactive-heartbeats-infra.sh").read_text(encoding="utf-8")
        self.assertIn("tick --name care", care)
        self.assertIn("tick --name infra", infra)
        self.assertIn(mock_hermes, care)
        self.assertIn(mock_hermes, infra)
        self.assertFalse((scripts / "proactive-heartbeats-stale.sh").exists())

        created = [args for args in calls if args[:2] == ["cron", "create"]]
        self.assertEqual(
            {args[5] for args in created},
            {"proactive-heartbeats-care", "proactive-heartbeats-infra"},
        )
        self.assertIn(["cron", "remove", "proactive-heartbeats-stale"], calls)
