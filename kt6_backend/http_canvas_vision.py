from __future__ import annotations

import ipaddress
import json
import math
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from .topology_vision_contract import (
    REQUEST_SCHEMA_VERSION,
    RESPONSE_SCHEMA_VERSION,
    CanvasVisionHTTPError,
    CanvasVisionResponseError,
    TopologyVisionContract,
)
from .vision_recognition import CanvasFrame


class CanvasVisionTransportError(CanvasVisionHTTPError):
    """The remote service could not be reached securely."""


@dataclass(frozen=True)
class HTTPVisionResponse:
    """Small transport-neutral HTTP response used by injectable transports."""

    status: int
    headers: Mapping[str, str]
    body: bytes


class HTTPVisionTransport(Protocol):
    def post(
        self,
        *,
        url: str,
        body: bytes,
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> HTTPVisionResponse:
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
        # In particular, never forward a Bearer token to a redirected host.
        return None


class _UrllibHTTPVisionTransport:
    """Verified-TLS, no-redirect implementation using only the standard library."""

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
    ) -> HTTPVisionResponse:
        request = Request(url=url, data=body, headers=dict(headers), method="POST")
        try:
            with self._opener.open(request, timeout=timeout_seconds) as response:
                status = int(getattr(response, "status", response.getcode()))
                response_headers = {
                    str(key): str(value) for key, value in response.headers.items()
                }
                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    try:
                        declared_length = int(content_length)
                    except ValueError as exc:
                        raise CanvasVisionResponseError(
                            "vision response has an invalid Content-Length"
                        ) from exc
                    if declared_length < 0 or declared_length > max_response_bytes:
                        raise CanvasVisionResponseError(
                            "vision response exceeds configured size limit"
                        )
                response_body = response.read(max_response_bytes + 1)
        except CanvasVisionHTTPError:
            raise
        except HTTPError as exc:
            raise CanvasVisionTransportError(
                f"vision HTTP request failed with status {exc.code}"
            ) from exc
        except ssl.SSLError as exc:
            raise CanvasVisionTransportError(
                "vision HTTPS certificate or TLS validation failed"
            ) from exc
        except (TimeoutError, URLError, OSError) as exc:
            raise CanvasVisionTransportError(
                f"vision HTTP request failed: {type(exc).__name__}"
            ) from exc

        if len(response_body) > max_response_bytes:
            raise CanvasVisionResponseError("vision response exceeds configured size limit")
        return HTTPVisionResponse(status=status, headers=response_headers, body=response_body)


