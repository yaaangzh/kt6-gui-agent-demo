from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BrowserTarget:
    """A browser-observed CDP target that passed KT6 authorization checks."""

    node_id: str
    backend_node_id: int
    frame_id: str
    frame_url: str
    page_url: str
    dom_id: str
    owner_business_id: str
    action_id: str


@dataclass(frozen=True)
class CanvasTarget:
    """Pixel-grounded point inside a live, revalidated Canvas element."""

    node_id: str
    canvas_backend_node_id: int
    frame_id: str
    frame_url: str
    page_url: str
    canvas_dom_id: str
    asset_id: str
    x_ratio: float
    y_ratio: float
    producer_id: str


@dataclass(frozen=True)
class BrowserAction:
    """The fixed action vocabulary exposed to a browser runtime."""

    op: str
    target: BrowserTarget | CanvasTarget


@dataclass(frozen=True)
class BrowserExecutionResult:
    success: bool
    error_code: str
    backend_node_id: int | None = None
    x: float | None = None
    y: float | None = None


__all__ = [
    "BrowserAction",
    "BrowserExecutionResult",
    "BrowserTarget",
    "CanvasTarget",
]
