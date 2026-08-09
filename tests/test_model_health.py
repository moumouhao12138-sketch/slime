from __future__ import annotations

from io import BytesIO
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from slime_cairn import __version__
from slime_cairn.workers.health import ModelEndpoint


class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, size=-1):
        return b'{"id":"probe"}'


class _Opener:
    def __init__(self) -> None:
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        return _Response()


class ModelEndpointTests(unittest.TestCase):
    def test_openai_responses_probe_uses_expected_request_without_exposing_key(self):
        endpoint = ModelEndpoint("https://models.example/v1", "super-secret", "openai-responses")
        with patch("slime_cairn.workers.health.urlopen", return_value=_Response()) as request:
            result = endpoint.probe("model-a", 3)

        self.assertTrue(result["healthy"])
        self.assertEqual(result["endpoint"], "https://models.example/v1/responses")
        self.assertNotIn("super-secret", repr(result))
        sent = request.call_args.args[0]
        self.assertEqual(sent.get_full_url(), "https://models.example/v1/responses")
        self.assertEqual(sent.get_header("Authorization"), "Bearer super-secret")
        self.assertEqual(sent.get_header("User-agent"), f"slime-cairn/{__version__}")
        self.assertIn(b'"input"', sent.data)

    def test_http_authentication_failure_is_reported_without_key(self):
        endpoint = ModelEndpoint("https://models.example/v1", "top-secret", "openai-responses")
        failure = HTTPError(
            endpoint.request_url(),
            401,
            "Unauthorized",
            hdrs=None,
            fp=BytesIO(b'{"error":"invalid credential"}'),
        )
        with patch("slime_cairn.workers.health.urlopen", side_effect=failure):
            result = endpoint.probe("model-a", 3)

        self.assertFalse(result["healthy"])
        self.assertEqual(result["status"], 401)
        self.assertIn("invalid credential", result["detail"])
        self.assertNotIn("top-secret", repr(result))

    def test_anthropic_probe_uses_messages_protocol(self):
        endpoint = ModelEndpoint("https://models.example", "secret", "anthropic-messages")
        with patch("slime_cairn.workers.health.urlopen", return_value=_Response()) as request:
            result = endpoint.probe("claude-fixture", 3)

        self.assertTrue(result["healthy"])
        sent = request.call_args.args[0]
        self.assertEqual(sent.get_full_url(), "https://models.example/v1/messages")
        self.assertEqual(sent.get_header("Anthropic-version"), "2023-06-01")
        self.assertIn(b'"max_tokens":10', sent.data)

    def test_openai_chat_probe_uses_chat_completions_protocol(self):
        endpoint = ModelEndpoint(
            "https://models.example/v1",
            "secret",
            "openai-chat-completions",
        )
        with patch("slime_cairn.workers.health.urlopen", return_value=_Response()) as request:
            result = endpoint.probe("model-chat", 3)

        self.assertTrue(result["healthy"])
        sent = request.call_args.args[0]
        self.assertEqual(sent.get_full_url(), "https://models.example/v1/chat/completions")
        self.assertIn(b'"messages"', sent.data)
        self.assertNotIn(b'"input"', sent.data)

    def test_probe_uses_the_worker_proxy_environment(self):
        endpoint = ModelEndpoint("https://models.example/v1", "secret", "openai-responses")
        opener = _Opener()
        with patch("slime_cairn.workers.health.build_opener", return_value=opener) as build:
            result = endpoint.probe(
                "model-a",
                3,
                {"HTTPS_PROXY": "http://proxy.example:8080"},
            )

        self.assertTrue(result["healthy"])
        handler = build.call_args.args[0]
        self.assertEqual(handler.proxies, {"https": "http://proxy.example:8080"})
        self.assertEqual(len(opener.calls), 1)


if __name__ == "__main__":
    unittest.main()
