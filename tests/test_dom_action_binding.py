import copy
import unittest

from kt6_backend.asset_inventory import (
    AssetResolver,
    InMemoryAssetInventoryAdapter,
)
from kt6_backend.dom_action_binding import DOMActionBindingService


ASSETS = [
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
    },
    {
        "asset_id": "ap_002",
        "asset_type": "ap",
        "name": "AP2",
        "management_ip": "10.0.0.2",
        "serial_number": "SN-002",
        "site_id": "site-a",
        "status": "online",
        "version": 2,
        "allowed_origins": ["https://nce.example"],
    },
]


def device_snapshot(capture_id: str = "capture-1") -> dict:
    common = {
        "frame_id": "0",
        "frame_url": "https://nce.example/devices",
        "document_id": "document-1",
    }
    return {
        "capture_id": capture_id,
        "page": {"url": "https://nce.example/devices"},
        "created_at": 100.0,
        "content_hash": f"hash-{capture_id}",
        "scene_revision": 1,
        "dom": {
            "elements": [
                {
                    **common,
                    "ref": "frame:0:#ap-1-row",
                    "selector": "#ap-1-row",
                    "parent_ref": "",
                    "role": "row",
                    "label": "AP1",
                    "business_id": "ap_001",
                    "management_ip": "10.0.0.1",
                    "serial_number": "SN-001",
                    "site_id": "site-a",
                    "asset_version": 3,
                },
                {
                    **common,
                    "ref": "frame:0:#shutdown-ap-1",
                    "selector": "#shutdown-ap-1",
                    "parent_ref": "frame:0:#ap-1-row",
                    "role": "button",
                    "label": "关闭",
                    "action_id": "ap.shutdown",
                    "owner_business_id": "ap_001",
                    "actionable": True,
                    "disabled": False,
                },
                {
                    **common,
                    "ref": "frame:0:#ap-2-row",
                    "selector": "#ap-2-row",
                    "parent_ref": "",
                    "role": "row",
                    "label": "AP2",
                    "business_id": "ap_002",
                    "management_ip": "10.0.0.2",
                    "serial_number": "SN-002",
                    "site_id": "site-a",
                    "asset_version": 2,
                },
                {
                    **common,
                    "ref": "frame:0:#shutdown-ap-2",
                    "selector": "#shutdown-ap-2",
                    "parent_ref": "frame:0:#ap-2-row",
                    "role": "button",
                    "label": "关闭",
                    "action_id": "ap.shutdown",
                    "owner_business_id": "ap_002",
                    "actionable": True,
                    "disabled": False,
                },
                {
                    **common,
                    "ref": "frame:0:#global-close",
                    "selector": "#global-close",
                    "parent_ref": "",
                    "role": "button",
                    "label": "关闭",
                    "action_id": "ap.shutdown",
                    "actionable": True,
                    "disabled": False,
                },
            ]
        },
    }


