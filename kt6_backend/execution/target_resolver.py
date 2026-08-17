from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from ..asset_inventory import compact_text, strong_identity_key
from ..ui_graph import SCHEMA_VERSION as UI_GRAPH_SCHEMA_VERSION
from .models import BrowserTarget


_SIMPLE_ID_SELECTOR = re.compile(r"^#([A-Za-z_][A-Za-z0-9_-]*)$")


class TargetResolutionError(ValueError):
    def __init__(self, error_code: str):
        super().__init__(error_code)
        self.error_code = error_code


class UIGraphTargetResolver:
    """Bind one authorized DOM control to one fresh CDP backend node."""

    def resolve(
        self,
        graph: Mapping[str, Any],
        *,
        expected_graph_id: str,
        capture_id: str,
        target_node_id: str,
        control: Mapping[str, Any],
        asset_id: str,
    ) -> BrowserTarget:
        if graph.get("schema_version") != UI_GRAPH_SCHEMA_VERSION:
            raise TargetResolutionError("ui_graph_schema_mismatch")
        if compact_text(graph.get("graph_id"), 300) != compact_text(
            expected_graph_id, 300
        ):
            raise TargetResolutionError("ui_graph_id_mismatch")
        if compact_text(graph.get("capture_id"), 200) != compact_text(
            capture_id, 200
        ):
            raise TargetResolutionError("ui_graph_capture_mismatch")
        if (
            graph.get("analysis_only") is not True
            or graph.get("execution_authorized") is not False
            or graph.get("safe_for_execution") is not False
            or self._mapping(graph.get("stats")).get("truncated") is True
        ):
            raise TargetResolutionError("ui_graph_safety_boundary_invalid")

        node_id = compact_text(target_node_id, 300)
        nodes = graph.get("nodes")
        if not node_id or not isinstance(nodes, list):
            raise TargetResolutionError("browser_target_node_missing")
        matches = [
            node
            for node in nodes
            if isinstance(node, Mapping)
            and compact_text(node.get("id"), 300) == node_id
        ]
        if len(matches) != 1:
            raise TargetResolutionError("browser_target_node_missing")
        node = matches[0]
        interaction = self._mapping(node.get("interaction"))
        source = self._mapping(node.get("source"))
        if (
            source.get("kind") != "cdp"
            or interaction.get("candidate") is not True
            or interaction.get("status") != "candidate_only"
            or node.get("disabled") is True
            or node.get("actionable") is not False
            or node.get("can_click_now") is not False
            or node.get("safe_for_execution") is not False
        ):
            raise TargetResolutionError("browser_target_not_candidate")

        backend_node_id = source.get("backend_node_id")
        if (
            isinstance(backend_node_id, bool)
            or not isinstance(backend_node_id, int)
            or backend_node_id < 1
        ):
            raise TargetResolutionError("browser_target_backend_id_missing")

        owner = compact_text(node.get("owner_business_id"), 200)
        if not owner or strong_identity_key(
            "asset_id", owner
        ) != strong_identity_key("asset_id", asset_id):
            raise TargetResolutionError("browser_target_asset_mismatch")
        control_action = compact_text(control.get("action_id"), 200).casefold()
        if not control_action or compact_text(
            node.get("action_id"), 200
        ).casefold() != control_action:
            raise TargetResolutionError("browser_target_action_mismatch")

        selector = compact_text(control.get("selector"), 500)
        selector_match = _SIMPLE_ID_SELECTOR.fullmatch(selector)
        attributes = self._mapping(node.get("attributes"))
        if (
            selector_match is None
            or compact_text(attributes.get("id"), 300) != selector_match.group(1)
        ):
            raise TargetResolutionError("browser_target_selector_mismatch")
        frame_id = compact_text(source.get("frame_id"), 200)
        if not frame_id:
            raise TargetResolutionError("browser_target_frame_missing")
        page_url = compact_text(self._mapping(graph.get("page")).get("url"), 2048)
        parsed_page = urlsplit(page_url)
        if (
            parsed_page.scheme not in {"http", "https"}
            or not parsed_page.netloc
            or parsed_page.username
            or parsed_page.password
        ):
            raise TargetResolutionError("browser_target_page_missing")
        return BrowserTarget(
            node_id=node_id,
            backend_node_id=backend_node_id,
            frame_id=frame_id,
            page_url=page_url,
        )

    @staticmethod
    def _mapping(value: Any) -> Mapping[str, Any]:
        return value if isinstance(value, Mapping) else {}


__all__ = ["TargetResolutionError", "UIGraphTargetResolver"]
