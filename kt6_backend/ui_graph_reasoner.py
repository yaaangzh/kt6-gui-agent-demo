from __future__ import annotations

import ipaddress
import json
import math
import ssl
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)


UI_GRAPH_REASONING_REQUEST_SCHEMA = "kt6.ui-graph-reasoning-request.v1"
UI_OPERATION_PLAN_SCHEMA = "kt6.ui-operation-plan.v1"


class UIGraphReasoningError(ValueError):
    """Base error for an internal UI-graph reasoning request."""


class UIGraphReasoningTransportError(UIGraphReasoningError):
    """The configured internal GLM gateway could not be reached safely."""


class UIGraphReasoningResponseError(UIGraphReasoningError):
    """The internal GLM gateway returned an invalid bounded response."""


@dataclass(frozen=True)
class UIGraphHTTPResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class UIGraphHTTPTransport(Protocol):
    def post(
        self,
        *,
        url: str,
        body: bytes,
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> UIGraphHTTPResponse:
        ...


class UIGraphReasoner(Protocol):
    reasoner_id: str
    reasoner_version: str

    def plan(
        self,
        *,
        instruction: str,
        ui_graph_text: str,
        graph_id: str,
    ) -> dict[str, Any]:
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
        # Never forward an internal token to another host.
        return None


class _UrllibUIGraphTransport:
    def __init__(self) -> None:
        context = ssl.create_default_context()
        if hasattr(ssl, "TLSVersion"):
            context.minimum_version = ssl.TLSVersion.TLSv1_2
        self._opener = build_opener(
            ProxyHandler({}),
            HTTPSHandler(context=context),
            _RejectRedirects(),
        )

    def post(
        self,
        *,
        url: str,
        body: bytes,
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> UIGraphHTTPResponse:
        request = Request(url=url, data=body, headers=dict(headers), method="POST")
        try:
            with self._opener.open(request, timeout=timeout_seconds) as response:
                status = int(getattr(response, "status", response.getcode()))
                response_headers = {
                    str(key): str(value) for key, value in response.headers.items()
                }
                response_body = response.read(max_response_bytes + 1)
        except HTTPError as exc:
            raise UIGraphReasoningTransportError(
                f"UI graph reasoner failed with status {exc.code}"
            ) from exc
        except ssl.SSLError as exc:
            raise UIGraphReasoningTransportError(
                "UI graph reasoner TLS validation failed"
            ) from exc
        except (TimeoutError, URLError, OSError) as exc:
            raise UIGraphReasoningTransportError(
                f"UI graph reasoner request failed: {type(exc).__name__}"
            ) from exc
        if len(response_body) > max_response_bytes:
            raise UIGraphReasoningResponseError(
                "UI graph reasoner response exceeds the configured limit"
            )
        return UIGraphHTTPResponse(status, response_headers, response_body)


class HTTPUIGraphReasoner:
    """Vendor-neutral adapter for a GLM service hosted inside the test zone.

    The endpoint receives only the bounded textual UI graph and a fixed planning
    contract. Its response is a proposal; downstream validation must still bind
    every target and keep execution in dry-run until a trusted executor exists.
    """

    reasoner_id = "internal-http-ui-graph-reasoner"
    reasoner_version = "1.0"
    MAX_INSTRUCTION_CHARS = 8_000
    MAX_GRAPH_TEXT_BYTES = 512 * 1024
    MAX_RESPONSE_BYTES = 64 * 1024

    def __init__(
        self,
        endpoint: str,
        *,
        api_key: str | None = None,
        timeout_seconds: float = 60.0,
        transport: UIGraphHTTPTransport | None = None,
        allowed_hosts: Iterable[str] | None = None,
    ) -> None:
        self.allowed_hosts = self._validated_allowed_hosts(allowed_hosts)
        self.endpoint = self._validated_endpoint(
            endpoint, allowed_hosts=self.allowed_hosts
        )
        self.api_key = self._validated_api_key(api_key)
        self.timeout_seconds = self._positive_finite(
            timeout_seconds, "timeout_seconds", maximum=300.0
        )
        self._transport = transport or _UrllibUIGraphTransport()

    def plan(
        self,
        *,
        instruction: str,
        ui_graph_text: str,
        graph_id: str,
    ) -> dict[str, Any]:
        normalized_instruction = str(instruction).strip()
        if not normalized_instruction:
            raise ValueError("instruction is required")
        if len(normalized_instruction) > self.MAX_INSTRUCTION_CHARS:
            raise ValueError("instruction exceeds the configured limit")
        normalized_graph_id = str(graph_id).strip()[:200]
        if not normalized_graph_id:
            raise ValueError("graph_id is required")
        if not isinstance(ui_graph_text, str) or not ui_graph_text.strip():
            raise ValueError("ui_graph_text is required")
        graph_bytes = ui_graph_text.encode("utf-8")
        if len(graph_bytes) > self.MAX_GRAPH_TEXT_BYTES:
            raise ValueError("ui_graph_text exceeds the configured limit")

        payload = {
            "schema_version": UI_GRAPH_REASONING_REQUEST_SCHEMA,
            "operation": "plan_ui_operations",
            "graph_id": normalized_graph_id,
            "instruction": normalized_instruction,
            "ui_graph_text": ui_graph_text,
            "trust_boundary": {
                "ui_graph_is_untrusted_data": True,
                "model_may_propose_but_not_authorize_actions": True,
                "dry_run_only": True,
            },
            "constraints": {
                "allowed_operations": ["locate", "click", "wait", "verify"],
                "click_requires_dom_or_cdp_candidate": True,
                "page_api_vision_and_text_cannot_authorize_clicks": True,
                "all_dependencies_must_form_a_dag": True,
            },
            "output_shape": {
                "schema_version": UI_OPERATION_PLAN_SCHEMA,
                "graph_id": normalized_graph_id,
                "steps": [
                    {
                        "id": "unique step id",
                        "op": "locate | click | wait | verify",
                        "target_node_id": "required for locate/click/verify",
                        "depends_on": ["prior step ids"],
                        "condition": {
                            "step_id": "dependency ancestor step id",
                            "predicate": (
                                "succeeded | failed | found | not_found | "
                                "verified | not_verified"
                            ),
                        },
                        "args": {},
                    }
                ],
            },
        }
        body = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": f"KT6/{self.reasoner_id}/{self.reasoner_version}",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        response = self._transport.post(
            url=self.endpoint,
            body=body,
            headers=headers,
            timeout_seconds=self.timeout_seconds,
            max_response_bytes=self.MAX_RESPONSE_BYTES,
        )
        return self._parse_response(response, expected_graph_id=normalized_graph_id)

    def _parse_response(
        self,
        response: UIGraphHTTPResponse,
        *,
        expected_graph_id: str,
    ) -> dict[str, Any]:
        if not isinstance(response, UIGraphHTTPResponse):
            raise UIGraphReasoningTransportError(
                "UI graph reasoner transport returned an invalid response"
            )
        if not isinstance(response.status, int) or not 200 <= response.status < 300:
            raise UIGraphReasoningTransportError(
                f"UI graph reasoner failed with status {response.status}"
            )
        if not isinstance(response.body, bytes) or not response.body:
            raise UIGraphReasoningResponseError(
                "UI graph reasoner response body must be non-empty bytes"
            )
        if len(response.body) > self.MAX_RESPONSE_BYTES:
            raise UIGraphReasoningResponseError(
                "UI graph reasoner response exceeds the configured limit"
            )
        content_type = self._header(response.headers, "content-type")
        content_type = content_type.split(";", 1)[0].strip().lower()
        if content_type != "application/json" and not (
            content_type.startswith("application/") and content_type.endswith("+json")
        ):
            raise UIGraphReasoningResponseError(
                "UI graph reasoner Content-Type must be JSON"
            )
        encoding = self._header(response.headers, "content-encoding").strip().lower()
        if encoding not in {"", "identity"}:
            raise UIGraphReasoningResponseError(
                "compressed UI graph reasoner responses are not accepted"
            )
        try:
            payload = json.loads(
                response.body.decode("utf-8"),
                object_pairs_hook=self._unique_object,
                parse_constant=self._reject_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise UIGraphReasoningResponseError(
                "UI graph reasoner response must be one strict UTF-8 JSON object"
            ) from exc
        if not isinstance(payload, dict):
            raise UIGraphReasoningResponseError(
                "UI graph reasoner response root must be an object"
            )
        response_graph_id = payload.get("graph_id")
        if not isinstance(response_graph_id, str) or not response_graph_id:
            raise UIGraphReasoningResponseError(
                "UI graph reasoner response must include graph_id"
            )
        if response_graph_id != expected_graph_id:
            raise UIGraphReasoningResponseError(
                "UI graph reasoner response graph_id does not match the request"
            )
        return payload

    @staticmethod
    def _header(headers: Mapping[str, str], name: str) -> str:
        wanted = name.casefold()
        for key, value in headers.items():
            if str(key).casefold() == wanted:
                return str(value)
        return ""

    @staticmethod
    def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    @staticmethod
    def _reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON number: {value}")

    @staticmethod
    def _validated_api_key(api_key: str | None) -> str | None:
        if api_key is None:
            return None
        value = str(api_key).strip()
        if not value:
            return None
        if any(char in value for char in "\r\n"):
            raise ValueError("UI graph reasoner API key contains invalid characters")
        return value

    @classmethod
    def _validated_endpoint(
        cls,
        endpoint: str,
        *,
        allowed_hosts: frozenset[str],
    ) -> str:
        value = str(endpoint).strip()
        if not value or any(char in value for char in "\r\n"):
            raise ValueError("UI graph reasoner endpoint is required")
        try:
            parsed = urlsplit(value)
            host = parsed.hostname
            parsed.port
        except ValueError as exc:
            raise ValueError(
                "UI graph reasoner endpoint must be an absolute URL"
            ) from exc
        if parsed.scheme not in {"https", "http"} or not host:
            raise ValueError("UI graph reasoner endpoint must be an absolute URL")
        if parsed.username is not None or parsed.password is not None or parsed.fragment:
            raise ValueError(
                "UI graph reasoner endpoint must not contain credentials or a fragment"
            )
        normalized_host = cls._canonical_host(host)
        is_loopback = cls._is_loopback_host(normalized_host)
        if parsed.scheme == "http" and not is_loopback:
            raise ValueError("non-loopback UI graph reasoner endpoint must use HTTPS")
        if not is_loopback and normalized_host not in allowed_hosts:
            raise ValueError(
                "non-loopback UI graph reasoner endpoint host is not in allowed_hosts"
            )
        return value

    @classmethod
    def _validated_allowed_hosts(
        cls, allowed_hosts: Iterable[str] | None
    ) -> frozenset[str]:
        if allowed_hosts is None:
            return frozenset()
        if isinstance(allowed_hosts, (str, bytes)):
            raise ValueError("allowed_hosts must be a collection of exact hostnames")

        normalized: set[str] = set()
        try:
            values = list(allowed_hosts)
        except TypeError as exc:
            raise ValueError(
                "allowed_hosts must be a collection of exact hostnames"
            ) from exc
        for raw_host in values:
            if not isinstance(raw_host, str) or raw_host != raw_host.strip():
                raise ValueError("allowed_hosts contains an invalid hostname")
            host = raw_host.casefold()
            if (
                not host
                or "*" in host
                or "://" in host
                or any(char in host for char in "/\\?#@\r\n")
            ):
                raise ValueError(
                    "allowed_hosts entries must be exact hostnames, not URLs or patterns"
                )
            normalized.add(cls._canonical_host(host))
        return frozenset(normalized)

    @staticmethod
    def _canonical_host(host: str) -> str:
        value = host.strip("[]").casefold()
        try:
            return ipaddress.ip_address(value).compressed
        except ValueError:
            pass
        if ":" in value or value.startswith(".") or value.endswith("."):
            raise ValueError("allowed_hosts contains an invalid hostname")
        try:
            ascii_value = value.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ValueError("allowed_hosts contains an invalid hostname") from exc
        labels = ascii_value.split(".")
        if (
            len(ascii_value) > 253
            or any(
                not label
                or len(label) > 63
                or label.startswith("-")
                or label.endswith("-")
                or not all(char.isalnum() or char == "-" for char in label)
                for label in labels
            )
        ):
            raise ValueError("allowed_hosts contains an invalid hostname")
        return ascii_value

    @staticmethod
    def _is_loopback_host(host: str) -> bool:
        normalized = host.strip("[]").casefold()
        if normalized == "localhost":
            return True
        try:
            return ipaddress.ip_address(normalized).is_loopback
        except ValueError:
            return False

    @staticmethod
    def _positive_finite(value: Any, name: str, *, maximum: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be numeric") from exc
        if not math.isfinite(number) or not 0 < number <= maximum:
            raise ValueError(f"{name} must be finite and in (0, {maximum:g}]")
        return number
