import unittest

from kt6_backend.asset_inventory import (
    AssetResolver,
    InMemoryAssetInventoryAdapter,
)


ASSETS = [
    {
        "asset_id": "ap_001",
        "asset_type": "ap",
        "name": "AP1",
        "aliases": ["AP-001"],
        "management_ip": "10.0.0.1",
        "serial_number": "SN-001",
        "site_id": "site-a",
        "site": "站点A",
        "floor": "1F",
        "status": "online",
        "version": 3,
    },
    {
        "asset_id": "ap_101",
        "asset_type": "ap",
        "name": "AP1",
        "management_ip": "10.0.1.1",
        "serial_number": "SN-101",
        "site_id": "site-b",
        "site": "站点B",
        "floor": "2F",
        "status": "online",
        "version": 8,
    },
]


class AssetResolverTest(unittest.TestCase):
    def setUp(self):
        self.inventory = InMemoryAssetInventoryAdapter(ASSETS)
        self.resolver = AssetResolver(self.inventory)

    def test_strong_identifiers_resolve_without_guessing(self):
        for reference in (
            {"asset_id": "ap_001"},
            {"management_ip": "10.0.0.1"},
            {"serial_number": "SN-001"},
        ):
            with self.subTest(reference=reference):
                result = self.resolver.resolve(reference)
                self.assertEqual(result["status"], "verified")
                self.assertTrue(result["identity_verified"])
                self.assertFalse(result["safe_for_execution"])
                self.assertEqual(result["asset"]["asset_id"], "ap_001")

    def test_name_requires_scope_and_duplicate_names_are_ambiguous(self):
        ambiguous = self.resolver.resolve("AP1")
        self.assertEqual(ambiguous["status"], "ambiguous")
        self.assertEqual(ambiguous["reason"], "multiple_inventory_matches")

        verified = self.resolver.resolve("AP1", {"site_id": "site-a"})
        self.assertEqual(verified["status"], "verified")
        self.assertEqual(verified["asset"]["asset_id"], "ap_001")

    def test_unique_name_without_scope_is_still_not_executable(self):
        inventory = InMemoryAssetInventoryAdapter([ASSETS[0]])
        result = AssetResolver(inventory).resolve("AP1")

        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(result["reason"], "name_requires_scope")
        self.assertFalse(result["safe_for_execution"])

    def test_conflicting_strong_evidence_fails_closed(self):
        result = self.resolver.resolve(
            {
                "asset_id": "ap_001",
                "management_ip": "10.0.1.1",
            }
        )

        self.assertEqual(result["status"], "conflict")
        self.assertEqual(result["reason"], "strong_identity_conflict")
        self.assertFalse(result["safe_for_execution"])

    def test_duplicate_inventory_identity_is_ambiguous(self):
        duplicate = dict(ASSETS[0])
        duplicate["site_id"] = "site-c"
        inventory = InMemoryAssetInventoryAdapter([ASSETS[0], duplicate])

        result = AssetResolver(inventory).resolve({"asset_id": "ap_001"})

        self.assertEqual(result["status"], "conflict")
        self.assertEqual(
            result["reason"], "inventory_duplicate_asset_id"
        )
        self.assertEqual(result["candidate_count"], 2)

    def test_strong_identity_punctuation_cannot_collide(self):
        asset = dict(ASSETS[0])
        asset["management_ip"] = "10.10.1.20"
        resolver = AssetResolver(InMemoryAssetInventoryAdapter([asset]))

        ip_result = resolver.resolve(
            {"management_ip": "101.0.12.0"}
        )
        serial_result = self.resolver.resolve(
            {"serial_number": "SN001"}
        )

        self.assertEqual(ip_result["status"], "unmatched")
        self.assertEqual(
            ip_result["reason"], "management_ip_not_found"
        )
        self.assertEqual(serial_result["status"], "unmatched")
        self.assertEqual(
            serial_result["reason"], "serial_number_not_found"
        )

    def test_name_conflicting_with_strong_identity_is_rejected(self):
        result = self.resolver.resolve(
            {"asset_id": "ap_001", "name": "AP2"}
        )

        self.assertEqual(result["status"], "conflict")
        self.assertEqual(result["reason"], "strong_identity_conflict")

    def test_reference_scope_cannot_override_supplied_scope(self):
        result = self.resolver.resolve(
            {
                "name": "AP1",
                "site_id": "site-b",
            },
            {"site_id": "site-a"},
        )

        self.assertEqual(result["status"], "conflict")
        self.assertEqual(result["reason"], "scope_conflict")

    def test_duplicate_serial_is_inventory_conflict(self):
        duplicate = dict(ASSETS[1])
        duplicate["serial_number"] = "sn-001"
        resolver = AssetResolver(
            InMemoryAssetInventoryAdapter([ASSETS[0], duplicate])
        )

        result = resolver.resolve({"asset_id": "ap_101"})

        self.assertEqual(result["status"], "conflict")
        self.assertEqual(
            result["reason"], "inventory_duplicate_serial_number"
        )

    def test_inventory_order_does_not_change_resolution(self):
        forward = self.resolver.resolve("AP1", {"site_id": "site-b"})
        reverse = AssetResolver(
            InMemoryAssetInventoryAdapter(list(reversed(ASSETS)))
        ).resolve("AP1", {"site_id": "site-b"})

        self.assertEqual(forward, reverse)


if __name__ == "__main__":
    unittest.main()
