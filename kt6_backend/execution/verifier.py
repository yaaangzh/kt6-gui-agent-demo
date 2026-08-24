from __future__ import annotations

from typing import Any, Mapping, Protocol

from ..asset_inventory import compact_text, strong_identity_key
from .semantic_target import matching_nodes, node_selected


class OutcomeVerifier(Protocol):
    """KT6-owned business outcome verifier for a post-action fresh capture."""

    verifier_id: str
    supported_action_id: str
    expected_type: str

    def verify(
        self,
        *,
        action_id: str,
        asset_id: str,
        before: Mapping[str, Any],
        after: Mapping[str, Any],
    ) -> bool:
        ...


class AssetDetailOutcomeVerifier:
    """Verify that a new capture contains the requested asset detail panel."""

    verifier_id = "asset_detail_dom"
    supported_action_id = "open_asset_details"
    expected_type = "asset_detail_visible"
    panel_test_id = "asset-detail-panel"

    def verify(
        self,
        *,
        action_id: str,
        asset_id: str,
        before: Mapping[str, Any],
        after: Mapping[str, Any],
    ) -> bool:
        if compact_text(action_id, 200) != self.supported_action_id:
            return False
        before_id = compact_text(before.get("capture_id"), 200)
        after_id = compact_text(after.get("capture_id"), 200)
        if not before_id or not after_id or before_id == after_id:
            return False
        try:
            if float(after.get("created_at", 0)) <= float(
                before.get("created_at", 0)
            ):
                return False
        except (TypeError, ValueError, OverflowError):
            return False
        return not self._panel_present(before, asset_id) and self._panel_present(
            after, asset_id
        )

    def _panel_present(
        self,
        capture: Mapping[str, Any],
        asset_id: str,
    ) -> bool:
        dom = capture.get("dom")
        elements = dom.get("elements") if isinstance(dom, Mapping) else None
        if not isinstance(elements, list):
            return False
        matches = [
            element
            for element in elements
            if isinstance(element, Mapping)
            and compact_text(element.get("test_id"), 200) == self.panel_test_id
            and strong_identity_key(
                "asset_id",
                compact_text(element.get("owner_business_id"), 200),
            )
            == strong_identity_key("asset_id", asset_id)
        ]
        return len(matches) == 1


class PageReadyVerifier:
    """Verify that the fixture topology view appeared after navigation."""

    verifier_id = "topology_page_dom"
    supported_action_id = "open_topology"
    expected_type = "page_ready"
    page_test_id = "topology-page"

    def verify(
        self,
        *,
        action_id: str,
        asset_id: str,
        before: Mapping[str, Any],
        after: Mapping[str, Any],
    ) -> bool:
        if compact_text(action_id, 200) != self.supported_action_id:
            return False
        return _fresh_transition(
            before,
            after,
            lambda capture: _matching_elements(
                capture,
                field="test_id",
                value=self.page_test_id,
                asset_id=asset_id,
                asset_field="owner_business_id",
                require_visible=True,
            ),
        )


class CanvasSelectionVerifier:
    """Verify the DOM status emitted by a real Canvas click handler."""

    verifier_id = "canvas_selection_dom"
    supported_action_id = "select_canvas_asset"
    expected_type = "canvas_asset_selected"
    result_test_id = "canvas-selection-result"

    def verify(
        self,
        *,
        action_id: str,
        asset_id: str,
        before: Mapping[str, Any],
        after: Mapping[str, Any],
    ) -> bool:
        if compact_text(action_id, 200) != self.supported_action_id:
            return False
        return _fresh_transition(
            before,
            after,
            lambda capture: _matching_elements(
                capture,
                field="test_id",
                value=self.result_test_id,
                asset_id=asset_id,
                asset_field="selected_asset_id",
            ),
        )


