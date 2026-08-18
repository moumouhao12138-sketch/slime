"""Client for the competition AI Agent API.

The competition AccessKey is deliberately held only by this control-plane
client. It is never copied into a Project, Worker environment, prompt, event,
or API response.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
import os
from pathlib import Path
from typing import Any, Mapping

import httpx

from ... import __version__


API_ROOT = "/slab-match/api/v1/agent"
SUCCESS_CODE = "00000"


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


@dataclass(frozen=True, slots=True)
class AgentMatchSettings:
    """Configuration for the competition control plane."""

    base_url: str = ""
    access_key: str = field(default="", repr=False)
    timeout: float = 30.0
    environment_poll_interval: float = 3.0
    environment_ready_timeout: float = 180.0

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.access_key)

    @property
    def task_key(self) -> str:
        if not self.access_key:
            return ""
        return sha256(self.access_key.encode("utf-8")).hexdigest()[:16]

    @classmethod
    def from_env(
        cls,
        environment: Mapping[str, str] | None = None,
        cwd: str | Path | None = None,
    ) -> "AgentMatchSettings":
        env = dict(os.environ if environment is None else environment)
        root = Path(cwd or Path.cwd()).resolve()
        candidates: list[Path] = []
        config_value = str(env.get("SLIME_DISPATCH_CONFIG", "")).strip()
        if config_value:
            config_path = Path(config_value)
            if not config_path.is_absolute():
                config_path = root / config_path
            candidates.append(config_path.resolve().parent / ".env")
        candidates.append(root / ".env")

        file_values: dict[str, str] = {}
        for candidate in candidates:
            for key, value in _read_env_file(candidate).items():
                file_values.setdefault(key, value)

        def value(name: str, default: str = "") -> str:
            process_value = str(env.get(name, "")).strip()
            if process_value:
                return process_value
            return str(file_values.get(name, default)).strip()

        def positive_number(name: str, default: str) -> float:
            try:
                number = float(value(name, default))
            except ValueError as exc:
                raise ValueError(f"{name} must be a number") from exc
            if number <= 0:
                raise ValueError(f"{name} must be greater than zero")
            return number

        base_url = value("AGENT_MATCH_BASE_URL").rstrip("/")
        if base_url.endswith(API_ROOT):
            base_url = base_url[: -len(API_ROOT)].rstrip("/")
        return cls(
            base_url=base_url,
            access_key=value("AGENT_MATCH_ACCESS_KEY"),
            timeout=positive_number("AGENT_MATCH_TIMEOUT", "30"),
            environment_poll_interval=positive_number("AGENT_MATCH_ENV_POLL_INTERVAL", "3"),
            environment_ready_timeout=positive_number("AGENT_MATCH_ENV_READY_TIMEOUT", "180"),
        )

    def public_status(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "base_url": self.base_url,
            "access_key_configured": bool(self.access_key),
            "task_key": self.task_key,
            "timeout": self.timeout,
            "environment_poll_interval": self.environment_poll_interval,
            "environment_ready_timeout": self.environment_ready_timeout,
        }


class AgentMatchError(RuntimeError):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        detail: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = int(status_code)
        self.code = str(code or "agent_match_error")
        self.message = str(message or self.code)
        self.detail = {} if detail is None else detail

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "detail": self.detail}


class AgentMatchClient:
    def __init__(
        self,
        settings: AgentMatchSettings,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not settings.configured:
            raise ValueError("AGENT_MATCH_BASE_URL and AGENT_MATCH_ACCESS_KEY are required")
        self.settings = settings
        self._client = httpx.Client(
            base_url=settings.base_url,
            timeout=settings.timeout,
            headers={
                "X-Agent-AccessKey": settings.access_key,
                "Accept": "application/json",
                "User-Agent": f"slime-cairn/{__version__}",
            },
            transport=transport,
        )

    def close_client(self) -> None:
        self._client.close()

    def __enter__(self) -> "AgentMatchClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close_client()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        allow_business_failure: bool = False,
    ) -> Any:
        try:
            response = self._client.request(
                method,
                f"{API_ROOT}{path}",
                params=params,
                json=json_body,
            )
        except httpx.TimeoutException as exc:
            raise AgentMatchError(504, "agent_match_timeout", "Competition API request timed out") from exc
        except httpx.HTTPError as exc:
            raise AgentMatchError(502, "agent_match_unreachable", "Competition API request failed") from exc

        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise AgentMatchError(
                502,
                "invalid_platform_response",
                f"Competition API returned non-JSON HTTP {response.status_code}",
            ) from exc
        if not isinstance(payload, dict):
            raise AgentMatchError(
                502,
                "invalid_platform_response",
                "Competition API response must be an object envelope",
                payload,
            )

        code = str(payload.get("code") or "")
        message = str(payload.get("message") or "")
        if code == SUCCESS_CODE and not response.is_error:
            return payload.get("data")

        failure = {
            "isCorrect": False,
            "platform_code": code or f"http_{response.status_code}",
            "platform_message": message or f"Competition API HTTP {response.status_code}",
            "platform_data": payload.get("data"),
        }
        if allow_business_failure and response.status_code < 500:
            return failure
        raise AgentMatchError(
            response.status_code if response.is_error else 409,
            failure["platform_code"],
            failure["platform_message"],
            payload.get("data"),
        )

    @staticmethod
    def _required_object(value: Any, operation: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise AgentMatchError(
                502,
                "invalid_platform_response",
                f"Competition API {operation} response data must be an object",
                value,
            )
        return dict(value)

    def match_info(self) -> dict[str, Any]:
        return self._required_object(self._request("GET", "/match/notice/match-info"), "match-info")

    def overview(self) -> dict[str, Any]:
        return self._required_object(self._request("GET", "/answer-panel/overview"), "overview")

    def list_exercises(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/ctf/exercise-list")
        if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
            raise AgentMatchError(
                502,
                "invalid_platform_response",
                "Competition API exercise-list response data must be an array",
                data,
            )
        return [dict(item) for item in data]

    def get_exercise(self, exercise_id: int) -> dict[str, Any]:
        return self._required_object(
            self._request("GET", "/ctf/exercise", params={"exerciseId": int(exercise_id)}),
            "exercise",
        )

    def build_environment(self, exercise_id: int) -> None:
        self._request("POST", "/ctf/build-exercise-env", json_body={"exerciseId": int(exercise_id)})

    def recover_environment(self, exercise_id: int) -> None:
        self._request("POST", "/ctf/recover-exercise-env", json_body={"exerciseId": int(exercise_id)})

    def submit(self, exercise_id: int, flag: str) -> dict[str, Any]:
        data = self._request(
            "POST",
            "/answer-panel/answer",
            json_body={"exerciseId": int(exercise_id), "flag": flag},
            allow_business_failure=True,
        )
        if isinstance(data, dict) and "platform_code" in data:
            message = str(data.get("platform_message") or "").casefold()
            incorrect_markers = (
                "wrong answer",
                "incorrect answer",
                "answer incorrect",
                "答案错误",
                "回答错误",
                "答案不正确",
                "flag错误",
                "flag不正确",
                "flag不对",
            )
            if not any(marker in message for marker in incorrect_markers):
                raise AgentMatchError(
                    409,
                    str(data.get("platform_code") or "answer_rejected"),
                    str(data.get("platform_message") or "Competition API rejected the answer request"),
                    data.get("platform_data"),
                )
            return data
        if isinstance(data, dict) and "isCorrect" in data:
            return {
                "isCorrect": bool(data.get("isCorrect")),
                "platform_code": SUCCESS_CODE,
                "platform_message": "",
                "platform_data": dict(data),
            }
        raise AgentMatchError(
            502,
            "invalid_platform_response",
            "Competition API answer response data must contain isCorrect",
            data,
        )

    def list_notices(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/match/notice/now-list")
        if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
            raise AgentMatchError(
                502,
                "invalid_platform_response",
                "Competition API notice list response data must be an array",
                data,
            )
        return [dict(item) for item in data]

    def notice_detail(self, notice_id: int) -> dict[str, Any]:
        return self._required_object(
            self._request("GET", "/match/notice/detail", params={"id": int(notice_id)}),
            "notice detail",
        )
