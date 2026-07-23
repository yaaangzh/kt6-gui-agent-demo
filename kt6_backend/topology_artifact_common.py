from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .topology_image_cli import TopologyImageCLIError, inspect_image
from .vision_recognition import CanvasFrame


class TopologyArtifactCLIError(ValueError):
    """A standalone topology artifact command received invalid input."""


def build_image_input(
    image_path: Path,
    source_id: str,
) -> tuple[dict[str, Any], tuple[CanvasFrame, ...]]:
    """Build the same page/frame metadata used by the KT6 pixels-only path."""

    normalized_source = str(source_id).strip()
    if not normalized_source or len(normalized_source) > 200:
        raise TopologyArtifactCLIError(
            "source-id must contain 1 to 200 characters"
        )
    try:
        mime_type, width, height, _raw, digest = inspect_image(image_path)
    except TopologyImageCLIError as exc:
        raise TopologyArtifactCLIError(str(exc)) from exc

    stable_source = quote(normalized_source, safe="-._~")
    page = {
        "url": f"kt6://image-test/{stable_source}",
        "title": normalized_source,
        "language": "zh-CN",
        "ui_version": "topology-artifact-cli-v1",
        "viewport": {
            "width": width,
            "height": height,
            "device_pixel_ratio": 1,
        },
    }
    frame = CanvasFrame(
        canvas_id="uploaded_topology",
        screenshot_path=image_path.expanduser().resolve(),
        screenshot_sha256=digest,
        mime_type=mime_type,
        width=width,
        height=height,
        client_width=float(width),
        client_height=float(height),
        bbox=(0.0, 0.0, float(width), float(height)),
    )
    return page, (frame,)


def normalize_cv_context(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract bounded CV candidates from a raw result or public capture."""

    source: Any = payload
    if isinstance(payload.get("scene"), dict):
        source = payload["scene"]
    elif isinstance(payload.get("result"), dict):
        source = payload["result"]
    if not isinstance(source, dict):
        raise TopologyArtifactCLIError("CV JSON does not contain an object result")

    objects = source.get("objects", source.get("elements", []))
    links = source.get("links", source.get("relations", []))
    if not isinstance(objects, list) or not isinstance(links, list):
        raise TopologyArtifactCLIError(
            "CV JSON objects/elements and links/relations must be lists"
        )
    return {"objects": objects, "links": links}


def ensure_distinct_paths(*paths: Path | None) -> None:
    """Reject configurations that would overwrite an input or sibling artifact."""

    seen: dict[str, Path] = {}
    for path in paths:
        if path is None:
            continue
        resolved = path.expanduser().resolve()
        key = str(resolved).casefold()
        previous = seen.get(key)
        if previous is not None:
            raise TopologyArtifactCLIError(
                f"artifact paths must be distinct: {previous} and {path}"
            )
        seen[key] = path


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


__all__ = [
    "TopologyArtifactCLIError",
    "build_image_input",
    "ensure_distinct_paths",
    "normalize_cv_context",
    "write_json",
]
