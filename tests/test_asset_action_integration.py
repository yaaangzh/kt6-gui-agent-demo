from pathlib import Path
import tempfile
import unittest

from kt6_backend.asset_inventory import (
    AssetResolver,
    InMemoryAssetInventoryAdapter,
)
from kt6_backend.dom_action_binding import DOMActionBindingService
from kt6_backend.page_perception import (
    PagePerceptionService,
    SQLitePageCaptureStore,
)
from kt6_backend.perception_runtime import PerceptionRuntime
from kt6_backend.safe_dom_actions import SafeDOMActionService


def browser_payload() -> dict:
    context = {
        "frame_id": "0",
        "frame_url": "https://nce.example/devices",
        "document_id": "document-1",
    }
    return {
        "page": {
            "url": "https://nce.example/devices",
            "title": "NCE Devices",
            "viewport": {
                "width": 1280,
                "height": 720,
                "device_pixel_ratio": 1,
            },
        },
        "dom": {
            "elements": [
                {
                    **context,
                    "ref": "frame:0:#ap-1-row",
                    "selector": "#ap-1-row",
                    "role": "row",
                    "label": "AP1",
                    "business_id": "ap_001",
                    "asset_id": "ap_001",
                    "management_ip": "10.0.0.1",
                    "serial_number": "SN-001",
                    "site_id": "site-a",
                    "asset_version": 3,
                    "bbox": [20, 100, 500, 40],
                },
                {
                    **context,
                    "ref": "frame:0:#shutdown-ap-1",
                    "selector": "#shutdown-ap-1",
                    "parent_ref": "frame:0:#ap-1-row",
                    "role": "button",
                    "label": "关闭",
                    "action_id": "ap.shutdown",
                    "owner_business_id": "ap_001",
                    "actionable": True,
                    "bbox": [440, 105, 60, 30],
                },
            ]
        },
        "canvases": [],
        "adapter_scene": None,
    }


class AssetActionIntegrationTest(unittest.TestCase):
    def test_capture_prepare_fresh_preflight_and_dry_run(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            perception = PagePerceptionService(
                SQLitePageCaptureStore(
                    root / "captures.sqlite3",
                    root / "assets",
                ),
                PerceptionRuntime(),
            )
            inventory = InMemoryAssetInventoryAdapter(
                [
                    {
                        "asset_id": "ap_001",
                        "asset_type": "ap",
                        "name": "AP1",
                        "management_ip": "10.0.0.1",
                        "serial_number": "SN-001",
                        "site_id": "site-a",
                        "status": "online",
                        "version": 3,
                        "allowed_origins": ["https://nce.example"],
                    }
                ]
            )
            actions = SafeDOMActionService(
                DOMActionBindingService(AssetResolver(inventory)),
                perception,
            )

            initial = perception.ingest(browser_payload())
            self.assertFalse(initial["scene"]["actionable_grounding"])

            prepared = actions.prepare(
                asset_reference="AP1",
                action="关闭",
                page_capture_id=initial["capture_id"],
                scope={"site_id": "site-a"},
                task_id="task-1",
                principal_id="operator-1",
            )
            self.assertEqual(prepared["status"], "prepared")

            current = perception.ingest(browser_payload())
            ready = actions.preflight(
                plan_id=prepared["plan_id"],
                current_capture_id=current["capture_id"],
                confirmed=True,
                confirmed_asset_id="ap_001",
                confirmed_action="shutdown_ap",
                permissions=["assets.ap.shutdown"],
            )
            self.assertEqual(ready["status"], "ready")

            result = actions.execute(
                execution_token=ready["execution_token"],
                dry_run=True,
            )
            self.assertEqual(result["status"], "dry_run_ok")
            self.assertEqual(result["asset_id"], "ap_001")
            self.assertFalse(result["executed"])


if __name__ == "__main__":
    unittest.main()
