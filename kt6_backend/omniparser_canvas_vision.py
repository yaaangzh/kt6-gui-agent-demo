from __future__ import annotations

import base64
import copy
import hashlib
import ipaddress
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .vision_recognition import CanvasFrame, CanvasVisionAdapter


DEFAULT_OMNIPARSER_TIMEOUT_SECONDS = 120.0
MAX_OMNIPARSER_TIMEOUT_SECONDS = 600.0
DEFAULT_MAX_RESPONSE_BYTES = 48 * 1024 * 1024
MAX_ELEMENTS_PER_FRAME = 500
MAX_SOM_FRAMES = 4


class OmniParserVisionError(RuntimeError):
    """OmniParser preprocessing or model invocation failed."""


class OmniParserTransportError(OmniParserVisionError):
    """The OmniParser server could not be reached."""


class OmniParserResponseError(OmniParserVisionError):
    """The OmniParser server returned an invalid or oversized response."""


@dataclass(frozen=True)
class OmniParserHTTPResponse:
    status: int
    body: bytes


class OmniParserTransport(Protocol):
    def post(
        self,
        *,
        url: str,
        body: bytes,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> OmniParserHTTPResponse:
        ...


class _UrllibOmniParserTransport:
    """Minimal no-dependency JSON transport for the local OmniParser server."""

    def post(
        self,
        *,
        url: str,
        body: bytes,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> OmniParserHTTPResponse:
        request = Request(
            url=url,
            data=body,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "KT6/omniparser-vision/0.1",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                status = int(getattr(response, "status", response.getcode()))
                body = response.read(max_response_bytes + 1)
        except HTTPError as exc:
            raise OmniParserTransportError(
                f"omniparser HTTP request failed with status {exc.code}"
            ) from exc
        except (TimeoutError, URLError, OSError) as exc:
            raise OmniParserTransportError(
                f"omniparser HTTP request failed: {type(exc).__name__}"
            ) from exc
        if len(body) > max_response_bytes:
            raise OmniParserResponseError(
                "omniparser response exceeds configured size limit"
            )
        return OmniParserHTTPResponse(status=status, body=body)


class OmniParserCanvasVisionAdapter:
    """Feed a real OmniParser SoM screenshot plus structured elements to the GLM.

    The adapter calls a local ``omniparserserver`` (POST /parse/ with
    ``{"base64_image": ...}``), persists the returned Set-of-Mark image, and
    passes the marked frames plus ``parsed_content_list`` elements as
    ``cv_observations`` to the configured multimodal model.  It is a benchmark
    pilot: no silent fallback, so a missing or failing server surfaces loudly
    instead of producing invalid comparison data.
    """

    adapter_id = "omniparser-structured-multimodal"
    adapter_version = "0.1.0"
    supports_actionable_grounding = False

    def __init__(
        self,
        *,
        model_adapter: CanvasVisionAdapter,
        endpoint: str,
        workdir: Path,
        timeout_seconds: float = DEFAULT_OMNIPARSER_TIMEOUT_SECONDS,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        transport: OmniParserTransport | None = None,
    ) -> None:
        self.model_adapter = model_adapter
        self.endpoint = self._validated_endpoint(endpoint)
        self.workdir = Path(workdir).resolve()
        self.timeout_seconds = self._positive_finite(
            timeout_seconds,
            "timeout_seconds",
            maximum=MAX_OMNIPARSER_TIMEOUT_SECONDS,
        )
        self.max_response_bytes = self._positive_int(
            max_response_bytes,
            "max_response_bytes",
            maximum=128 * 1024 * 1024,
        )
        self._transport = transport or _UrllibOmniParserTransport()

    def recognize(
        self,
        *,
        page: dict[str, Any],
        frames: tuple[CanvasFrame, ...],
    ) -> dict[str, Any] | None:
        observations: dict[str, Any] = {
            "source": "omniparser_structured_elements",
            "objects": [],
            "links": [],
            "elements": [],
        }
        if not frames:
            return self._call_model(page, frames, observations)

        started = time.perf_counter()
        som_frames, frame_elements, parse_latency = self._parse_frames(frames)
        prep_ms = (time.perf_counter() - started) * 1000.0
        visible_elements = [
            copy.deepcopy(item)
            for items in frame_elements
            for item in items
            if not item.get("_skipped")
        ]
        observations = {
            "source": "omniparser_structured_elements",
            "objects": self._objects_from_elements(frame_elements),
            "links": [],
            "elements": visible_elements,
        }

        model_started = time.perf_counter()
        recognized = self._call_model(page, som_frames, observations)
        model_ms = (time.perf_counter() - model_started) * 1000.0
        if recognized is None or not isinstance(recognized, dict):
            return recognized

        result = copy.deepcopy(recognized)
        objects = result.get("objects", [])
        links = result.get("links", result.get("relations", []))
        text_count = sum(
            1
            for item in visible_elements
            if item.get("type") == "text"
        )
        icon_count = sum(
            1
            for item in visible_elements
            if item.get("type") == "icon"
        )
        result["omniparser_benchmark"] = {
            "mode": "omniparser_som",
            "endpoint": self.endpoint,
            "parsed": True,
            "frame_count": len(som_frames),
            "element_count": len(visible_elements),
            "text_count": text_count,
            "icon_count": icon_count,
            "skipped_elements": sum(
                item.get("_skipped", 0)
                for items in frame_elements
                for item in items
            ),
            "som_frames": [str(frame.screenshot_path.name) for frame in som_frames],
            "parse_latency_s": parse_latency,
            "prep_ms": round(prep_ms, 2),
            "model_ms": round(model_ms, 2),
            "total_ms": round(prep_ms + model_ms, 2),
            "glm_object_count": len(objects) if isinstance(objects, list) else 0,
            "glm_link_count": len(links) if isinstance(links, list) else 0,
        }
        return result

    def _parse_frames(
        self,
        frames: tuple[CanvasFrame, ...],
    ) -> tuple[
        tuple[CanvasFrame, ...],
        list[list[dict[str, Any]]],
        float | None,
    ]:
        self.workdir.mkdir(parents=True, exist_ok=True)
        som_frames: list[CanvasFrame] = []
        frame_elements: list[list[dict[str, Any]]] = []
        server_latency: float | None = None
        for index, frame in enumerate(frames[:MAX_SOM_FRAMES]):
            image_bytes = self._read_frame(frame)
            payload = json.dumps(
                {"base64_image": base64.b64encode(image_bytes).decode("ascii")},
                separators=(",", ":"),
            ).encode("utf-8")
            response = self._transport.post(
                url=self.endpoint,
                body=payload,
                timeout_seconds=self.timeout_seconds,
                max_response_bytes=self.max_response_bytes,
            )
            if not isinstance(response, OmniParserHTTPResponse):
                raise OmniParserTransportError(
                    "omniparser transport returned an invalid response object"
                )
            if not isinstance(response.status, int) or not (
                200 <= response.status < 300
            ):
                raise OmniParserTransportError(
                    f"omniparser HTTP request failed with status {response.status}"
                )
            parsed = self._parse_json_body(response.body)
            som_image = parsed.get("som_image_base64", parsed.get("som_image"))
            raw_elements = parsed.get("parsed_content_list")
            if not isinstance(som_image, str) or not som_image:
                raise OmniParserResponseError(
                    "omniparser response is missing som_image_base64"
                )
            if not isinstance(raw_elements, list):
                raise OmniParserResponseError(
                    "omniparser response is missing parsed_content_list"
                )
            latency_value = parsed.get("latency")
            if isinstance(latency_value, (int, float)) and not isinstance(
                latency_value, bool
            ):
                server_latency = float(latency_value)

            try:
                som_bytes = base64.b64decode(som_image, validate=True)
            except (ValueError, TypeError) as exc:
                raise OmniParserResponseError(
                    "omniparser returned an invalid SoM image encoding"
                ) from exc
            if not som_bytes:
                raise OmniParserResponseError("omniparser returned an empty SoM image")
            output_path = self.workdir / (
                f"som_{frame.canvas_id or 'frame'}_{index}.png"
            )
            output_path.write_bytes(som_bytes)
            som_frames.append(
                self._marked_frame_copy(
                    frame,
                    output_path=output_path,
                    capture_method=(
                        f"{frame.capture_method};omniparser_som"
                        if frame.capture_method
                        else "omniparser_som"
                    ),
                )
            )
            elements, skipped = self._normalize_elements(raw_elements, frame)
            frame_elements.append(elements)
            if skipped:
                frame_elements[-1].append({"_skipped": skipped})
        return tuple(som_frames), frame_elements, server_latency

    @staticmethod
    def _parse_json_body(body: bytes) -> dict[str, Any]:
        if not body:
            raise OmniParserResponseError("omniparser response body is empty")
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OmniParserResponseError(
                "omniparser response is not strict JSON"
            ) from exc
        if not isinstance(value, dict):
            raise OmniParserResponseError("omniparser response must be a JSON object")
        return value

    @staticmethod
    def _normalize_elements(
        raw_elements: list[Any],
        frame: CanvasFrame,
    ) -> tuple[list[dict[str, Any]], int]:
        normalized: list[dict[str, Any]] = []
        skipped = 0
        for index, item in enumerate(raw_elements[:MAX_ELEMENTS_PER_FRAME]):
            if not isinstance(item, Mapping):
                skipped += 1
                continue
            element_type = str(item.get("type", "unknown")).strip().lower()[:20]
            raw_content = item.get("content")
            content = (
                str(raw_content).strip()[:500] if raw_content is not None else ""
            )
            raw_source = item.get("source")
            try:
                bbox = OmniParserCanvasVisionAdapter._bbox_to_pixel(
                    item.get("bbox"),
                    width=frame.width,
                    height=frame.height,
                )
            except (TypeError, ValueError):
                skipped += 1
                continue
            element: dict[str, Any] = {
                "idx": index,
                "type": element_type or "unknown",
                "content": content,
                "bbox": bbox,
                "interactivity": bool(item.get("interactivity", False)),
            }
            if raw_source is not None:
                element["source"] = str(raw_source)[:100]
            normalized.append(element)
        return normalized, skipped

    @staticmethod
    def _bbox_to_pixel(
        bbox: Any,
        *,
        width: int,
        height: int,
    ) -> list[float]:
        if (
            not isinstance(bbox, (list, tuple))
            or len(bbox) != 4
            or any(isinstance(value, bool) for value in bbox)
            or any(not isinstance(value, (int, float)) for value in bbox)
        ):
            raise ValueError("element bbox must be [x1, y1, x2, y2]")
        values = [float(value) for value in bbox]
        if not all(math.isfinite(value) for value in values):
            raise ValueError("element bbox must be finite")
        if all(0.0 <= value <= 1.5 for value in values):
            # Official server returns normalized xyxy in [0, 1].
            x1, y1, x2, y2 = (
                values[0] * width,
                values[1] * height,
                values[2] * width,
                values[3] * height,
            )
        else:
            x1, y1, x2, y2 = values
        x = min(max(x1, 0.0), float(width))
        y = min(max(y1, 0.0), float(height))
        w = min(max(x2 - x1, 0.0), float(width) - x)
        h = min(max(y2 - y1, 0.0), float(height) - y)
        if w <= 0.0 or h <= 0.0:
            raise ValueError("element bbox has zero area")
        return [round(x, 3), round(y, 3), round(w, 3), round(h, 3)]

    def _objects_from_elements(
        self,
        frame_elements: list[list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        objects: list[dict[str, Any]] = []
        seen: set[str] = set()
        for items in frame_elements:
            for item in items:
                if item.get("_skipped"):
                    continue
                content = str(item.get("content", "")).strip()
                index = int(item.get("idx", 0))
                base_id = content if content and not self._has_whitespace(content) else (
                    f"som_{index}"
                )
                business_id = base_id[:100]
                if business_id in seen:
                    business_id = f"{base_id[:80]}#{index}"
                seen.add(business_id)
                element_type = str(item.get("type", "unknown"))
                objects.append(
                    {
                        "business_id": business_id,
                        "type": "ui_text" if element_type == "text" else (
                            "ui_icon" if element_type == "icon" else "omniparser_element"
                        ),
                        "label": content or business_id,
                        "bbox": list(item["bbox"]),
                        "confidence": 0.8 if element_type == "text" else 0.7,
                        "attributes": {
                            "recognizer": "omniparser_v2",
                            "omniparser_idx": index,
                            "omniparser_type": element_type,
                            "interactivity": bool(item.get("interactivity", False)),
                            "raw_content": content,
                        },
                    }
                )
        return objects

    @staticmethod
    def _has_whitespace(value: str) -> bool:
        return any(char.isspace() for char in value)

    @staticmethod
    def _marked_frame_copy(
        frame: CanvasFrame,
        *,
        output_path: Path,
        capture_method: str,
    ) -> CanvasFrame:
        digest = hashlib.sha256()
        with output_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return CanvasFrame(
            canvas_id=frame.canvas_id,
            screenshot_path=output_path,
            screenshot_sha256=digest.hexdigest(),
            mime_type="image/png",
            width=frame.width,
            height=frame.height,
            client_width=frame.client_width,
            client_height=frame.client_height,
            bbox=frame.bbox,
            source_kind=frame.source_kind,
            source_type=frame.source_type,
            capture_method=capture_method,
            source_ref=frame.source_ref,
            source_canvas_id=frame.source_canvas_id,
            frame_id=frame.frame_id,
            frame_url=frame.frame_url,
            document_id=frame.document_id,
            region_selector=frame.region_selector,
            primitive_count=frame.primitive_count,
            device_pixel_ratio=frame.device_pixel_ratio,
            coordinate_space=frame.coordinate_space,
            capture_kind=frame.capture_kind,
            roi_status=frame.roi_status,
            source_region=frame.source_region,
            source_pixel_region=frame.source_pixel_region,
            source_frame_id=frame.source_frame_id,
            source_frame_url=frame.source_frame_url,
            visible_ratio=frame.visible_ratio,
            visible_capture_error=frame.visible_capture_error,
        )

    def _call_model(
        self,
        page: dict[str, Any],
        frames: tuple[CanvasFrame, ...],
        observations: dict[str, Any],
    ) -> dict[str, Any] | None:
        contextual = getattr(self.model_adapter, "recognize_with_context", None)
        if callable(contextual):
            try:
                return contextual(
                    page=copy.deepcopy(page),
                    frames=frames,
                    cv_observations=copy.deepcopy(observations),
                )
            except TypeError:
                pass
        return self.model_adapter.recognize(
            page=copy.deepcopy(page),
            frames=frames,
        )

    @staticmethod
    def _read_frame(frame: CanvasFrame) -> bytes:
        try:
            return Path(frame.screenshot_path).read_bytes()
        except OSError as exc:
            raise OmniParserVisionError(
                f"cannot read canvas frame: {type(exc).__name__}"
            ) from exc

    @staticmethod
    def _validated_endpoint(endpoint: str) -> str:
        value = str(endpoint).strip()
        if not value or any(char in value for char in "\r\n"):
            raise ValueError("omniparser endpoint is required")
        parsed = urlsplit(value)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            raise ValueError(
                "omniparser endpoint must be an absolute HTTP(S) URL"
            )
        if parsed.username is not None or parsed.password is not None or parsed.fragment:
            raise ValueError("omniparser endpoint must not contain credentials or a fragment")
        if parsed.scheme == "http" and not OmniParserCanvasVisionAdapter._is_loopback_host(
            parsed.hostname
        ):
            raise ValueError("remote omniparser endpoint must use HTTPS")
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
            raise ValueError(f"{field_name} must be in (0, {maximum:g}]")
        return result

    @staticmethod
    def _positive_int(value: Any, field_name: str, maximum: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{field_name} must be an integer")
        if not 0 < value <= maximum:
            raise ValueError(f"{field_name} must be in (0, {maximum}]")
        return value


__all__ = [
    "DEFAULT_OMNIPARSER_TIMEOUT_SECONDS",
    "MAX_OMNIPARSER_TIMEOUT_SECONDS",
    "OmniParserCanvasVisionAdapter",
    "OmniParserHTTPResponse",
    "OmniParserResponseError",
    "OmniParserTransport",
    "OmniParserTransportError",
    "OmniParserVisionError",
]
