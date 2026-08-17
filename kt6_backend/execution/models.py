from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BrowserTarget:
    """A browser-observed CDP target that passed KT6 authorization checks."""

    node_id: str
    backend_node_id: int
    frame_id: str
    page_url: str


@dataclass(frozen=True)
class BrowserAction:
    """The fixed action vocabulary exposed to a browser runtime."""

    op: str
    target: BrowserTarget


@dataclass(frozen=True)
class BrowserExecutionResult:
    success: bool
    error_code: str
    backend_node_id: int | None = None
    x: float | None = None
    y: float | None = None


__all__ = ["BrowserAction", "BrowserExecutionResult", "BrowserTarget"]
