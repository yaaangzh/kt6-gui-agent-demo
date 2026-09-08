"""Bounded OpenAI-compatible chat-completions client used by evaluations.

The project deliberately keeps this adapter dependency-free.  Scheme-specific
executors can therefore talk to DeepSeek, an approved internal gateway, or a
UI-TARS model server without importing a vendor SDK into the KT6 runtime.
"""

from __future__ import annotations

import ipaddress
import json
import math
import ssl
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener


class ModelAPIError(RuntimeError):
    """Base error with a stable, non-sensitive classification."""

    error_code = "model_api_error"
    retryable = False


class ModelAPITransportError(ModelAPIError):
    error_code = "model_api_transport_error"
    retryable = True


class ModelAPIResponseError(ModelAPIError):
    error_code = "model_api_invalid_response"


@dataclass(frozen=True)
class ModelHTTPResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class ModelHTTPTransport(Protocol):
    def post(
        self,
        *,
        url: str,
        body: bytes,
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> ModelHTTPResponse:
        ...


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(  # type: ignore[override]
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Mapping[str, str],
        newurl: str,
    ) -> None:
        # A redirect must never receive the configured bearer credential.
        return None


class _UrllibModelTransport:
    def __init__(self) -> None:
        context = ssl.create_default_context()
        if hasattr(ssl, "TLSVersion"):
            context.minimum_version = ssl.TLSVersion.TLSv1_2
        self._opener = build_opener(HTTPSHandler(context=context), _RejectRedirects())

    def post(
        self,
        *,
        url: str,
        body: bytes,
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> ModelHTTPResponse:
        request = Request(url=url, data=body, headers=dict(headers), method="POST")
        try:
            with self._opener.open(request, timeout=timeout_seconds) as response:
                status = int(getattr(response, "status", response.getcode()))
                response_headers = {
                    str(key): str(value) for key, value in response.headers.items()
                }
                response_body = response.read(max_response_bytes + 1)
        except HTTPError as exc:
            error = ModelAPITransportError(
                f"model API request failed with status {exc.code}"
            )
            error.retryable = exc.code == 429 or 500 <= exc.code <= 599
            raise error from exc
        except ssl.SSLError as exc:
            raise ModelAPITransportError("model API TLS validation failed") from exc
        except (TimeoutError, URLError, OSError) as exc:
            raise ModelAPITransportError(
                f"model API request failed: {type(exc).__name__}"
            ) from exc
        if len(response_body) > max_response_bytes:
            raise ModelAPIResponseError("model API response exceeds configured limit")
        return ModelHTTPResponse(status, response_headers, response_body)


@dataclass(frozen=True)
class ChatCompletionResult:
    """Normalized response plus the bounded raw JSON needed for evidence."""

    response_id: str
    model: str
    content: str
    message: dict[str, Any]
    usage: dict[str, int]
    raw_response: dict[str, Any]

    def json_content(self) -> dict[str, Any]:
        try:
            payload = json.loads(
                self.content,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except (json.JSONDecodeError, RecursionError, ValueError) as exc:
            raise ModelAPIResponseError(
                "model API message content must be one strict JSON object"
            ) from exc
        if not isinstance(payload, dict):
            raise ModelAPIResponseError(
                "model API message content must be one strict JSON object"
            )
        return payload


class OpenAICompatibleChatClient:
    """Small fail-closed client for the common chat-completions protocol."""

    DEFAULT_MAX_REQUEST_BYTES = 24 * 1024 * 1024
    DEFAULT_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
    MAX_MESSAGES = 128
    MAX_MODEL_CHARS = 200

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 60.0,
        max_tokens: int = 4096,
        max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        allowed_hosts: Iterable[str] | None = None,
        transport: ModelHTTPTransport | None = None,
    ) -> None:
        self.allowed_hosts = self._allowed_hosts(allowed_hosts)
        self.endpoint = self._chat_completions_endpoint(
            base_url,
            allowed_hosts=self.allowed_hosts,
        )
        self.api_key = self._api_key(api_key)
        self.model = self._bounded_text(model, "model", self.MAX_MODEL_CHARS)
        self.timeout_seconds = self._positive_number(
            timeout_seconds, "timeout_seconds", 600.0
        )
        self.max_tokens = self._positive_int(max_tokens, "max_tokens", 131_072)
        self.max_request_bytes = self._positive_int(
            max_request_bytes, "max_request_bytes", 64 * 1024 * 1024
        )
        self.max_response_bytes = self._positive_int(
            max_response_bytes, "max_response_bytes", 16 * 1024 * 1024
        )
        self._transport = transport or _UrllibModelTransport()

    def complete(
        self,
        *,
        messages: Sequence[Mapping[str, Any]],
        json_mode: bool = False,
        temperature: float = 0.0,
        extra_body: Mapping[str, Any] | None = None,
    ) -> ChatCompletionResult:
        normalized_messages = self._messages(messages)
        normalized_temperature = self._temperature(temperature)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": normalized_messages,
            "max_tokens": self.max_tokens,
            "stream": False,
            "temperature": normalized_temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if extra_body:
            forbidden = {
                "model",
                "messages",
                "stream",
                "max_tokens",
                "temperature",
                "response_format",
            }
            overlap = forbidden.intersection(extra_body)
            if overlap:
                raise ValueError("extra_body must not override core request fields")
            payload.update(self._json_copy(dict(extra_body), "extra_body"))
        body = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(body) > self.max_request_bytes:
            raise ValueError("model API request exceeds configured limit")
        response = self._transport.post(
            url=self.endpoint,
            body=body,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "KT6/openai-compatible-evaluation/1.0",
            },
            timeout_seconds=self.timeout_seconds,
            max_response_bytes=self.max_response_bytes,
        )
        return self._parse_response(response)

    def _parse_response(self, response: ModelHTTPResponse) -> ChatCompletionResult:
        if not isinstance(response, ModelHTTPResponse):
            raise ModelAPITransportError("model API transport returned invalid response")
        if not isinstance(response.status, int) or not 200 <= response.status < 300:
            error = ModelAPITransportError(
                f"model API request failed with status {response.status}"
            )
            error.retryable = (
                isinstance(response.status, int)
                and (response.status == 429 or 500 <= response.status <= 599)
            )
            raise error
        content_type = self._header(response.headers, "content-type")
        media_type = content_type.split(";", 1)[0].strip().casefold()
        if media_type != "application/json" and not (
            media_type.startswith("application/") and media_type.endswith("+json")
        ):
            raise ModelAPIResponseError("model API Content-Type must be JSON")
        encoding = self._header(response.headers, "content-encoding").strip().casefold()
        if encoding not in {"", "identity"}:
            raise ModelAPIResponseError("compressed model API responses are not accepted")
        if not isinstance(response.body, bytes) or not response.body:
            raise ModelAPIResponseError("model API response body must be non-empty bytes")
        if len(response.body) > self.max_response_bytes:
            raise ModelAPIResponseError("model API response exceeds configured limit")
        try:
            payload = json.loads(
                response.body.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
            raise ModelAPIResponseError(
                "model API response must be strict UTF-8 JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise ModelAPIResponseError("model API response root must be an object")
        choices = payload.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise ModelAPIResponseError(
                "model API response must contain exactly one choice"
            )
        choice = choices[0]
        if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
            raise ModelAPIResponseError("model API choice must contain a message")
        finish_reason = choice.get("finish_reason")
        if finish_reason not in {None, "stop", "tool_calls"}:
            raise ModelAPIResponseError(
                "model API response did not finish with a complete message"
            )
        message = dict(choice["message"])
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ModelAPIResponseError("model API message content must be non-empty text")
        response_id = payload.get("id", "")
        response_model = payload.get("model", self.model)
        if not isinstance(response_id, str) or len(response_id) > 500:
            raise ModelAPIResponseError("model API response id is invalid")
        if not isinstance(response_model, str) or not response_model.strip():
            raise ModelAPIResponseError("model API response model is invalid")
        usage = self._usage(payload.get("usage", {}))
        return ChatCompletionResult(
            response_id=response_id,
            model=response_model.strip()[: self.MAX_MODEL_CHARS],
            content=content,
            message=message,
            usage=usage,
            raw_response=payload,
        )

    @classmethod
    def _chat_completions_endpoint(
        cls,
        value: str,
        *,
        allowed_hosts: frozenset[str],
    ) -> str:
        raw = cls._bounded_text(value, "base_url", 2048)
        try:
            parsed = urlsplit(raw)
            host = parsed.hostname
            parsed.port
        except ValueError as exc:
            raise ValueError("base_url must be an absolute URL") from exc
        if parsed.scheme not in {"https", "http"} or not host:
            raise ValueError("base_url must be an absolute URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("base_url must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("base_url must not contain a query or fragment")
        normalized_host = host.rstrip(".").casefold()
        if not cls._is_loopback(host) and normalized_host not in allowed_hosts:
            raise ValueError("remote model API host is not in allowed_hosts")
        path = parsed.path.rstrip("/")
        if not path.endswith("/chat/completions"):
            path = f"{path}/chat/completions"
        return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))

    @staticmethod
    def _is_loopback(host: str) -> bool:
        normalized = host.rstrip(".").casefold()
        if normalized == "localhost":
            return True
        try:
            return ipaddress.ip_address(normalized).is_loopback
        except ValueError:
            return False

    @classmethod
    def _allowed_hosts(cls, values: Iterable[str] | None) -> frozenset[str]:
        hosts: set[str] = set()
        for index, raw in enumerate(values or ()):
            host = cls._bounded_text(raw, f"allowed_hosts[{index}]", 253)
            normalized = host.rstrip(".").casefold()
            if any(char in normalized for char in ":/\\@?#[]"):
                raise ValueError("allowed_hosts must contain exact host names only")
            hosts.add(normalized)
        return frozenset(hosts)

    @staticmethod
    def _api_key(value: str) -> str:
        key = str(value).strip()
        if not key or len(key) > 8192 or any(char in key for char in "\r\n"):
            raise ValueError("api_key is required and must be bounded")
        return key

    @classmethod
    def _messages(
        cls, messages: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        if isinstance(messages, (str, bytes)) or not isinstance(messages, Sequence):
            raise ValueError("messages must be a bounded sequence")
        if not 1 <= len(messages) <= cls.MAX_MESSAGES:
            raise ValueError("messages must be a non-empty bounded sequence")
        normalized: list[dict[str, Any]] = []
        for index, message in enumerate(messages):
            if not isinstance(message, Mapping):
                raise ValueError(f"messages[{index}] must be an object")
            role = message.get("role")
            if role not in {"system", "user", "assistant", "tool"}:
                raise ValueError(f"messages[{index}].role is unsupported")
            if "content" not in message:
                raise ValueError(f"messages[{index}].content is required")
            normalized.append(cls._json_copy(dict(message), f"messages[{index}]"))
        return normalized

    @staticmethod
    def _json_copy(value: Any, field_name: str) -> Any:
        try:
            encoded = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            return json.loads(
                encoded,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except (TypeError, RecursionError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"{field_name} must contain strict JSON values") from exc

    @staticmethod
    def _temperature(value: Any) -> float:
        if isinstance(value, bool):
            raise ValueError("temperature must be between 0 and 2")
        try:
            result = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("temperature must be between 0 and 2") from exc
        if not math.isfinite(result) or not 0 <= result <= 2:
            raise ValueError("temperature must be between 0 and 2")
        return result

    @staticmethod
    def _positive_number(value: Any, name: str, maximum: float) -> float:
        if isinstance(value, bool):
            raise ValueError(f"{name} must be a positive finite number")
        try:
            result = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"{name} must be a positive finite number") from exc
        if not math.isfinite(result) or not 0 < result <= maximum:
            raise ValueError(f"{name} must be a positive finite number")
        return result

    @staticmethod
    def _positive_int(value: Any, name: str, maximum: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be a positive integer")
        if not 0 < value <= maximum:
            raise ValueError(f"{name} must be a positive integer")
        return value

    @staticmethod
    def _bounded_text(value: Any, name: str, maximum: int) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{name} must be text")
        normalized = value.strip()
        if (
            not normalized
            or len(normalized) > maximum
            or any(char in normalized for char in "\r\n")
        ):
            raise ValueError(f"{name} is required and must be bounded")
        return normalized

    @staticmethod
    def _usage(value: Any) -> dict[str, int]:
        if value is None:
            return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        if not isinstance(value, dict):
            raise ModelAPIResponseError("model API usage must be an object")

        def count(*names: str) -> int:
            raw = next((value[name] for name in names if name in value), 0)
            if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
                raise ModelAPIResponseError("model API token usage is invalid")
            return raw

        input_tokens = count("prompt_tokens", "input_tokens")
        output_tokens = count("completion_tokens", "output_tokens")
        total_tokens = count("total_tokens") or input_tokens + output_tokens
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
        }

    @staticmethod
    def _header(headers: Mapping[str, str], name: str) -> str:
        if not isinstance(headers, Mapping):
            raise ModelAPITransportError("model API transport returned invalid headers")
        wanted = name.casefold()
        for key, value in headers.items():
            if str(key).casefold() == wanted:
                return str(value)
        return ""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


__all__ = [
    "ChatCompletionResult",
    "ModelAPIError",
    "ModelAPIResponseError",
    "ModelAPITransportError",
    "ModelHTTPResponse",
    "ModelHTTPTransport",
    "OpenAICompatibleChatClient",
]