class HTTPTopologyVisionAdapter:
    """Send persisted Canvas pixels to a vendor-neutral topology vision endpoint."""

    adapter_id = "http-topology-vision"
    adapter_version = "1.0"
    supports_actionable_grounding = False

    MAX_FRAMES = TopologyVisionContract.MAX_FRAMES
    MAX_OBJECTS = TopologyVisionContract.MAX_OBJECTS
    MAX_RELATIONS = TopologyVisionContract.MAX_RELATIONS
    DEFAULT_MAX_FRAME_BYTES = TopologyVisionContract.DEFAULT_MAX_FRAME_BYTES
    DEFAULT_MAX_TOTAL_FRAME_BYTES = TopologyVisionContract.DEFAULT_MAX_TOTAL_FRAME_BYTES
    DEFAULT_MAX_RESPONSE_BYTES = TopologyVisionContract.DEFAULT_MAX_RESPONSE_BYTES
    MAX_IMAGE_PIXELS = TopologyVisionContract.MAX_IMAGE_PIXELS

    def __init__(
        self,
        endpoint: str,
        api_key: str | None = None,
        timeout_seconds: float = 30.0,
        *,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
        max_total_frame_bytes: int = DEFAULT_MAX_TOTAL_FRAME_BYTES,
        transport: HTTPVisionTransport | None = None,
    ) -> None:
        self.endpoint = self._validated_endpoint(endpoint)
        self.api_key = self._validated_api_key(api_key)
        self.timeout_seconds = self._positive_finite(
            timeout_seconds, "timeout_seconds", maximum=300.0
        )
        self.contract = TopologyVisionContract(
            max_response_bytes=max_response_bytes,
            max_frame_bytes=max_frame_bytes,
            max_total_frame_bytes=max_total_frame_bytes,
        )
        # Preserve the adapter's established public limit attributes.
        self.max_response_bytes = self.contract.max_response_bytes
        self.max_frame_bytes = self.contract.max_frame_bytes
        self.max_total_frame_bytes = self.contract.max_total_frame_bytes
        self._transport = transport or _UrllibHTTPVisionTransport()

    def recognize(
        self,
        *,
        page: dict[str, Any],
        frames: tuple[CanvasFrame, ...],
    ) -> dict[str, Any]:
        request_payload, frame_dimensions = self._request_payload(page, frames)
        body = json.dumps(
            request_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": f"KT6/{self.adapter_id}/{self.adapter_version}",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            response = self._transport.post(
                url=self.endpoint,
                body=body,
                headers=headers,
                timeout_seconds=self.timeout_seconds,
                max_response_bytes=self.max_response_bytes,
            )
        except CanvasVisionHTTPError:
            raise
        except ssl.SSLError as exc:
            raise CanvasVisionTransportError(
                "vision HTTPS certificate or TLS validation failed"
            ) from exc
        except (TimeoutError, OSError) as exc:
            raise CanvasVisionTransportError(
                f"vision HTTP request failed: {type(exc).__name__}"
            ) from exc

        return self._parse_response(response, frame_dimensions)

    def _request_payload(
        self,
        page: dict[str, Any],
        frames: tuple[CanvasFrame, ...],
    ) -> tuple[dict[str, Any], dict[str, tuple[int, int]]]:
        # Preserve the old fail-first behavior for a non-object page while the
        # remaining page fields are normalized after frame validation.
        if not isinstance(page, dict):
            raise ValueError("page must be an object")
        prepared = self.contract.prepare_frames(frames)
        payload = {
            "schema_version": REQUEST_SCHEMA_VERSION,
            "task": self.contract.task_specification(),
            "page": self.contract.prepare_page(page),
            "frames": [frame.as_base64_payload() for frame in prepared.frames],
        }
        return payload, dict(prepared.frame_dimensions)

    def _parse_response(
        self,
        response: HTTPVisionResponse,
        frame_dimensions: dict[str, tuple[int, int]],
    ) -> dict[str, Any]:
        if not isinstance(response, HTTPVisionResponse):
            raise CanvasVisionTransportError(
                "vision transport returned an invalid response object"
            )
        if not isinstance(response.status, int) or not 200 <= response.status < 300:
            raise CanvasVisionTransportError(
                f"vision HTTP request failed with status {response.status}"
            )
        # Keep the original HTTP fail-first order; the shared contract repeats
        # these byte checks so non-HTTP transports receive the same protection.
        if not isinstance(response.body, bytes):
            raise CanvasVisionResponseError("vision response body must be bytes")
        if len(response.body) > self.max_response_bytes:
            raise CanvasVisionResponseError("vision response exceeds configured size limit")
        content_type = self._header(response.headers, "content-type").split(";", 1)[0]
        content_type = content_type.strip().lower()
        if content_type != "application/json" and not (
            content_type.startswith("application/") and content_type.endswith("+json")
        ):
            raise CanvasVisionResponseError("vision response Content-Type must be JSON")
        content_encoding = self._header(response.headers, "content-encoding").strip().lower()
        if content_encoding not in {"", "identity"}:
            raise CanvasVisionResponseError("compressed vision responses are not accepted")
        if not response.body:
            raise CanvasVisionResponseError("vision response body is empty")
        return self.contract.parse_response_bytes(response.body, frame_dimensions)

    @classmethod
    def _output_schema(cls) -> dict[str, Any]:
        return TopologyVisionContract.output_schema()

    @staticmethod
    def _image_dimensions(raw: bytes, mime_type: str) -> tuple[int, int]:
        return TopologyVisionContract.image_dimensions(raw, mime_type)

    @staticmethod
    def _png_dimensions(raw: bytes) -> tuple[int, int] | None:
        return TopologyVisionContract._png_dimensions(raw)

    @staticmethod
    def _jpeg_dimensions(raw: bytes) -> tuple[int, int] | None:
        return TopologyVisionContract._jpeg_dimensions(raw)

    @staticmethod
    def _webp_dimensions(raw: bytes) -> tuple[int, int] | None:
        return TopologyVisionContract._webp_dimensions(raw)

    def _read_frame(self, screenshot_path: Any) -> bytes:
        return self.contract._read_frame(Path(screenshot_path))

    @staticmethod
    def _validated_endpoint(endpoint: str) -> str:
        value = str(endpoint).strip()
        if not value or any(char in value for char in "\r\n"):
            raise ValueError("vision endpoint is required")
        parsed = urlsplit(value)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            raise ValueError("vision endpoint must be an absolute HTTPS URL")
        if parsed.username is not None or parsed.password is not None or parsed.fragment:
            raise ValueError("vision endpoint must not contain credentials or a fragment")
        if parsed.scheme == "http" and not HTTPTopologyVisionAdapter._is_loopback_host(
            parsed.hostname
        ):
            raise ValueError("remote vision endpoint must use HTTPS")
        return value

    @staticmethod
    def _validated_api_key(api_key: str | None) -> str | None:
        if api_key is None:
            return None
        value = str(api_key).strip()
        if not value:
            return None
        if len(value) > 8192 or any(char in value for char in "\r\n"):
            raise ValueError("api_key is invalid")
        return value

    @staticmethod
    def _is_loopback_host(host: str) -> bool:
        lowered = host.rstrip(".").lower()
        if lowered == "localhost" or lowered.endswith(".localhost"):
            return True
        try:
            return ipaddress.ip_address(lowered).is_loopback
        except ValueError:
            return False

    @staticmethod
    def _positive_finite(value: Any, field_name: str, maximum: float) -> float:
        if isinstance(value, bool):
            raise ValueError(f"{field_name} must be a positive number")
        try:
            result = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"{field_name} must be a positive number") from exc
        if not math.isfinite(result) or result <= 0 or result > maximum:
            raise ValueError(f"{field_name} must be between 0 and {maximum}")
        return result

    @staticmethod
    def _positive_int(value: Any, field_name: str, maximum: int) -> int:
        return TopologyVisionContract._positive_int(value, field_name, maximum)

    @staticmethod
    def _header(headers: Mapping[str, str], name: str) -> str:
        if not isinstance(headers, Mapping):
            raise CanvasVisionTransportError("vision transport returned invalid headers")
        lowered = name.lower()
        for key, value in headers.items():
            if str(key).lower() == lowered:
                return str(value)
        return ""


HTTPCanvasVisionAdapter = HTTPTopologyVisionAdapter


__all__ = [
    "CanvasVisionHTTPError",
    "CanvasVisionResponseError",
    "CanvasVisionTransportError",
    "HTTPCanvasVisionAdapter",
    "HTTPTopologyVisionAdapter",
    "HTTPVisionResponse",
    "HTTPVisionTransport",
    "REQUEST_SCHEMA_VERSION",
    "RESPONSE_SCHEMA_VERSION",
]
