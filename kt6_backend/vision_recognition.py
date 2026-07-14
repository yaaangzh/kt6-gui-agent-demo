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
