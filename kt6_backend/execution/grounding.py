from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from ..asset_inventory import compact_text
from ..ui_graph import SCHEMA_VERSION as UI_GRAPH_SCHEMA_VERSION
from .models import BrowserTarget, VisualTarget
from .semantic_target import matching_nodes


_SIMPLE_DOM_ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


class GroundingError(ValueError):
    def __init__(self, error_code: str):
        super().__init__(error_code)
        self.error_code = error_code


class DOMGrounder:
    def resolve(
        self,
        target: Mapping[str, Any],
        graph: Mapping[str, Any],
    ) -> BrowserTarget:
        _require_safe_graph(graph)
        candidates = []
        for node in matching_nodes(target, graph, source_kinds=frozenset({"cdp"})):
            source = _mapping(node.get("source"))
            interaction = _mapping(node.get("interaction"))
            attributes = _mapping(node.get("attributes"))
            backend_id = source.get("backend_node_id")
            dom_id = compact_text(attributes.get("id"), 300)
            if (
                interaction.get("candidate") is True
                and interaction.get("status") == "candidate_only"
                and node.get("disabled") is not True
                and node.get("safe_for_execution") is False
                and node.get("can_click_now") is False
                and isinstance(backend_id, int)
                and not isinstance(backend_id, bool)
                and backend_id > 0
                and _SIMPLE_DOM_ID.fullmatch(dom_id)
            ):
                candidates.append((node, source, attributes, backend_id, dom_id))
        if len(candidates) != 1:
            raise GroundingError(
                "dom_grounding_target_missing"
                if not candidates
                else "dom_grounding_target_ambiguous"
            )
        node, source, attributes, backend_id, dom_id = candidates[0]
        page_url = _page_url(graph)
        frame_id = compact_text(source.get("frame_id"), 200)
        frame_url = compact_text(source.get("frame_url"), 2048) or page_url
        if not frame_id or not frame_url:
            raise GroundingError("dom_grounding_frame_missing")
        return BrowserTarget(
            node_id=compact_text(node.get("id"), 300),
            backend_node_id=backend_id,
            frame_id=frame_id,
            frame_url=frame_url,
            page_url=page_url,
            dom_id=dom_id,
            owner_business_id=compact_text(
                node.get("owner_business_id")
                or attributes.get("data-owner-business-id"),
                200,
            ),
            action_id=compact_text(
                node.get("action_id") or attributes.get("data-action-id"),
                200,
            ),
        )


class VisionGrounder:
    def __init__(self, *, producer_id: str | None):
        self.producer_id = compact_text(producer_id, 200)

    def resolve(
        self,
        target: Mapping[str, Any],
        graph: Mapping[str, Any],
    ) -> VisualTarget:
        _require_safe_graph(graph)
        if not self.producer_id:
            raise GroundingError("vision_grounding_not_configured")
        matches = []
        for node in matching_nodes(target, graph, source_kinds=frozenset({"vision"})):
            source = _mapping(node.get("source"))
            if (
                source.get("producer_id") == self.producer_id
                and _confidence(node.get("confidence")) >= 0.8
                and node.get("safe_for_execution") is False
            ):
                matches.append(node)
        if len(matches) != 1:
            raise GroundingError(
                "vision_grounding_target_missing"
                if not matches
                else "vision_grounding_target_ambiguous"
            )
        vision = matches[0]
        source = _mapping(vision.get("source"))
        bbox = vision.get("bbox")
        if not isinstance(bbox, list) or len(bbox) != 4:
            raise GroundingError("vision_grounding_geometry_invalid")
        try:
            x, y, width, height = (float(item) for item in bbox)
            canvas_width = float(source.get("canvas_width"))
            canvas_height = float(source.get("canvas_height"))
        except (TypeError, ValueError, OverflowError):
            raise GroundingError("vision_grounding_geometry_invalid") from None
        x_ratio = (x + width / 2) / canvas_width if canvas_width > 0 else 0
        y_ratio = (y + height / 2) / canvas_height if canvas_height > 0 else 0
        if not 0 < x_ratio < 1 or not 0 < y_ratio < 1:
            raise GroundingError("vision_grounding_geometry_invalid")
        canvas_id = compact_text(source.get("canvas_id"), 300)
        frame_id = compact_text(source.get("frame_id"), 200)
        page_url = _page_url(graph)
        canvas_matches = []
        for node in _nodes(graph):
            cdp_source = _mapping(node.get("source"))
            attributes = _mapping(node.get("attributes"))
            backend_id = cdp_source.get("backend_node_id")
            if (
                cdp_source.get("kind") == "cdp"
                and compact_text(attributes.get("id"), 300) == canvas_id
                and (not frame_id or cdp_source.get("frame_id") == frame_id)
                and isinstance(backend_id, int)
                and not isinstance(backend_id, bool)
                and backend_id > 0
            ):
                canvas_matches.append((node, cdp_source, backend_id))
        if len(canvas_matches) != 1:
            raise GroundingError(
                "canvas_element_missing"
                if not canvas_matches
                else "canvas_element_ambiguous"
            )
        _canvas_node, canvas_source, backend_id = canvas_matches[0]
        live_frame_id = compact_text(canvas_source.get("frame_id"), 200)
        frame_url = compact_text(canvas_source.get("frame_url"), 2048) or page_url
        if not live_frame_id or not frame_url:
            raise GroundingError("canvas_frame_mismatch")
        return VisualTarget(
            node_id=compact_text(vision.get("id"), 300),
            canvas_backend_node_id=backend_id,
            frame_id=live_frame_id,
            frame_url=frame_url,
            page_url=page_url,
            canvas_dom_id=canvas_id,
            asset_id=compact_text(target.get("asset_id") or target.get("query"), 200),
            x_ratio=round(x_ratio, 6),
            y_ratio=round(y_ratio, 6),
            producer_id=self.producer_id,
        )


class TargetGrounderRegistry:
    def __init__(self, *, vision_producer_id: str | None = None):
        self.dom = DOMGrounder()
        self.vision = VisionGrounder(producer_id=vision_producer_id)

    def resolve(
        self,
        target: Mapping[str, Any],
        graph: Mapping[str, Any],
    ) -> BrowserTarget | VisualTarget:
        try:
            return self.dom.resolve(target, graph)
        except GroundingError as exc:
            if exc.error_code != "dom_grounding_target_missing":
                raise
        return self.vision.resolve(target, graph)


def _require_safe_graph(graph: Mapping[str, Any]) -> None:
    if (
        graph.get("schema_version") != UI_GRAPH_SCHEMA_VERSION
        or graph.get("analysis_only") is not True
        or graph.get("execution_authorized") is not False
        or graph.get("safe_for_execution") is not False
        or _mapping(graph.get("stats")).get("truncated") is True
    ):
        raise GroundingError("grounding_ui_graph_invalid")


def _page_url(graph: Mapping[str, Any]) -> str:
    value = compact_text(_mapping(graph.get("page")).get("url"), 2048)
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise GroundingError("grounding_page_invalid")
    return value


def _nodes(graph: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = graph.get("nodes")
    if not isinstance(raw, list):
        raise GroundingError("grounding_ui_graph_nodes_missing")
    return [node for node in raw if isinstance(node, Mapping)]


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _confidence(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0


__all__ = [
    "DOMGrounder",
    "GroundingError",
    "TargetGrounderRegistry",
    "VisionGrounder",
]
