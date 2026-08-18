import copy
import unittest

from kt6_backend.asset_inventory import AssetResolver, InMemoryAssetInventoryAdapter
from kt6_backend.dom_action_binding import DOMActionBindingService
from kt6_backend.execution.target_resolver import UIGraphTargetResolver
from kt6_backend.execution.verifier import (
    AssetDetailOutcomeVerifier,
    CanvasSelectionVerifier,
    PageReadyVerifier,
)
from kt6_backend.execution.verifier_registry import OutcomeVerifierRegistry
from kt6_backend.safe_dom_actions import SafeDOMActionService
from tests.test_browser_executor import GraphCaptureProvider, RecordingExecutor, cdp_graph
from tests.test_dom_action_binding import ASSETS, device_snapshot


def details_snapshot(capture_id: str, created_at: float, *, panel: bool = False) -> dict:
    value = device_snapshot(capture_id)
    value["created_at"] = created_at
    value["content_hash"] = f"content-{capture_id}"
    subject = copy.deepcopy(value["dom"]["elements"][0])
    control = copy.deepcopy(value["dom"]["elements"][1])
    control.update(
        {
            "ref": "frame:0:#ap-001-details",
            "selector": "#ap-001-details",
            "label": "查看详情",
            "action_id": "device.details",
        }
    )
    value["dom"]["elements"] = [subject, control]
    if panel:
        value["dom"]["elements"].append(
            {
                "frame_id": "0",
                "frame_url": "https://nce.example/devices",
                "document_id": "document-1",
                "ref": "frame:0:#asset-detail-panel",
                "selector": "#asset-detail-panel",
                "parent_ref": "",
                "test_id": "asset-detail-panel",
                "owner_business_id": "ap_001",
                "label": "AP_001 详情",
                "actionable": False,
            }
        )
    return value


def details_graph() -> dict:
    graph = cdp_graph("capture-current")
    node = graph["nodes"][0]
    node["id"] = "cdp:details"
    node["action_id"] = "device.details"
    node["attributes"].update(
        {"id": "ap-001-details", "data-action-id": "device.details"}
    )
    return graph


