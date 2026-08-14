"""Compact PNG overview for one topology-recognition run."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Any, Mapping, Sequence


PANEL_WIDTH = 760
PANEL_HEIGHT = 520
HEADER_HEIGHT = 72
FOOTER_HEIGHT = 48
CANVAS_MARGIN = 20


class TopologyProcessVisualizationError(ValueError):
    """Raised when a recognition overview cannot be rendered."""


def render_topology_process_overview(
    *,
    image_path: Path,
    cv_result: Mapping[str, Any],
    routing_result: Mapping[str, Any],
    model_result: Mapping[str, Any] | None,
    fused_result: Mapping[str, Any],
) -> bytes:
    """Render original, CV/OCR, model-routing and fused stages into one PNG."""

    Image, ImageDraw, ImageFont = _pillow()
    try:
        with Image.open(image_path) as opened:
            opened.load()
            source = opened.convert("RGB")
    except (OSError, ValueError) as exc:
        raise TopologyProcessVisualizationError(
            "recognition source image cannot be decoded"
        ) from exc

    cv_objects = _items(cv_result.get("objects"), "cv_result.objects")
    cv_links = _items(cv_result.get("links"), "cv_result.links")
    fused_payload = fused_result.get("result")
    if not isinstance(fused_payload, Mapping):
        raise TopologyProcessVisualizationError("fused_result.result must be an object")
    fused_objects = _items(fused_payload.get("objects"), "fused_result.objects")
    fused_links = _items(fused_payload.get("links"), "fused_result.links")
    decision = str(routing_result.get("decision", "unknown"))
    reason = str(routing_result.get("reason", "")).strip()
    model_nodes = _model_nodes(model_result)

    panels = [
        _panel(
            Image,
            ImageDraw,
            ImageFont,
            source,
            title="1  Original screenshot",
            subtitle=f"Input pixels  |  {source.width} x {source.height}",
        ),
        _panel(
            Image,
            ImageDraw,
            ImageFont,
            source,
            title="2  Local CV / OCR",
            subtitle=f"Detected {len(cv_objects)} objects and {len(cv_links)} pixel links",
            objects=cv_objects,
            links=cv_links,
            color=(255, 140, 0),
            label_mode="cv",
        ),
        _panel(
            Image,
            ImageDraw,
            ImageFont,
            source,
            title="3  Routing / semantic assist",
            subtitle=_bounded_text(
                f"Route: {decision}" + (f"  |  {reason}" if reason else ""),
                105,
            ),
            objects=cv_objects,
            links=_model_links(model_result),
            color=(180, 80, 180),
            label_mode="model",
            model_nodes=model_nodes,
        ),
        _panel(
            Image,
            ImageDraw,
            ImageFont,
            source,
            title="4  Deterministic fusion",
            subtitle=f"Final {len(fused_objects)} objects and {len(fused_links)} links",
            objects=fused_objects,
            links=fused_links,
            color=(45, 170, 70),
            label_mode="fused",
        ),
    ]
    overview = Image.new("RGB", (PANEL_WIDTH * 2 + 20, PANEL_HEIGHT * 2 + 20), (230, 230, 230))
    overview.paste(panels[0], (0, 0))
    overview.paste(panels[1], (PANEL_WIDTH + 20, 0))
    overview.paste(panels[2], (0, PANEL_HEIGHT + 20))
    overview.paste(panels[3], (PANEL_WIDTH + 20, PANEL_HEIGHT + 20))
    output = BytesIO()
    overview.save(output, format="PNG", compress_level=4)
    return output.getvalue()


def _panel(
    Image: Any,
    ImageDraw: Any,
    ImageFont: Any,
    source: Any,
    *,
    title: str,
    subtitle: str,
    objects: Sequence[Mapping[str, Any]] = (),
    links: Sequence[Mapping[str, Any]] = (),
    color: tuple[int, int, int] = (50, 120, 210),
    label_mode: str = "plain",
    model_nodes: Mapping[str, Mapping[str, Any]] | None = None,
) -> Any:
    panel = Image.new("RGB", (PANEL_WIDTH, PANEL_HEIGHT), (248, 248, 248))
    draw = ImageDraw.Draw(panel)
    draw.rectangle((0, 0, PANEL_WIDTH - 1, PANEL_HEIGHT - 1), outline=(210, 210, 210))
    draw.text((CANVAS_MARGIN, 13), title, fill=(30, 30, 30), font=ImageFont.load_default(size=22))
    draw.text(
        (CANVAS_MARGIN, 44),
        _bounded_text(subtitle, 112),
        fill=(90, 90, 90),
        font=ImageFont.load_default(size=14),
    )

    available_width = PANEL_WIDTH - CANVAS_MARGIN * 2
    available_height = PANEL_HEIGHT - HEADER_HEIGHT - FOOTER_HEIGHT
    scale = min(available_width / source.width, available_height / source.height)
    rendered_width = max(1, int(round(source.width * scale)))
    rendered_height = max(1, int(round(source.height * scale)))
    resampling = Image.Resampling.LANCZOS if scale < 1.0 else Image.Resampling.NEAREST
    fitted = source.resize((rendered_width, rendered_height), resample=resampling)
    offset_x = (PANEL_WIDTH - rendered_width) // 2
    offset_y = HEADER_HEIGHT + (available_height - rendered_height) // 2
    panel.paste(fitted, (offset_x, offset_y))
    draw.rectangle(
        (offset_x, offset_y, offset_x + rendered_width - 1, offset_y + rendered_height - 1),
        outline=(190, 190, 190),
    )

    geometry = _geometry(
        objects,
        offset_x=offset_x,
        offset_y=offset_y,
        scale=scale,
        image_width=source.width,
        image_height=source.height,
    )
    _draw_links(draw, links, geometry, color)
    _draw_objects(
        draw,
        objects,
        geometry,
        color,
        label_mode=label_mode,
        model_nodes=model_nodes or {},
        font=ImageFont.load_default(size=11),
    )
    return panel


def _geometry(
    objects: Sequence[Mapping[str, Any]],
    *,
    offset_x: int,
    offset_y: int,
    scale: float,
    image_width: int,
    image_height: int,
) -> dict[str, tuple[int, int, int, int]]:
    geometry: dict[str, tuple[int, int, int, int]] = {}
    for item in objects:
        object_id = str(item.get("business_id", "")).strip()
        bbox = item.get("bbox")
        if not object_id or not isinstance(bbox, list) or len(bbox) != 4:
            continue
        try:
            x, y, width, height = (float(value) for value in bbox)
        except (TypeError, ValueError):
            continue
        left = max(0.0, min(float(image_width), x))
        top = max(0.0, min(float(image_height), y))
        right = max(left, min(float(image_width), x + width))
        bottom = max(top, min(float(image_height), y + height))
        geometry[object_id] = (
            offset_x + int(round(left * scale)),
            offset_y + int(round(top * scale)),
            offset_x + max(1, int(round(right * scale))),
            offset_y + max(1, int(round(bottom * scale))),
        )
    return geometry


def _draw_links(
    draw: Any,
    links: Sequence[Mapping[str, Any]],
    geometry: Mapping[str, tuple[int, int, int, int]],
    color: tuple[int, int, int],
) -> None:
    for item in links:
        source_box = geometry.get(str(item.get("source", "")))
        target_box = geometry.get(str(item.get("target", "")))
        if source_box is None or target_box is None:
            continue
        source = ((source_box[0] + source_box[2]) // 2, (source_box[1] + source_box[3]) // 2)
        target = ((target_box[0] + target_box[2]) // 2, (target_box[1] + target_box[3]) // 2)
        draw.line((source, target), fill=color, width=3)


def _draw_objects(
    draw: Any,
    objects: Sequence[Mapping[str, Any]],
    geometry: Mapping[str, tuple[int, int, int, int]],
    color: tuple[int, int, int],
    *,
    label_mode: str,
    model_nodes: Mapping[str, Mapping[str, Any]],
    font: Any,
) -> None:
    for item in objects:
        object_id = str(item.get("business_id", "")).strip()
        box = geometry.get(object_id)
        if box is None:
            continue
        draw.rectangle(box, outline=color, width=3)
        label = _object_label(item, label_mode, model_nodes.get(object_id))
        bounded_label = _bounded_text(label, 42)
        text_box = draw.textbbox((0, 0), bounded_label, font=font, stroke_width=2)
        label_width = text_box[2] - text_box[0]
        label_x = max(4, min(box[0], PANEL_WIDTH - label_width - 4))
        draw.text(
            (label_x, max(HEADER_HEIGHT + 2, box[1] - 17)),
            bounded_label,
            fill=color,
            font=font,
            stroke_width=2,
            stroke_fill=(255, 255, 255),
        )


def _object_label(
    item: Mapping[str, Any],
    label_mode: str,
    model_node: Mapping[str, Any] | None,
) -> str:
    object_id = str(item.get("business_id", ""))
    if label_mode == "cv":
        confidence = item.get("confidence")
        suffix = f"  {float(confidence):.2f}" if isinstance(confidence, (int, float)) else ""
        return f"CV  {object_id}{suffix}"
    if label_mode == "model":
        if not model_node:
            return f"CV anchor  {object_id}"
        semantic = " / ".join(
            value
            for value in (
                str(model_node.get("type", "")).strip(),
                str(model_node.get("role", "")).strip(),
            )
            if value
        )
        return f"MODEL  {object_id}" + (f"  {semantic}" if semantic else "")
    if label_mode == "fused":
        attributes = item.get("attributes")
        semantics = attributes.get("model_semantics") if isinstance(attributes, Mapping) else None
        role = str(semantics.get("role", "")).strip() if isinstance(semantics, Mapping) else ""
        object_type = str(item.get("type", "")).strip()
        semantic = " / ".join(value for value in (object_type, role) if value)
        return f"FUSED  {object_id}" + (f"  {semantic}" if semantic else "")
    return object_id


def _items(value: Any, field: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        raise TopologyProcessVisualizationError(f"{field} must be a list")
    return [item for item in value if isinstance(item, Mapping)]


def _model_nodes(
    model_result: Mapping[str, Any] | None,
) -> dict[str, Mapping[str, Any]]:
    if model_result is None or not isinstance(model_result.get("nodes"), list):
        return {}
    return {
        str(item.get("id", "")): item
        for item in model_result["nodes"]
        if isinstance(item, Mapping) and str(item.get("id", ""))
    }


def _model_links(model_result: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    if model_result is None or not isinstance(model_result.get("links"), list):
        return []
    return [item for item in model_result["links"] if isinstance(item, Mapping)]


def _bounded_text(value: str, limit: int) -> str:
    normalized = " ".join(str(value).split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 3)] + "..."


def _pillow() -> tuple[Any, Any, Any]:
    try:
        from PIL import Image, ImageDraw, ImageFont  # type: ignore[import-not-found]
    except ImportError as exc:
        raise TopologyProcessVisualizationError(
            "process visualization requires the local vision dependencies"
        ) from exc
    return Image, ImageDraw, ImageFont


__all__ = [
    "TopologyProcessVisualizationError",
    "render_topology_process_overview",
]