class DOMActionBindingTest(unittest.TestCase):
    def setUp(self):
        resolver = AssetResolver(InMemoryAssetInventoryAdapter(ASSETS))
        self.binder = DOMActionBindingService(resolver)

    def test_binds_unique_asset_and_owned_action_control(self):
        result = self.binder.bind(
            device_snapshot(),
            "AP1",
            "关闭",
            {"site_id": "site-a"},
        )

        self.assertEqual(result["status"], "verified")
        self.assertTrue(result["binding_verified"])
        self.assertFalse(result["safe_for_execution"])
        self.assertEqual(result["asset"]["asset_id"], "ap_001")
        self.assertEqual(result["action_id"], "shutdown_ap")
        self.assertEqual(result["subject"]["ref"], "frame:0:#ap-1-row")
        self.assertEqual(result["control"]["selector"], "#shutdown-ap-1")
        self.assertIn("control_owned_by_subject", result["evidence"])

    def test_global_or_other_asset_button_is_never_selected(self):
        snapshot = device_snapshot()
        snapshot["dom"]["elements"] = [
            element
            for element in snapshot["dom"]["elements"]
            if element["ref"] != "frame:0:#shutdown-ap-1"
        ]

        result = self.binder.bind(
            snapshot,
            {"asset_id": "ap_001"},
            "shutdown_ap",
        )

        self.assertEqual(result["status"], "unmatched")
        self.assertEqual(
            result["reason"], "action_control_not_owned_by_asset"
        )

    def test_cross_document_control_is_rejected(self):
        snapshot = device_snapshot()
        control = next(
            item
            for item in snapshot["dom"]["elements"]
            if item["ref"] == "frame:0:#shutdown-ap-1"
        )
        control["document_id"] = "document-2"

        result = self.binder.bind(
            snapshot,
            {"asset_id": "ap_001"},
            "shutdown_ap",
        )

        self.assertEqual(result["status"], "unmatched")

    def test_disabled_or_selectorless_control_is_rejected(self):
        for mutation in ("disabled", "selector"):
            with self.subTest(mutation=mutation):
                snapshot = device_snapshot()
                control = next(
                    item
                    for item in snapshot["dom"]["elements"]
                    if item["ref"] == "frame:0:#shutdown-ap-1"
                )
                if mutation == "disabled":
                    control["disabled"] = True
                else:
                    control["selector"] = ""
                result = self.binder.bind(
                    snapshot,
                    {"asset_id": "ap_001"},
                    "shutdown_ap",
                )
                self.assertEqual(result["status"], "unmatched")

    def test_multiple_controls_inside_same_asset_are_ambiguous(self):
        snapshot = device_snapshot()
        duplicate = copy.deepcopy(
            next(
                item
                for item in snapshot["dom"]["elements"]
                if item["ref"] == "frame:0:#shutdown-ap-1"
            )
        )
        duplicate["ref"] = "frame:0:#shutdown-ap-1-secondary"
        duplicate["selector"] = "#shutdown-ap-1-secondary"
        snapshot["dom"]["elements"].append(duplicate)

        result = self.binder.bind(
            snapshot,
            {"asset_id": "ap_001"},
            "shutdown_ap",
        )

        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(result["candidate_pair_count"], 2)
        self.assertFalse(result["safe_for_execution"])

    def test_conflicting_dom_identity_is_not_grounded(self):
        snapshot = device_snapshot()
        subject = snapshot["dom"]["elements"][0]
        subject["management_ip"] = "10.0.0.2"

        result = self.binder.bind(
            snapshot,
            {"asset_id": "ap_001"},
            "shutdown_ap",
        )

        self.assertEqual(result["status"], "unmatched")
        self.assertEqual(result["reason"], "asset_not_grounded_in_dom")


    def test_untrusted_or_unconfigured_page_origin_is_rejected(self):
        untrusted = device_snapshot()
        untrusted["page"]["url"] = "https://evil.example/devices"
        result = self.binder.bind(
            untrusted,
            {"asset_id": "ap_001"},
            "shutdown_ap",
        )
        self.assertEqual(result["reason"], "page_origin_not_allowed")

        assets = copy.deepcopy(ASSETS)
        assets[0]["allowed_origins"] = []
        binder = DOMActionBindingService(
            AssetResolver(InMemoryAssetInventoryAdapter(assets))
        )
        result = binder.bind(
            device_snapshot(),
            {"asset_id": "ap_001"},
            "shutdown_ap",
        )
        self.assertEqual(
            result["reason"],
            "asset_origin_not_configured",
        )
        self.assertFalse(result["safe_for_execution"])

    def test_untrusted_frame_origin_is_rejected(self):
        snapshot = device_snapshot()
        for element in snapshot["dom"]["elements"]:
            element["frame_url"] = "https://evil.example/embedded"

        result = self.binder.bind(
            snapshot,
            {"asset_id": "ap_001"},
            "shutdown_ap",
        )

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "frame_origin_not_allowed")

    def test_high_risk_action_requires_explicit_action_id(self):
        snapshot = device_snapshot()
        control = next(
            item
            for item in snapshot["dom"]["elements"]
            if item["ref"] == "frame:0:#shutdown-ap-1"
        )
        control["action_id"] = ""

        result = self.binder.bind(
            snapshot,
            {"asset_id": "ap_001"},
            "shutdown_ap",
        )

        self.assertEqual(result["status"], "unmatched")
        self.assertEqual(
            result["reason"],
            "action_control_not_owned_by_asset",
        )

    def test_owner_claim_cannot_replace_dom_ancestry(self):
        snapshot = device_snapshot()
        control = next(
            item
            for item in snapshot["dom"]["elements"]
            if item["ref"] == "frame:0:#shutdown-ap-1"
        )
        control["parent_ref"] = ""

        result = self.binder.bind(
            snapshot,
            {"asset_id": "ap_001"},
            "shutdown_ap",
        )

        self.assertEqual(result["status"], "unmatched")
        self.assertEqual(
            result["reason"],
            "action_control_not_owned_by_asset",
        )

    def test_duplicate_ref_or_selector_is_ambiguous(self):
        for duplicate_field in ("ref", "selector"):
            with self.subTest(duplicate_field=duplicate_field):
                snapshot = device_snapshot()
                duplicate = copy.deepcopy(
                    next(
                        item
                        for item in snapshot["dom"]["elements"]
                        if item["ref"] == "frame:0:#shutdown-ap-1"
                    )
                )
                if duplicate_field == "selector":
                    duplicate["ref"] = "frame:0:#duplicate-control"
                snapshot["dom"]["elements"].append(duplicate)

                result = self.binder.bind(
                    snapshot,
                    {"asset_id": "ap_001"},
                    "shutdown_ap",
                )

                self.assertEqual(result["status"], "ambiguous")
                self.assertEqual(
                    result["reason"],
                    f"duplicate_dom_{duplicate_field}",
                )

    def test_high_risk_control_rejects_semantic_alias_as_machine_id(self):
        snapshot = device_snapshot()
        control = next(
            item
            for item in snapshot["dom"]["elements"]
            if item["ref"] == "frame:0:#shutdown-ap-1"
        )
        control["action_id"] = "disable"

        result = self.binder.bind(
            snapshot,
            {"asset_id": "ap_001"},
            "shutdown_ap",
        )

        self.assertEqual(result["status"], "unmatched")

    def test_dom_management_ip_preserves_punctuation(self):
        assets = copy.deepcopy(ASSETS)
        assets[0]["management_ip"] = "10.10.1.20"
        binder = DOMActionBindingService(
            AssetResolver(InMemoryAssetInventoryAdapter(assets))
        )
        snapshot = device_snapshot()
        snapshot["dom"]["elements"][0][
            "management_ip"
        ] = "101.0.12.0"

        result = binder.bind(
            snapshot,
            {"asset_id": "ap_001"},
            "shutdown_ap",
        )

        self.assertEqual(result["status"], "unmatched")
        self.assertEqual(result["reason"], "asset_not_grounded_in_dom")

    def test_owner_asset_id_preserves_punctuation(self):
        assets = copy.deepcopy(ASSETS)
        assets[0]["asset_id"] = "ap-001"
        binder = DOMActionBindingService(
            AssetResolver(InMemoryAssetInventoryAdapter(assets))
        )
        snapshot = device_snapshot()
        subject = snapshot["dom"]["elements"][0]
        control = next(
            item
            for item in snapshot["dom"]["elements"]
            if item["ref"] == "frame:0:#shutdown-ap-1"
        )
        subject["business_id"] = "ap-001"
        control["owner_business_id"] = "ap001"

        result = binder.bind(
            snapshot,
            {"asset_id": "ap-001"},
            "shutdown_ap",
        )

        self.assertEqual(result["status"], "unmatched")
        self.assertEqual(
            result["reason"],
            "action_control_not_owned_by_asset",
        )

    def test_pseudo_selector_is_not_executable(self):
        snapshot = device_snapshot()
        control = next(
            item
            for item in snapshot["dom"]["elements"]
            if item["ref"] == "frame:0:#shutdown-ap-1"
        )
        control["selector"] = "frame:0:@capture:2"

        result = self.binder.bind(
            snapshot,
            {"asset_id": "ap_001"},
            "shutdown_ap",
        )

        self.assertEqual(result["status"], "unmatched")

if __name__ == "__main__":
    unittest.main()