class AssetDetailOutcomeVerifierTest(unittest.TestCase):
    def test_registry_selects_page_and_canvas_verifiers_by_contract(self):
        registry = OutcomeVerifierRegistry(
            [PageReadyVerifier(), CanvasSelectionVerifier()]
        )
        before = details_snapshot("capture-before", 100.0)
        page_after = details_snapshot("capture-page", 101.0)
        page_after["dom"]["elements"].append(
            {
                "test_id": "topology-page",
                "owner_business_id": "ap_001",
                "bbox": [0, 0, 500, 400],
            }
        )
        page_verified, page_verifier = registry.verify_expected(
            expected={
                "type": "page_ready",
                "page": "topology",
                "asset_id": "ap_001",
            },
            action_id="open_topology",
            before=before,
            after=page_after,
        )
        canvas_after = details_snapshot("capture-canvas", 102.0)
        canvas_after["dom"]["elements"].append(
            {
                "test_id": "canvas-selection-result",
                "selected_asset_id": "ap_001",
            }
        )
        canvas_verified, canvas_verifier = registry.verify_expected(
            expected={"type": "canvas_asset_selected", "asset_id": "ap_001"},
            action_id="select_canvas_asset",
            before=page_after,
            after=canvas_after,
        )

        self.assertTrue(page_verified)
        self.assertEqual(page_verifier, "topology_page_dom")
        self.assertTrue(canvas_verified)
        self.assertEqual(canvas_verifier, "canvas_selection_dom")

    def test_requires_a_new_capture_with_the_exact_asset_panel(self):
        verifier = AssetDetailOutcomeVerifier()
        before = details_snapshot("capture-before", 100.0)
        after = details_snapshot("capture-after", 102.0, panel=True)

        self.assertTrue(
            verifier.verify(
                action_id="open_asset_details",
                asset_id="AP_001",
                before=before,
                after=after,
            )
        )
        wrong_asset = copy.deepcopy(after)
        wrong_asset["dom"]["elements"][-1]["owner_business_id"] = "ap_002"
        self.assertFalse(
            verifier.verify(
                action_id="open_asset_details",
                asset_id="ap_001",
                before=before,
                after=wrong_asset,
            )
        )
        self.assertFalse(
            verifier.verify(
                action_id="open_asset_details",
                asset_id="ap_001",
                before=after,
                after=after,
            )
        )

    def test_safe_action_moves_from_dispatched_to_verified(self):
        captures = GraphCaptureProvider(
            [
                details_snapshot("capture-initial", 99.0),
                details_snapshot("capture-current", 101.0),
                details_snapshot("capture-after", 102.0, panel=True),
            ],
            details_graph(),
        )
        service = SafeDOMActionService(
            DOMActionBindingService(
                AssetResolver(InMemoryAssetInventoryAdapter(ASSETS))
            ),
            captures,
            clock=lambda: 101.0,
            executor=RecordingExecutor(),
            target_resolver=UIGraphTargetResolver(),
            outcome_verifiers=OutcomeVerifierRegistry(
                [AssetDetailOutcomeVerifier()]
            ),
        )
        prepared = service.prepare(
            asset_reference="AP1",
            action="查看详情",
            page_capture_id="capture-initial",
            scope={"site_id": "site-a"},
        )
        ready = service.preflight(
            plan_id=prepared["plan_id"],
            current_capture_id="capture-current",
            confirmed=True,
            confirmed_asset_id="ap_001",
            confirmed_action="open_asset_details",
            permissions=["assets.read"],
        )
        dispatched = service.execute(
            execution_token=ready["execution_token"],
            dry_run=False,
            graph_id="uig:current",
            target_node_id="cdp:details",
        )
        verified = service.verify_outcome(
            plan_id=prepared["plan_id"],
            current_capture_id="capture-after",
        )

        self.assertEqual(dispatched["status"], "executed_pending_verification")
        self.assertEqual(verified["status"], "verified")
        self.assertTrue(verified["outcome_verified"])
        steps = {
            step["step_id"]: step
            for step in verified["operation_plan"]["steps"]
        }
        self.assertEqual(steps["verify_outcome"]["status"], "completed")

    def test_missing_panel_is_verify_failed_not_success(self):
        captures = GraphCaptureProvider(
            [
                details_snapshot("capture-initial", 99.0),
                details_snapshot("capture-current", 101.0),
                details_snapshot("capture-after", 102.0),
            ],
            details_graph(),
        )
        service = SafeDOMActionService(
            DOMActionBindingService(
                AssetResolver(InMemoryAssetInventoryAdapter(ASSETS))
            ),
            captures,
            clock=lambda: 101.0,
            executor=RecordingExecutor(),
            target_resolver=UIGraphTargetResolver(),
            outcome_verifiers=OutcomeVerifierRegistry(
                [AssetDetailOutcomeVerifier()]
            ),
        )
        prepared = service.prepare(
            asset_reference="AP1",
            action="查看详情",
            page_capture_id="capture-initial",
            scope={"site_id": "site-a"},
        )
        ready = service.preflight(
            plan_id=prepared["plan_id"],
            current_capture_id="capture-current",
            confirmed=True,
            confirmed_asset_id="ap_001",
            confirmed_action="open_asset_details",
            permissions=["assets.read"],
        )
        service.execute(
            execution_token=ready["execution_token"],
            dry_run=False,
            graph_id="uig:current",
            target_node_id="cdp:details",
        )
        result = service.verify_outcome(
            plan_id=prepared["plan_id"],
            current_capture_id="capture-after",
        )

        self.assertEqual(result["status"], "verify_failed")
        self.assertFalse(result["outcome_verified"])
        self.assertNotEqual(result["status"], "verified")


if __name__ == "__main__":
    unittest.main()
