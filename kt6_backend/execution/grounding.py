from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from ..asset_inventory import compact_text, strong_identity_key
from ..ui_graph import SCHEMA_VERSION as UI_GRAPH_SCHEMA_VERSION
from .models import CanvasTarget


class GroundingError(ValueError):
    def __init__(self, error_code: str):
        super().__init__(error_code)
        self.error_code = error_code


@dataclass(frozen=True)
class GroundedDOMStep:
    graph_id: str
    capture_id: str
    target_node_id: str
    asset_id: str
    action_id: str
    grounder_id: str = "dom_ui_graph"


class DOMGrounder:
    ACTION_IDS = {
        "open_asset_details": "device.details",
        "open_topology": "navigation.topology",
    }

    def resolve(
        self,
        target: Mapping[str, Any],
        graph: Mapping[str, Any],
    ) -> GroundedDOMStep:
        _require_safe_graph(graph)
        asset_id = compact_text(target.get("asset_id"), 200)
        action_id = compact_text(target.get("action"), 200)
        observed_action = self.ACTION_IDS.get(action_id)
        if not asset_id or observed_action is None:
            raise GroundingError("dom_grounding_target_invalid")
        matches = []
        for node in _nodes(graph):
            source = _mapping(node.get("source"))
            interaction = _mapping(node.get("interaction"))
            if (
                source.get("kind") == "cdp"
                and compact_text(node.get("action_id"), 200).casefold()
                == observed_action
                and strong_identity_key(
                    "asset_id", compact_text(node.get("owner_business_id"), 200)
                )
                == strong_identity_key("asset_id", asset_id)
                and interaction.get("candidate") is True
                and interaction.get("status") == "candidate_only"
                and node.get("disabled") is not True
                and node.get("safe_for_execution") is False
                and node.get("can_click_now") is False
            ):
                matches.append(node)
        if len(matches) != 1:
            raise GroundingError(
                "dom_grounding_target_missing"
                if not matches
                else "dom_grounding_target_ambiguous"
            )
        return GroundedDOMStep(
            graph_id=compact_text(graph.get("graph_id"), 300),
            capture_id=compact_text(graph.get("capture_id"), 200),
            target_node_id=compact_text(matches[0].get("id"), 300),
            asset_id=asset_id,
            action_id=action_id,
        )


class CanvasGrounder:
    """Resolve fixture CV pixels into a live Canvas-relative point."""

    producer_id = "execution-fixture-canvas-cv"
    canvas_id = "topology-canvas"

    def resolve(
        self,
        target: Mapping[str, Any],
        graph: Mapping[str, Any],
    ) -> CanvasTarget:
        _require_safe_graph(graph)
        if target.get("source") != "canvas" or target.get("action") != "select_canvas_asset":
            raise GroundingError("canvas_grounding_target_invalid")
        asset_id = compact_text(target.get("asset_id"), 200)
        page_url = compact_text(_mapping(graph.get("page")).get("url"), 2048)
        parsed = urlsplit(page_url)
        if (
            not asset_id
            or parsed.scheme != "http"
            or (parsed.hostname or "").casefold() not in {"localhost", "127.0.0.1"}
            or parsed.path != "/execution-test.html"
        ):
            raise GroundingError("canvas_grounding_page_not_allowed")

        vision_matches = []
        for node in _nodes(graph):
            source = _mapping(node.get("source"))
            if (
                source.get("kind") == "vision"
                and source.get("producer_id") == self.producer_id
                and source.get("canvas_id") == self.canvas_id
                and strong_identity_key(
                    "asset_id", compact_text(node.get("business_id"), 200)
                )
                == strong_identity_key("asset_id", asset_id)
                and _confidence(node.get("confidence")) >= 0.8
                and node.get("safe_for_execution") is False
            ):
                vision_matches.append(node)
        if len(vision_matches) != 1:
            raise GroundingError(
                "canvas_grounding_target_missing"
                if not vision_matches
                else "canvas_grounding_target_ambiguous"
            )
        vision = vision_matches[0]
        source = _mapping(vision.get("source"))
        bbox = vision.get("bbox")
        if not isinstance(bbox, list) or len(bbox) != 4:
            raise GroundingError("canvas_grounding_geometry_invalid")
        try:
            x, y, width, height = (float(item) for item in bbox)
            canvas_width = float(source.get("canvas_width"))
            canvas_height = float(source.get("canvas_height"))
        except (TypeError, ValueError, OverflowError):
            raise GroundingError("canvas_grounding_geometry_invalid") from None
        x_ratio = (x + width / 2) / canvas_width if canvas_width > 0 else 0
        y_ratio = (y + height / 2) / canvas_height if canvas_height > 0 else 0
        if not 0 < x_ratio < 1 or not 0 < y_ratio < 1:
            raise GroundingError("canvas_grounding_geometry_invalid")

        canvas_matches = []
        for node in _nodes(graph):
            node_source = _mapping(node.get("source"))
            attributes = _mapping(node.get("attributes"))
            backend_id = node_source.get("backend_node_id")
            if (
                node_source.get("kind") == "cdp"
                and compact_text(attributes.get("id"), 200) == self.canvas_id
                and isinstance(backend_id, int)
                and not isinstance(backend_id, bool)
                and backend_id > 0
            ):
                canvas_matches.append(node)
        if len(canvas_matches) != 1:
            raise GroundingError(
                "canvas_element_missing"
                if not canvas_matches
                else "canvas_element_ambiguous"
            )
        canvas_node = canvas_matches[0]
        canvas_source = _mapping(canvas_node.get("source"))
        frame_id = compact_text(canvas_source.get("frame_id"), 200)
        frame_url = compact_text(canvas_source.get("frame_url"), 2048)
        if not frame_id or frame_url != page_url:
            raise GroundingError("canvas_frame_mismatch")
        return CanvasTarget(
            node_id=compact_text(vision.get("id"), 300),
            canvas_backend_node_id=int(canvas_source["backend_node_id"]),
            frame_id=frame_id,
            frame_url=frame_url,
            page_url=page_url,
            canvas_dom_id=self.canvas_id,
            asset_id=asset_id,
            x_ratio=round(x_ratio, 6),
            y_ratio=round(y_ratio, 6),
            producer_id=self.producer_id,
        )


class TargetGrounderRegistry:
    def __init__(self):
        self.dom = DOMGrounder()
        self.canvas = CanvasGrounder()

    def resolve(
        self,
        target: Mapping[str, Any],
        graph: Mapping[str, Any],
    ) -> GroundedDOMStep | CanvasTarget:
        if target.get("source") == "canvas":
            return self.canvas.resolve(target, graph)
        return self.dom.resolve(target, graph)


def _require_safe_graph(graph: Mapping[str, Any]) -> None:
    if (
        graph.get("schema_version") != UI_GRAPH_SCHEMA_VERSION
        or graph.get("analysis_only") is not True
        or graph.get("execution_authorized") is not False
        or graph.get("safe_for_execution") is not False
        or _mapping(graph.get("stats")).get("truncated") is True
    ):
        raise GroundingError("grounding_ui_graph_invalid")


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
    "CanvasGrounder",
    "DOMGrounder",
    "GroundedDOMStep",
    "GroundingError",
    "TargetGrounderRegistry",
]
