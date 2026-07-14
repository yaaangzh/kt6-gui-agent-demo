from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import struct
import sys
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen


MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_DIMENSION = 32_768
MAX_IMAGE_PIXELS = 100_000_000


class TopologyImageCLIError(ValueError):
    pass


def inspect_image(path: Path) -> tuple[str, int, int, bytes, str]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise TopologyImageCLIError(f"cannot read image: {path}") from exc
    if not raw:
        raise TopologyImageCLIError("image is empty")
    if len(raw) > MAX_IMAGE_BYTES:
        raise TopologyImageCLIError("image exceeds 5 MB")

    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        mime_type = "image/png"
        width, height = _png_dimensions(raw)
    elif raw.startswith(b"\xff\xd8"):
        mime_type = "image/jpeg"
        width, height = _jpeg_dimensions(raw)
    elif raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        mime_type = "image/webp"
        width, height = _webp_dimensions(raw)
    else:
        raise TopologyImageCLIError("only PNG, JPEG and WebP images are supported")

    if width <= 0 or height <= 0:
        raise TopologyImageCLIError("image dimensions must be positive")
    if width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION:
        raise TopologyImageCLIError("image dimensions exceed 32768 pixels")
    if width * height > MAX_IMAGE_PIXELS:
        raise TopologyImageCLIError("image exceeds 100 megapixels")
    return mime_type, width, height, raw, hashlib.sha256(raw).hexdigest()


def build_capture_payload(path: Path, source_id: str) -> tuple[dict[str, Any], str]:
    source_id = source_id.strip()
    if not source_id or len(source_id) > 200:
        raise TopologyImageCLIError("source-id must contain 1 to 200 characters")
    mime_type, width, height, raw, digest = inspect_image(path)
    data_url = f"data:{mime_type};base64,{base64.b64encode(raw).decode('ascii')}"
    stable_source = quote(source_id, safe="-._~")
    return (
        {
            "page": {
                "url": f"kt6://image-test/{stable_source}",
                "title": source_id,
                "language": "zh-CN",
                "ui_version": "topology-image-cli-v1",
                "viewport": {
                    "width": width,
                    "height": height,
                    "device_pixel_ratio": 1,
                },
            },
            # These fields are deliberately fixed so Renderer/DOM/text evidence
            # cannot win scene selection during a pixels-only acceptance test.
            "dom": {"elements": []},
            "canvases": [
                {
                    "canvas_id": "uploaded_topology",
                    "width": width,
                    "height": height,
                    "client_width": width,
                    "client_height": height,
                    "bbox": [0, 0, width, height],
                    "data_url": data_url,
                }
            ],
            "adapter_scene": None,
        },
        digest,
    )


