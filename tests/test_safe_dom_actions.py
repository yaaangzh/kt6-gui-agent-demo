import copy
import threading
import unittest

from kt6_backend.asset_inventory import (
    AssetResolver,
    InMemoryAssetInventoryAdapter,
)
from kt6_backend.dom_action_binding import DOMActionBindingService
from kt6_backend.safe_dom_actions import SafeDOMActionService
from tests.test_dom_action_binding import ASSETS, device_snapshot


class FakeClock:
    def __init__(self, value: float):
        self.value = value

    def __call__(self) -> float:
        return self.value


class DictCaptureProvider:
    def __init__(self, snapshots: list[dict]):
        self.snapshots = {
            snapshot["capture_id"]: copy.deepcopy(snapshot)
            for snapshot in snapshots
        }

    def get_action_snapshot(self, capture_id: str):
        value = self.snapshots.get(capture_id)
        return copy.deepcopy(value) if value else None


def fresh_snapshot(capture_id: str, created_at: float) -> dict:
    snapshot = device_snapshot(capture_id)
    snapshot["created_at"] = created_at
    snapshot["content_hash"] = f"content-{capture_id}"
    return snapshot


class SafeDOMActionServiceTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(100.0)
        self.inventory = InMemoryAssetInventoryAdapter(ASSETS)
        self.initial = fresh_snapshot("capture-initial", 99.0)
        self.current = fresh_snapshot("capture-current", 101.0)
        self.captures = DictCaptureProvider([self.initial, self.current])
        binder = DOMActionBindingService(AssetResolver(self.inventory))
        self.service = SafeDOMActionService(
            binder,
            self.captures,
            clock=self.clock,
            capture_max_age_seconds=10,
            token_ttl_seconds=5,
        )

    def prepare(self):
        return self.service.prepare(
            asset_reference="AP1",
            action="关闭",
            page_capture_id="capture-initial",
            scope={"site_id": "site-a"},
            task_id="task-1",
            principal_id="operator-1",
        )

    def preflight(self, plan_id: str, **overrides):
        arguments = {
            "plan_id": plan_id,
            "current_capture_id": "capture-current",
            "confirmed": True,
            "confirmed_asset_id": "ap_001",
            "confirmed_action": "shutdown_ap",
            "permissions": ["assets.ap.shutdown"],
        }
        arguments.update(overrides)
        self.clock.value = 101.0
        return self.service.preflight(**arguments)

    def test_fresh_revalidation_issues_one_time_dry_run_token(self):
        prepared = self.prepare()
        self.assertEqual(prepared["status"], "prepared")
        self.assertFalse(prepared["safe_for_execution"])

        ready = self.preflight(prepared["plan_id"])
        self.assertEqual(ready["status"], "ready")
        self.assertTrue(ready["preflight_verified"])
        self.assertFalse(ready["safe_for_execution"])
        self.assertTrue(ready["dry_run_only"])

        result = self.service.execute(
            execution_token=ready["execution_token"],
            dry_run=True,
        )
        self.assertEqual(result["status"], "dry_run_ok")
        self.assertFalse(result["executed"])
        self.assertEqual(result["asset_id"], "ap_001")
        self.assertEqual(result["target"]["selector"], "#shutdown-ap-1")

        replay = self.service.execute(
            execution_token=ready["execution_token"],
            dry_run=True,
        )
        self.assertEqual(replay["reason"], "token_invalid_or_replayed")

    def test_same_capture_or_missing_confirmation_is_rejected(self):
        prepared = self.prepare()

        same_capture = self.preflight(
            prepared["plan_id"],
            current_capture_id="capture-initial",
        )
        self.assertEqual(same_capture["reason"], "fresh_capture_required")

        not_confirmed = self.preflight(
            prepared["plan_id"],
            confirmed=False,
        )
        self.assertEqual(
            not_confirmed["reason"], "human_confirmation_required"
        )

    def test_confirmation_and_permission_are_bound_to_target(self):
        prepared = self.prepare()

        wrong_target = self.preflight(
            prepared["plan_id"],
            confirmed_asset_id="ap_002",
        )
        self.assertEqual(
            wrong_target["reason"], "confirmation_target_mismatch"
        )

        punctuation_variant = self.preflight(
            prepared["plan_id"],
            confirmed_asset_id="ap001",
        )
        self.assertEqual(
            punctuation_variant["reason"],
            "confirmation_target_mismatch",
        )

        denied = self.preflight(
            prepared["plan_id"],
            permissions=["assets.read"],
        )
        self.assertEqual(denied["reason"], "permission_denied")

        wildcard = self.preflight(
            prepared["plan_id"],
            permissions=["*"],
        )
        self.assertEqual(wildcard["reason"], "permission_denied")

    def test_dom_selector_document_or_action_change_is_rejected(self):
        for field, value in (
            ("selector", "#unexpected"),
            ("document_id", "document-2"),
            ("action_id", "device.details"),
        ):
            with self.subTest(field=field):
                current = fresh_snapshot("capture-current", 101.0)
                control = next(
                    item
                    for item in current["dom"]["elements"]
                    if item["ref"] == "frame:0:#shutdown-ap-1"
                )
                control[field] = value
                self.captures.snapshots["capture-current"] = current
                prepared = self.prepare()
                result = self.preflight(prepared["plan_id"])
                self.assertNotEqual(result["status"], "ready")
                self.assertIn(
                    result["reason"],
                    {
                        "dom_target_changed",
                        "revalidation_action_control_not_owned_by_asset",
                    },
                )

    def test_asset_version_or_state_change_is_rejected(self):
        for mutation, expected in (
            ("version", "asset_version_changed"),
            ("status", "asset_state_not_allowed"),
        ):
            with self.subTest(mutation=mutation):
                self.inventory.replace(ASSETS)
                current = fresh_snapshot("capture-current", 101.0)
                assets = copy.deepcopy(ASSETS)
                if mutation == "version":
                    assets[0]["version"] = 4
                    current["dom"]["elements"][0]["asset_version"] = 4
                else:
                    assets[0]["status"] = "offline"
                self.captures.snapshots["capture-current"] = current
                prepared = self.prepare()
                self.inventory.replace(assets)
                result = self.preflight(prepared["plan_id"])
                self.assertEqual(result["reason"], expected)

    def test_stale_capture_and_expired_token_are_rejected(self):
        prepared = self.prepare()
        stale = fresh_snapshot("capture-current", 80.0)
        self.captures.snapshots["capture-current"] = stale
        result = self.preflight(prepared["plan_id"])
        self.assertEqual(result["reason"], "current_capture_stale")

        self.captures.snapshots["capture-current"] = fresh_snapshot(
            "capture-current", 101.0
        )
        prepared = self.prepare()
        ready = self.preflight(prepared["plan_id"])
        self.clock.value = ready["expires_at"]
        expired = self.service.execute(
            execution_token=ready["execution_token"],
            dry_run=True,
        )
        self.assertEqual(expired["reason"], "token_expired")

    def test_live_execution_is_explicitly_unavailable(self):
        prepared = self.prepare()
        ready = self.preflight(prepared["plan_id"])

        result = self.service.execute(
            execution_token=ready["execution_token"],
            dry_run=False,
        )

        self.assertEqual(
            result["reason"], "live_execution_channel_unavailable"
        )
        self.assertFalse(result["safe_for_execution"])


    def test_old_plan_cannot_be_confirmed_later(self):
        prepared = self.prepare()
        self.clock.value = 400.0

        result = self.service.preflight(
            plan_id=prepared["plan_id"],
            current_capture_id="capture-current",
            confirmed=True,
            confirmed_asset_id="ap_001",
            confirmed_action="shutdown_ap",
            permissions=["assets.ap.shutdown"],
        )
        self.assertEqual(result["reason"], "plan_expired")
    def test_concurrent_token_consumption_succeeds_only_once(self):
        prepared = self.prepare()
        ready = self.preflight(prepared["plan_id"])
        barrier = threading.Barrier(3)
        results = []
        result_lock = threading.Lock()

        def consume():
            barrier.wait()
            result = self.service.execute(
                execution_token=ready["execution_token"],
                dry_run=True,
            )
            with result_lock:
                results.append(result)

        workers = [threading.Thread(target=consume) for _ in range(2)]
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join(timeout=2)

        self.assertEqual(len(results), 2)
        self.assertEqual(
            sorted(result["status"] for result in results),
            ["dry_run_ok", "rejected"],
        )


if __name__ == "__main__":
    unittest.main()