class UIGraphOutcomeVerifier:
    """Verify generic model-planned outcomes from two fresh UI Graphs."""

    verifier_id = "ui_graph_state"
    expected_types = frozenset(
        {
            "element_visible",
            "element_disappeared",
            "element_selected",
            "selected",
            "text_present",
            "url_changed",
            "page_changed",
            "input_value",
        }
    )

    def verify(
        self,
        *,
        expected: Mapping[str, Any],
        before: Mapping[str, Any],
        after: Mapping[str, Any],
    ) -> bool:
        if before.get("capture_id") == after.get("capture_id"):
            return False
        expected_type = compact_text(expected.get("type"), 100)
        if expected_type in {"page_changed", "url_changed"}:
            before_page = before.get("page")
            after_page = after.get("page")
            if not isinstance(before_page, Mapping) or not isinstance(
                after_page, Mapping
            ):
                return False
            return compact_text(before_page.get("url"), 2048) != compact_text(
                after_page.get("url"), 2048
            )
        target = expected.get("target")
        if not isinstance(target, Mapping):
            return False
        source_kinds = frozenset({"cdp"}) if expected_type == "input_value" else None
        before_matches = matching_nodes(target, before, source_kinds=source_kinds)
        after_matches = matching_nodes(target, after, source_kinds=source_kinds)
        if expected_type == "input_value":
            expected_value = expected.get("value")
            if not isinstance(expected_value, str):
                return False
            before_value = (
                _node_attribute(before_matches[0], "value")
                if len(before_matches) == 1
                else None
            )
            after_value = (
                _node_attribute(after_matches[0], "value")
                if len(after_matches) == 1
                else None
            )
            return after_value == expected_value and before_value != expected_value
        if expected_type == "element_visible":
            return len(after_matches) == 1 and len(before_matches) == 0
        if expected_type == "element_disappeared":
            return len(after_matches) == 0 and len(before_matches) >= 1
        if expected_type == "text_present":
            return len(after_matches) >= 1
        if expected_type in {"element_selected", "selected"}:
            return (
                len(after_matches) == 1
                and node_selected(after_matches[0])
                and not (
                    len(before_matches) == 1
                    and node_selected(before_matches[0])
                )
            )
        return False


def _node_attribute(node: Mapping[str, Any], name: str) -> str | None:
    attributes = node.get("attributes")
    if not isinstance(attributes, Mapping) or name not in attributes:
        return None
    value = attributes.get(name)
    return value if isinstance(value, str) else str(value)


def _fresh_transition(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    predicate: Any,
) -> bool:
    before_id = compact_text(before.get("capture_id"), 200)
    after_id = compact_text(after.get("capture_id"), 200)
    if not before_id or not after_id or before_id == after_id:
        return False
    try:
        if float(after.get("created_at", 0)) <= float(before.get("created_at", 0)):
            return False
    except (TypeError, ValueError, OverflowError):
        return False
    return not predicate(before) and predicate(after)


def _matching_elements(
    capture: Mapping[str, Any],
    *,
    field: str,
    value: str,
    asset_id: str,
    asset_field: str = "asset_id",
    require_visible: bool = False,
) -> bool:
    dom = capture.get("dom")
    elements = dom.get("elements") if isinstance(dom, Mapping) else None
    if not isinstance(elements, list):
        return False
    matches = [
        element
        for element in elements
        if isinstance(element, Mapping)
        and compact_text(element.get(field), 200) == value
        and strong_identity_key(
            "asset_id", compact_text(element.get(asset_field), 200)
        )
        == strong_identity_key("asset_id", asset_id)
        and (not require_visible or _visible_bbox(element.get("bbox")))
    ]
    return len(matches) == 1


def _visible_bbox(value: Any) -> bool:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return False
    try:
        return float(value[2]) > 0 and float(value[3]) > 0
    except (TypeError, ValueError, OverflowError):
        return False


__all__ = [
    "AssetDetailOutcomeVerifier",
    "CanvasSelectionVerifier",
    "OutcomeVerifier",
    "PageReadyVerifier",
    "UIGraphOutcomeVerifier",
]
