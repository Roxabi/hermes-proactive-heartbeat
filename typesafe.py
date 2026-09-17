"""Minimal, optional TypeSafe System One HTTP client."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

DEFAULT_TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_TYPESAFE_MODEL = "jev-latest"
_MAX_RESPONSE_BYTES = 1_048_576

Questions = Mapping[str, Mapping[str, Any]] | Sequence[Mapping[str, Any]]


@dataclass(frozen=True)
class TypeSafeClient:
    """Evaluate a batch of independent questions with one System One request."""

    api_key: str | None = None
    url: str = DEFAULT_TYPESAFE_URL
    model: str = DEFAULT_TYPESAFE_MODEL
    timeout: float = 10.0

    def __post_init__(self) -> None:
        if self.api_key is None:
            object.__setattr__(self, "api_key", os.getenv("TYPESAFE_API_KEY"))

    def evaluate(
        self,
        state: str | list[Any] | Mapping[str, Any],
        questions: Questions,
    ) -> dict[str, Any] | None:
        """Return flat answers keyed by question id, or ``None`` when unavailable.

        Choice answers become their selected label string. Noul answers become the
        yes-probability float. Missing API key or recoverable transport/JSON failures
        return ``None``.
        """

        request_questions = _normalize_questions(questions)
        if not self.api_key or not str(self.api_key).strip() or not request_questions:
            return None

        try:
            body = json.dumps(
                {
                    "state": state,
                    "model": self.model,
                    "questions": request_questions,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            request = urllib.request.Request(
                self.url,
                data=body,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status = getattr(response, "status", 200)
                if not 200 <= int(status) < 300:
                    return None
                raw = response.read(_MAX_RESPONSE_BYTES + 1)

            if len(raw) > _MAX_RESPONSE_BYTES:
                return None
            payload = json.loads(raw.decode("utf-8"))
        except (
            json.JSONDecodeError,
            TypeError,
            UnicodeDecodeError,
            ValueError,
            TimeoutError,
            urllib.error.URLError,
            OSError,
        ):
            return None

        if not isinstance(payload, dict):
            return None
        answers = payload.get("answers")
        if not isinstance(answers, dict):
            return None

        flat = _flatten_answers(answers)
        return flat if flat is not None else None


def _normalize_questions(questions: Questions) -> dict[str, dict[str, Any]]:
    if isinstance(questions, Mapping):
        normalized: dict[str, dict[str, Any]] = {}
        for question_id, question in questions.items():
            if not isinstance(question, Mapping):
                continue
            body = {str(key): value for key, value in question.items() if key != "id"}
            normalized[str(question_id)] = body
        return normalized

    normalized = {}
    for question in questions:
        if not isinstance(question, Mapping) or "id" not in question:
            continue
        question_id = str(question["id"])
        body = {str(key): value for key, value in question.items() if key != "id"}
        normalized[question_id] = body
    return normalized


def _flatten_answers(answers: Mapping[str, Any]) -> dict[str, Any] | None:
    flat: dict[str, Any] = {}
    for question_id, answer in answers.items():
        value = _flatten_answer(answer)
        if value is None:
            return None
        flat[str(question_id)] = value
    return flat


def _flatten_answer(answer: Any) -> str | float | None:
    if isinstance(answer, str):
        return answer
    if isinstance(answer, bool):
        return None
    if isinstance(answer, (int, float)):
        return float(answer)
    if not isinstance(answer, Mapping):
        return None

    answer_type = answer.get("type")
    if answer_type == "choice":
        choice = answer.get("choice")
        return choice if isinstance(choice, str) else None
    if answer_type == "noul":
        noul = answer.get("noul")
        if isinstance(noul, bool) or not isinstance(noul, (int, float)):
            return None
        return float(noul)

    choice = answer.get("choice")
    if isinstance(choice, str):
        return choice
    noul = answer.get("noul")
    if isinstance(noul, (int, float)) and not isinstance(noul, bool):
        return float(noul)
    return None
