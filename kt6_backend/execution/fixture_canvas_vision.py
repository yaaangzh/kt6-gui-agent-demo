from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..vision_recognition import CanvasFrame


class ExecutionFixtureVisionError(RuntimeError):
    pass


PixelLoader = Callable[[Path], tuple[int, int, Sequence[tuple[int, int, int]]]]


class ExecutionFixtureCanvasVisionAdapter:
    """Small real-pixel detector for the repository execution fixture."""

    adapter_id = "execution-fixture-canvas-cv"
    adapter_version = "1.0"
    supports_actionable_grounding = False
    _COLORS = {
        "ap_001": {(32, 166, 122), (255, 159, 26)},
        "switch_001": {(36, 107, 253)},
        "ap_002": {(138, 92, 245)},
    }

    def __init__(self, *, pixel_loader: PixelLoader | None = None):
        self._pixel_loader = pixel_loader or self._load_with_pillow

    def recognize(
        self,
        *,
        page: dict[str, Any],
        frames: tuple[CanvasFrame, ...],
    ) -> dict[str, Any]:
        parsed = urlsplit(str(page.get("url", "")).strip())
        if (
            parsed.scheme != "http"
            or (parsed.hostname or "").casefold() not in {"localhost", "127.0.0.1"}
            or parsed.path != "/execution-test.html"
            or len(frames) != 1
            or frames[0].canvas_id != "topology-canvas"
        ):
            raise ExecutionFixtureVisionError("execution_fixture_canvas_invalid")
        frame = frames[0]
        width, height, pixels = self._pixel_loader(frame.screenshot_path)
        if width != frame.width or height != frame.height or len(pixels) != width * height:
            raise ExecutionFixtureVisionError("execution_fixture_canvas_size_mismatch")
        objects = []
        for business_id, colors in self._COLORS.items():
            points = [
                (index % width, index // width)
                for index, pixel in enumerate(pixels)
                if tuple(pixel[:3]) in colors
            ]
            if len(points) < 80:
                raise ExecutionFixtureVisionError(
                    f"execution_fixture_object_missing:{business_id}"
                )
            xs = [point[0] for point in points]
            ys = [point[1] for point in points]
            left, top, right, bottom = min(xs), min(ys), max(xs), max(ys)
            objects.append(
                {
                    "business_id": business_id,
                    "type": "switch" if business_id.startswith("switch") else "ap",
                    "label": business_id.upper(),
                    "bbox": [left, top, right - left + 1, bottom - top + 1],
                    "confidence": 0.99,
                    "canvas_id": frame.canvas_id,
                    "attributes": {
                        "recognition_method": "fixture_color_component",
                        "matched_pixel_count": len(points),
                    },
                }
            )
        return {
            "confidence": 0.99,
            "objects": objects,
            "links": [
                {"source": "ap_001", "target": "switch_001", "type": "link"},
                {"source": "switch_001", "target": "ap_002", "type": "link"},
            ],
        }

    @staticmethod
    def _load_with_pillow(
        path: Path,
    ) -> tuple[int, int, Sequence[tuple[int, int, int]]]:
        try:
            from PIL import Image  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ExecutionFixtureVisionError(
                "Pillow is required for execution fixture Canvas perception"
            ) from exc
        try:
            with Image.open(path) as image:
                image.load()
                rgb = image.convert("RGB")
                return rgb.width, rgb.height, list(rgb.getdata())
        except (OSError, ValueError) as exc:
            raise ExecutionFixtureVisionError(
                "execution_fixture_canvas_decode_failed"
            ) from exc


__all__ = ["ExecutionFixtureCanvasVisionAdapter", "ExecutionFixtureVisionError"]
