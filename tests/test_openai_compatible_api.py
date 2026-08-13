from __future__ import annotations

import json
import unittest

from kt6_backend.openai_compatible_api import (
    ModelAPIResponseError,
    ModelAPITransportError,
    ModelHTTPResponse,
    OpenAICompatibleChatClient,
)


class StubTransport:
    def __init__(self, response: ModelHTTPResponse) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    def post(self, **kwargs):
        self.calls.append(dict(kwargs))
        return self.response


def response(payload: object, *, content_type: str = "application/json"):
    return ModelHTTPResponse(
        status=200,
        headers={"Content-Type": content_type},
        body=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
    )


class OpenAICompatibleChatClientTest(unittest.TestCase):
    def client(self, transport: StubTransport) -> OpenAICompatibleChatClient:
        return OpenAICompatibleChatClient(
            base_url="https://models.example.test/v1",
            api_key="secret-value",
            model="test-model",
            allowed_hosts=["models.example.test"],
            transport=transport,
        )

    def test_posts_bounded_chat_completion_without_exposing_key_in_body(self):
        transport = StubTransport(
            response(
                {
                    "id": "chat-1",
                    "model": "served-model",
                    "choices": [
                        {"message": {"role": "assistant", "content": '{"ok":true}'}}
                    ],
                    "usage": {
                        "prompt_tokens": 12,
                        "completion_tokens": 3,
                        "total_tokens": 15,
                    },
                }
            )
        )
        result = self.client(transport).complete(
            messages=[
                {"role": "system", "content": "Return JSON."},
                {"role": "user", "content": "test"},
            ],
            json_mode=True,
        )

        self.assertEqual(result.json_content(), {"ok": True})
        self.assertEqual(result.model, "served-model")
        self.assertEqual(result.usage["total_tokens"], 15)
        call = transport.calls[0]
        self.assertEqual(
            call["url"], "https://models.example.test/v1/chat/completions"
        )
        self.assertEqual(call["headers"]["Authorization"], "Bearer secret-value")
        self.assertNotIn(b"secret-value", call["body"])
        request_payload = json.loads(call["body"])
        self.assertEqual(request_payload["response_format"], {"type": "json_object"})

    def test_accepts_complete_endpoint_and_loopback_http(self):
        transport = StubTransport(
            response(
                {
                    "choices": [
                        {"message": {"role": "assistant", "content": "done"}}
                    ]
                }
            )
        )
        client = OpenAICompatibleChatClient(
            base_url="http://127.0.0.1:8000/v1/chat/completions",
            api_key="local",
            model="ui-tars",
            transport=transport,
        )
        client.complete(messages=[{"role": "user", "content": "go"}])
        self.assertEqual(
            transport.calls[0]["url"],
            "http://127.0.0.1:8000/v1/chat/completions",
        )

    def test_rejects_remote_plain_http_and_credentials_in_url(self):
        for endpoint in (
            "http://models.example.test/v1",
            "https://user:pass@models.example.test/v1",
            "https://models.example.test/v1?token=secret",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                OpenAICompatibleChatClient(
                    base_url=endpoint,
                    api_key="key",
                    model="model",
                    allowed_hosts=["models.example.test"],
                )

    def test_remote_endpoint_requires_exact_host_allowlist(self):
        for allowed_hosts in (None, [], ["other.example.test"], ["example.test"]):
            with self.subTest(allowed_hosts=allowed_hosts), self.assertRaises(ValueError):
                OpenAICompatibleChatClient(
                    base_url="https://models.example.test/v1",
                    api_key="key",
                    model="model",
                    allowed_hosts=allowed_hosts,
                )
        with self.assertRaises(ValueError):
            OpenAICompatibleChatClient(
                base_url="http://evil.localhost/v1",
                api_key="key",
                model="model",
            )

    def test_rejects_incomplete_finish_reason_even_when_content_is_valid_json(self):
        transport = StubTransport(
            response(
                {
                    "choices": [
                        {
                            "finish_reason": "length",
                            "message": {"content": '{"action":"click"}'},
                        }
                    ]
                }
            )
        )
        with self.assertRaises(ModelAPIResponseError):
            self.client(transport).complete(
                messages=[{"role": "user", "content": "go"}]
            )

    def test_injected_http_status_has_correct_retryability(self):
        for status, retryable in ((401, False), (429, True), (500, True)):
            with self.subTest(status=status):
                transport = StubTransport(
                    ModelHTTPResponse(
                        status=status,
                        headers={"Content-Type": "application/json"},
                        body=b"{}",
                    )
                )
                with self.assertRaises(ModelAPITransportError) as raised:
                    self.client(transport).complete(
                        messages=[{"role": "user", "content": "go"}]
                    )
                self.assertEqual(raised.exception.retryable, retryable)

    def test_rejects_duplicate_keys_and_nonfinite_response(self):
        bodies = (
            b'{"choices":[],"choices":[]}',
            b'{"choices":[],"value":NaN}',
            b'{"choices":[],"value":Infinity}',
        )
        for body in bodies:
            with self.subTest(body=body):
                transport = StubTransport(
                    ModelHTTPResponse(
                        status=200,
                        headers={"Content-Type": "application/json"},
                        body=body,
                    )
                )
                with self.assertRaises(ModelAPIResponseError):
                    self.client(transport).complete(
                        messages=[{"role": "user", "content": "go"}]
                    )

    def test_rejects_multiple_choices_empty_content_and_bad_usage(self):
        payloads = (
            {"choices": []},
            {
                "choices": [
                    {"message": {"content": "a"}},
                    {"message": {"content": "b"}},
                ]
            },
            {"choices": [{"message": {"content": ""}}]},
            {
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": -1},
            },
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                transport = StubTransport(response(payload))
                with self.assertRaises(ModelAPIResponseError):
                    self.client(transport).complete(
                        messages=[{"role": "user", "content": "go"}]
                    )

    def test_json_content_must_be_one_strict_object(self):
        for content in ("[]", "not-json", '{"x":NaN}', '{"x":1,"x":2}'):
            with self.subTest(content=content):
                transport = StubTransport(
                    response({"choices": [{"message": {"content": content}}]})
                )
                result = self.client(transport).complete(
                    messages=[{"role": "user", "content": "go"}]
                )
                with self.assertRaises(ModelAPIResponseError):
                    result.json_content()

    def test_rejects_core_overrides_and_nonfinite_request_values(self):
        transport = StubTransport(
            response({"choices": [{"message": {"content": "ok"}}]})
        )
        client = self.client(transport)
        with self.assertRaises(ValueError):
            client.complete(
                messages=[{"role": "user", "content": "go"}],
                extra_body={"model": "override"},
            )
        with self.assertRaises(ValueError):
            client.complete(
                messages=[{"role": "user", "content": float("nan")}]
            )

    def test_rejects_non_json_content_type_and_compression(self):
        valid_payload = {"choices": [{"message": {"content": "ok"}}]}
        responses = (
            response(valid_payload, content_type="text/plain"),
            ModelHTTPResponse(
                status=200,
                headers={
                    "Content-Type": "application/json",
                    "Content-Encoding": "gzip",
                },
                body=json.dumps(valid_payload).encode(),
            ),
        )
        for model_response in responses:
            with self.subTest(headers=model_response.headers):
                with self.assertRaises(ModelAPIResponseError):
                    self.client(StubTransport(model_response)).complete(
                        messages=[{"role": "user", "content": "go"}]
                    )


if __name__ == "__main__":
    unittest.main()
