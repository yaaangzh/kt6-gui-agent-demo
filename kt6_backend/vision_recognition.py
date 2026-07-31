from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class CanvasFrame:
    """A persisted Canvas screenshot made available to a vision adapter."""

    canvas_id: str
    screenshot_path: Path
    screenshot_sha256: str
    mime_type: str
    width: int
    height: int
    client_width: float
    client_height: float
    bbox: tuple[float, float, float, float]
    source_kind: str = ""
    source_type: str = ""
    capture_method: str = ""
    source_ref: str = ""
    source_canvas_id: str = ""
    frame_id: str = ""
    frame_url: str = ""
    document_id: str = ""
    region_selector: str = ""
    primitive_count: int = 0
    device_pixel_ratio: float = 1.0
    coordinate_space: Any = None
    capture_kind: str = ""
    roi_status: str = ""
    source_region: Any = None
    source_pixel_region: Any = None
    source_frame_id: str = ""
    source_frame_url: str = ""
    visible_ratio: float = 0.0
    visible_capture_error: str = ""


class CanvasVisionAdapter(Protocol):
    """Recognize topology semantics from real, persisted Canvas pixels.

    Implementations return a topology-like mapping containing ``objects`` and
    optional ``links`` / ``co_channel_relations``.  Provenance is deliberately
    not accepted from the adapter: PagePerceptionService derives and stamps it
    from the frames that were actually supplied.
    """

    adapter_id: str
    adapter_version: str
    # Remote pixel recognition must remain analysis-only until a trusted adapter
    # also verifies business IDs against the production inventory.
    supports_actionable_grounding: bool

    def recognize(
        self,
        *,
        page: dict[str, Any],
        frames: tuple[CanvasFrame, ...],
    ) -> dict[str, Any] | None:
        ...
