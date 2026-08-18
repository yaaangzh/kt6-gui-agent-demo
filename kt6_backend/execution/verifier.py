from __future__ import annotations

from typing import Any, Mapping, Protocol

from ..asset_inventory import compact_text, strong_identity_key


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
]