def submit_capture(
    api_base: str,
    payload: dict[str, Any],
    *,
    timeout_seconds: float = 60.0,
    opener: Callable[..., Any] = urlopen,
) -> dict[str, Any]:
    parsed = urlparse(api_base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise TopologyImageCLIError("api-base must be an absolute HTTP(S) URL")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0 or timeout_seconds > 300:
        raise TopologyImageCLIError("timeout must be greater than 0 and at most 300 seconds")

    endpoint = api_base.rstrip("/") + "/api/perception/captures"
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = Request(
        endpoint,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with opener(request, timeout=timeout_seconds) as response:
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    declared_length = int(content_length)
                except ValueError as exc:
                    raise TopologyImageCLIError(
                        "capture API returned an invalid Content-Length"
                    ) from exc
                if declared_length < 0 or declared_length > MAX_RESPONSE_BYTES:
                    raise TopologyImageCLIError("capture API response exceeds 10 MB")
            raw_response = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        detail = exc.read(4096).decode("utf-8", errors="replace")
        raise TopologyImageCLIError(f"capture API returned HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise TopologyImageCLIError(f"capture API is unavailable: {exc.reason}") from exc
    except OSError as exc:
        raise TopologyImageCLIError(f"capture API request failed: {exc}") from exc
    if len(raw_response) > MAX_RESPONSE_BYTES:
        raise TopologyImageCLIError("capture API response exceeds 10 MB")
    try:
        result = json.loads(raw_response.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TopologyImageCLIError("capture API returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise TopologyImageCLIError("capture API response must be an object")
    return result


def acceptance_result(response: dict[str, Any], local_sha256: str) -> tuple[bool, dict[str, Any]]:
    scene = response.get("scene", {})
    summary = response.get("summary", {})
    provenance = scene.get("provenance", {}) if isinstance(scene, dict) else {}
    remote_hashes = provenance.get("screenshot_sha256", [])
    hash_matches = isinstance(remote_hashes, list) and local_sha256 in remote_hashes
    object_count = int(scene.get("object_count", 0)) if isinstance(scene, dict) else 0
    valid = bool(
        summary.get("selected_mode") == "canvas_vision_adapter"
        and provenance.get("semantic_source") == "canvas_pixels"
        and provenance.get("pixel_inference_performed") is True
        and provenance.get("pixel_verified") is True
        and provenance.get("adapter_id")
        and provenance.get("adapter_version")
        and hash_matches
        and object_count > 0
    )
    report = {
        "accepted_as_pixel_recognition": valid,
        "capture_id": response.get("capture_id"),
        "mode": summary.get("selected_mode"),
        "semantic_source": provenance.get("semantic_source"),
        "adapter_id": provenance.get("adapter_id"),
        "adapter_version": provenance.get("adapter_version"),
        "pixel_inference_performed": provenance.get("pixel_inference_performed"),
        "pixel_verified": provenance.get("pixel_verified"),
        "actionable_grounding": provenance.get("actionable_grounding"),
        "screenshot_sha256_matches": hash_matches,
        "object_count": object_count,
        "relation_count": scene.get("relation_count", 0) if isinstance(scene, dict) else 0,
        "elements": scene.get("elements", []) if isinstance(scene, dict) else [],
        "relations": scene.get("relations", []) if isinstance(scene, dict) else [],
        "semantic_tree": scene.get("semantic_tree", {}) if isinstance(scene, dict) else {},
        "issues": scene.get("issues", []) if isinstance(scene, dict) else [],
        "vision_error": scene.get("vision_error") if isinstance(scene, dict) else None,
    }
    return valid, report


def _png_dimensions(raw: bytes) -> tuple[int, int]:
    if len(raw) < 24 or raw[12:16] != b"IHDR":
        raise TopologyImageCLIError("invalid PNG header")
    return struct.unpack(">II", raw[16:24])


def _jpeg_dimensions(raw: bytes) -> tuple[int, int]:
    sof_markers = {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
    offset = 2
    while offset + 3 < len(raw):
        if raw[offset] != 0xFF:
            offset += 1
            continue
        while offset < len(raw) and raw[offset] == 0xFF:
            offset += 1
        if offset >= len(raw):
            break
        marker = raw[offset]
        offset += 1
        if marker in {0xD8, 0xD9, 0x01} or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(raw):
            break
        segment_length = struct.unpack(">H", raw[offset : offset + 2])[0]
        if segment_length < 2 or offset + segment_length > len(raw):
            raise TopologyImageCLIError("invalid JPEG segment")
        if marker in sof_markers:
            if segment_length < 7:
                raise TopologyImageCLIError("invalid JPEG size segment")
            height, width = struct.unpack(">HH", raw[offset + 3 : offset + 7])
            return width, height
        offset += segment_length
    raise TopologyImageCLIError("JPEG dimensions were not found")


def _webp_dimensions(raw: bytes) -> tuple[int, int]:
    if len(raw) < 30:
        raise TopologyImageCLIError("invalid WebP header")
    chunk = raw[12:16]
    if chunk == b"VP8X":
        width = int.from_bytes(raw[24:27], "little") + 1
        height = int.from_bytes(raw[27:30], "little") + 1
        return width, height
    if chunk == b"VP8 " and raw[23:26] == b"\x9d\x01\x2a":
        width = int.from_bytes(raw[26:28], "little") & 0x3FFF
        height = int.from_bytes(raw[28:30], "little") & 0x3FFF
        return width, height
    if chunk == b"VP8L" and raw[20] == 0x2F:
        b1, b2, b3, b4 = raw[21:25]
        width = 1 + b1 + ((b2 & 0x3F) << 8)
        height = 1 + (b2 >> 6) + (b3 << 2) + ((b4 & 0x0F) << 10)
        return width, height
    raise TopologyImageCLIError("unsupported WebP bitstream")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Submit one topology image through the KT6 pixels-only perception path."
    )
    parser.add_argument("image", type=Path)
    parser.add_argument("--api-base", default="http://127.0.0.1:8787")
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--timeout", type=float, default=60.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload, digest = build_capture_payload(args.image, args.source_id)
        response = submit_capture(args.api_base, payload, timeout_seconds=args.timeout)
        accepted, report = acceptance_result(response, digest)
        rendered = json.dumps(report, ensure_ascii=False, indent=2)
        print(rendered)
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(
                json.dumps(response, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        return 0 if accepted else 2
    except TopologyImageCLIError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 3
    except OSError as exc:
        print(
            json.dumps({"error": f"cannot write result: {exc}"}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
