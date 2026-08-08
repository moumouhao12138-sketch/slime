"""TSec Benchmark Platform control-plane client.

The platform credential deliberately stays in this module and never becomes
part of a Project scope, Worker prompt, Blackboard event, or exported result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
import os
from pathlib import Path
from typing import Any, Mapping

import httpx


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
class BenchmarkSettings:
    base_url: str = ""
    token: str = field(default="", repr=False)
    timeout: float = 30.0

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.token)

    @property
    def task_key(self) -> str:
        if not self.token:
            return ""
        return sha256(self.token.encode("utf-8")).hexdigest()[:16]

    @classmethod
    def from_env(
        cls,
        environment: Mapping[str, str] | None = None,
        cwd: str | Path | None = None,
    ) -> "BenchmarkSettings":
        """Load process values first, then the dispatch/project ``.env`` file."""

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

        timeout_text = value("BENCHMARK_TIMEOUT", "30")
        try:
            timeout = float(timeout_text)
        except ValueError as exc:
            raise ValueError("BENCHMARK_TIMEOUT must be a number") from exc
        if timeout <= 0:
            raise ValueError("BENCHMARK_TIMEOUT must be greater than zero")
        return cls(
            base_url=value("BENCHMARK_BASE_URL").rstrip("/"),
            token=value("BENCHMARK_TOKEN"),
            timeout=timeout,
        )

    def public_status(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "base_url": self.base_url,
            "token_configured": bool(self.token),
            "task_key": self.task_key,
            "timeout": self.timeout,
        }


class BenchmarkError(RuntimeError):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        detail: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = int(status_code)
        self.code = str(code or "benchmark_error")
        self.message = str(message or self.code)
        self.detail = {} if detail is None else detail

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "detail": self.detail}


class BenchmarkClient:
    API_ROOT = "/openapi/v1/challenges"

    def __init__(
        self,
        settings: BenchmarkSettings,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not settings.configured:
            raise ValueError("BENCHMARK_BASE_URL and BENCHMARK_TOKEN are required")
        self.settings = settings
        self._client = httpx.Client(
            base_url=settings.base_url,
            timeout=settings.timeout,
            headers={"BENCHMARK_TOKEN": settings.token, "Accept": "application/json"},
            transport=transport,
        )

    def close_client(self) -> None:
        self._client.close()

    def __enter__(self) -> "BenchmarkClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close_client()

    def _request(
        self,
        method: str,
        path: str = "",
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        try:
            response = self._client.request(
                method,
                f"{self.API_ROOT}{path}",
                params=params,
                json=json_body,
            )
        except httpx.TimeoutException as exc:
            raise BenchmarkError(504, "benchmark_timeout", "Benchmark platform request timed out") from exc
        except httpx.HTTPError as exc:
            raise BenchmarkError(502, "benchmark_unreachable", "Benchmark platform request failed") from exc

        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise BenchmarkError(
                502,
                "invalid_platform_response",
                f"Benchmark platform returned non-JSON HTTP {response.status_code}",
            ) from exc

        if response.is_error:
            if isinstance(payload, dict) and isinstance(payload.get("detail"), list):
                messages = [
                    str(item.get("msg", "validation error"))
                    for item in payload["detail"]
                    if isinstance(item, dict)
                ]
                raise BenchmarkError(
                    response.status_code,
                    "validation_error",
                    "; ".join(messages) or "Benchmark request validation failed",
                    payload.get("detail"),
                )
            if isinstance(payload, dict):
                raise BenchmarkError(
                    response.status_code,
                    str(payload.get("code") or "benchmark_error"),
                    str(payload.get("message") or f"Benchmark HTTP {response.status_code}"),
                    payload.get("detail", {}),
                )
            raise BenchmarkError(
                response.status_code,
                "benchmark_error",
                f"Benchmark HTTP {response.status_code}",
                payload,
            )
        return payload

    def list_challenges(self) -> list[dict[str, Any]]:
        payload = self._request("GET")
        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            raise BenchmarkError(502, "invalid_platform_response", "Challenge list must be an array")
        return [dict(item) for item in payload]

    def start(self, unique_code: str) -> dict[str, Any]:
        payload = self._request("POST", "/start", params={"unique_code": unique_code})
        return self._required_object(payload, "start")

    def hint(self, unique_code: str) -> dict[str, Any]:
        payload = self._request("GET", "/hint", params={"unique_code": unique_code})
        return self._required_object(payload, "hint")

    def submit(self, unique_code: str, flag: str) -> dict[str, Any]:
        payload = self._request(
            "POST",
            "/submit",
            json_body={"unique_code": unique_code, "flag": flag},
        )
        return self._required_object(payload, "submit")

    def close(self, unique_code: str) -> dict[str, Any]:
        payload = self._request("POST", "/close", params={"unique_code": unique_code})
        return self._required_object(payload, "close")

    @staticmethod
    def _required_object(payload: Any, operation: str) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise BenchmarkError(
                502,
                "invalid_platform_response",
                f"Benchmark {operation} response must be an object",
            )
        return dict(payload)
