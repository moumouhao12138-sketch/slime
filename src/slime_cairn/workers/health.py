from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener, urlopen

from .. import __version__


SUPPORTED_PROTOCOLS = frozenset(
    {
        "anthropic-messages",
        "openai-chat-completions",
        "openai-responses",
    }
)


@dataclass(frozen=True, slots=True)
class ModelEndpoint:
    """The explicit model endpoint used by one container-native Worker.

    The dispatcher probes this endpoint before it creates a project container.
    ``api_key`` is deliberately excluded from repr and every public diagnostic.
    """

    base_url: str
    api_key: str = field(repr=False)
    protocol: str = "openai-responses"

    def __post_init__(self) -> None:
        base_url = self.base_url.strip().rstrip("/")
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("model endpoint base_url must be an absolute http(s) URL")
        if not self.api_key.strip():
            raise ValueError("model endpoint api_key must not be empty")
        if self.protocol not in SUPPORTED_PROTOCOLS:
            raise ValueError(
                "model endpoint protocol must be one of: "
                + ", ".join(sorted(SUPPORTED_PROTOCOLS))
            )
        object.__setattr__(self, "base_url", base_url)

    def request_url(self) -> str:
        suffix = {
            "anthropic-messages": "/v1/messages",
            "openai-chat-completions": "/chat/completions",
            "openai-responses": "/responses",
        }[self.protocol]
        return f"{self.base_url}{suffix}"

    def public_config(self) -> dict[str, str]:
        return {
            "base_url": self.base_url,
            "protocol": self.protocol,
            "endpoint": self.request_url(),
        }

    def probe(
        self,
        model: str,
        timeout: float,
        environment: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Send a bounded real request to validate URL, Key, and model together."""

        model = model.strip()
        if not model:
            return {
                "healthy": False,
                **self.public_config(),
                "status": None,
                "detail": "model is not configured",
            }
        if timeout <= 0:
            raise ValueError("model healthcheck timeout must be greater than zero")

        payload, headers = self._request_payload(model)
        request = Request(
            self.request_url(),
            data=payload.encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            opener = _proxy_opener(environment)
            response_context = opener.open(request, timeout=timeout) if opener else urlopen(request, timeout=timeout)
            with response_context as response:
                status_value = getattr(response, "status", None)
                status = int(status_value if status_value is not None else response.getcode())
                response.read(4096)
        except HTTPError as exc:
            return {
                "healthy": False,
                **self.public_config(),
                "status": int(exc.code),
                "detail": _clip_response(exc),
            }
        except (URLError, OSError, TimeoutError) as exc:
            return {
                "healthy": False,
                **self.public_config(),
                "status": None,
                "detail": f"{type(exc).__name__}: {exc}"[:500],
            }
        return {
            "healthy": 200 <= status < 300,
            **self.public_config(),
            "status": status,
            "detail": "" if 200 <= status < 300 else f"HTTP {status}",
        }

    def _request_payload(self, model: str) -> tuple[str, dict[str, str]]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": f"slime-cairn/{__version__}",
        }
        if self.protocol == "anthropic-messages":
            headers["anthropic-version"] = "2023-06-01"
            return (
                '{"model":' + _json_string(model) + ',"max_tokens":10,"messages":[{"role":"user","content":"ping"}]}',
                headers,
            )
        if self.protocol == "openai-chat-completions":
            return (
                '{"model":' + _json_string(model) + ',"max_tokens":10,"messages":[{"role":"user","content":"ping"}]}',
                headers,
            )
        return (
            '{"model":' + _json_string(model) + ',"input":[{"role":"user","content":"ping"}],"stream":false}',
            headers,
        )


def _json_string(value: str) -> str:
    import json

    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def _clip_response(error: HTTPError) -> str:
    try:
        payload = error.read(4096).decode("utf-8", errors="replace")
    except OSError:
        payload = ""
    compact = " ".join(payload.split())
    return compact[:500] or f"HTTP {error.code}"


def _proxy_opener(environment: Mapping[str, str] | None) -> Any | None:
    values = dict(environment or {})
    all_proxy = values.get("all_proxy") or values.get("ALL_PROXY")
    http_proxy = values.get("http_proxy") or values.get("HTTP_PROXY") or all_proxy
    https_proxy = values.get("https_proxy") or values.get("HTTPS_PROXY") or all_proxy
    proxies = {
        scheme: value
        for scheme, value in {"http": http_proxy, "https": https_proxy}.items()
        if value
    }
    return build_opener(ProxyHandler(proxies)) if proxies else None
