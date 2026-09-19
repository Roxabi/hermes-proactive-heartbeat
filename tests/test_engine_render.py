from __future__ import annotations

import json

from engine import TickResult
from models import Candidate
from tests.engine_fakes import action
from tests.isolation import IsolatedHomeTestCase


class TickResultRenderingTests(IsolatedHomeTestCase):
    def test_silent_render_is_exactly_one_compact_gate_line(self) -> None:
        result = TickResult(candidate=None, state={}, diagnostics={})

        rendered = result.render()

        self.assertEqual(rendered, '{"wakeAgent": false}')
        self.assertNotIn("\n", rendered)

    def test_wake_render_is_one_compact_candidate_object_without_gate_line(self) -> None:
        candidate = Candidate(
            collector="sense",
            fingerprint="care:water",
            action=action("water", 70),
            facts={"plant": "fern"},
            decision={"action": "water", "source": "rule"},
        )
        result = TickResult(candidate=candidate, state={}, diagnostics={})

        rendered = result.render()

        self.assertNotIn("\n", rendered)
        self.assertNotIn("wakeAgent", rendered)
        self.assertNotIn(": ", rendered)
        self.assertEqual(
            json.loads(rendered),
            {
                "heartbeat_candidate": {
                    "collector": "sense",
                    "fingerprint": "care:water",
                    "action": "water",
                    "priority": 70,
                    "inputs": [
                        {
                            "collector": "sense",
                            "fingerprint": "care:water",
                            "facts": {"plant": "fern"},
                            "decision": {
                                "action": "water",
                                "source": "rule",
                            },
                        }
                    ],
                    "decision": {
                        "action": "water",
                        "source": "rule",
                    },
                    "context": {},
                    "delivery": {
                        "instruction": "Deliver water",
                        "max_sentences": 2,
                    },
                }
            },
        )
