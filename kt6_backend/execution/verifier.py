from __future__ import annotations

from typing import Any, Mapping, Protocol

from ..asset_inventory import compact_text, strong_identity_key


class OutcomeVerifier(Protocol):
    """KT6-owned business outcome verifier for a post-action fresh capture."""

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
                "asset_id", compact_text(element.get("asset_id"), 200)
            )
            == strong_identity_key("asset_id", asset_id)
        ]
        return len(matches) == 1


__all__ = ["AssetDetailOutcomeVerifier", "OutcomeVerifier"]
