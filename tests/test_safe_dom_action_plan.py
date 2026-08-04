import copy
import json
import unittest

from kt6_backend.asset_inventory import (
    AssetResolver,
    InMemoryAssetInventoryAdapter,
)
from kt6_backend.dom_action_binding import DOMActionBindingService
from kt6_backend.safe_dom_actions import SafeDOMActionService
from tests.test_dom_action_binding import ASSETS, device_snapshot
from tests.test_safe_dom_actions import DictCaptureProvider, FakeClock


def snapshot(capture_id: str, created_at: float) -> dict:
    value = device_snapshot(capture_id)
    value["created_at"] = created_at
    value["content_hash"] = f"content-{capture_id}"
    return value


class SafeDOMActionOperationPlanTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(100.0)
        self.initial = snapshot("capture-initial", 99.0)
        self.current = snapshot("capture-current", 101.0)
        self.captures = DictCaptureProvider([self.initial, self.current])
        inventory = InMemoryAssetInventoryAdapter(ASSETS)
        binder = DOMActionBindingService(AssetResolver(inventory))
        self.service = SafeDOMActionService(
            binder,
            self.captures,
            clock=self.clock,
            capture_max_age_seconds=10,
            token_ttl_seconds=5,
            plan_ttl_seconds=30,
        )

    def prepare(self) -> dict:
        return self.service.prepare(
            asset_reference="AP1",
            action="关闭",
            page_capture_id="capture-initial",
            scope={"site_id": "site-a"},
            task_id="task-secret",
            principal_id="principal-secret",
        )

    def preflight(self, plan_id: str) -> dict:
        self.clock.value = 101.0
        return self.service.preflight(
            plan_id=plan_id,
            current_capture_id="capture-current",
            confirmed=True,
            confirmed_asset_id="ap_001",
            confirmed_action="shutdown_ap",
            permissions=["assets.ap.shutdown"],
        )

    @staticmethod
    def step_status(operation_plan: dict, step_id: str) -> str:
        return next(
            step["status"]
            for step in operation_plan["steps"]
            if step["step_id"] == step_id
        )

    def test_prepare_returns_clear_plan_and_get_plan_is_redacted(self):
        prepared = self.prepare()

        operation_plan = prepared["operation_plan"]
        self.assertEqual(operation_plan["status"], "prepared")
        self.assertEqual(
            [step["step_id"] for step in operation_plan["steps"]],
            [step_id for step_id, _name in self.service.OPERATION_STEPS],
        )
        self.assertEqual(
            self.step_status(operation_plan, "bind_target"), "completed"
        )
        self.assertEqual(
            self.step_status(operation_plan, "fresh_capture_revalidation"),
            "pending",
        )

        status = self.service.get_plan(prepared["plan_id"])
        serialized = json.dumps(status, sort_keys=True)
        self.assertEqual(status["status"], "prepared")
        self.assertFalse(status["safe_for_execution"])
        self.assertNotIn("principal-secret", serialized)
        self.assertNotIn("task-secret", serialized)
        self.assertNotIn("binding", status)
        self.assertNotIn("scope", status)
        self.assertNotIn("execution_token", serialized)

    def test_preflight_and_dry_run_advance_operation_steps(self):
        prepared = self.prepare()
        ready = self.preflight(prepared["plan_id"])

        self.assertEqual(ready["operation_plan"]["status"], "ready")
        self.assertEqual(
            self.step_status(
                ready["operation_plan"], "confirm_and_authorize"
            ),
            "completed",
        )
        self.assertEqual(
            self.step_status(
                ready["operation_plan"], "fresh_capture_revalidation"
            ),
            "completed",
        )
        self.assertEqual(
            self.step_status(ready["operation_plan"], "final_revalidation"),
            "ready",
        )

        result = self.service.execute(
            execution_token=ready["execution_token"], dry_run=True
        )

        plan = result["operation_plan"]
        self.assertEqual(plan["status"], "dry_run_ok")
        self.assertEqual(
            self.step_status(plan, "final_revalidation"), "completed"
        )
        self.assertEqual(self.step_status(plan, "execute"), "completed")
        self.assertEqual(
            self.step_status(plan, "verify_outcome"), "skipped"
        )
        self.assertFalse(result["executed"])
        self.assertFalse(result["safe_for_execution"])

    def test_live_execution_stays_blocked_and_is_visible_in_plan(self):
        prepared = self.prepare()
        ready = self.preflight(prepared["plan_id"])

        result = self.service.execute(
            execution_token=ready["execution_token"], dry_run=False
        )

        self.assertEqual(result["reason"], "live_execution_channel_unavailable")
        self.assertEqual(result["operation_plan"]["status"], "blocked")
        self.assertEqual(
            self.step_status(result["operation_plan"], "execute"), "blocked"
        )
        self.assertFalse(result["safe_for_execution"])

    def test_explicitly_truncated_capture_fails_closed_at_each_phase(self):
        truncated_initial = copy.deepcopy(self.initial)
        truncated_initial["dom"]["stats"] = {"truncated": True}
        self.captures.snapshots["capture-initial"] = truncated_initial
        rejected = self.prepare()
        self.assertEqual(rejected["reason"], "dom_evidence_truncated")

        self.captures.snapshots["capture-initial"] = copy.deepcopy(self.initial)
        prepared = self.prepare()
        incomplete_current = copy.deepcopy(self.current)
        incomplete_current["dom"]["coverage"] = {"source_complete": False}
        self.captures.snapshots["capture-current"] = incomplete_current
        rejected = self.preflight(prepared["plan_id"])
        self.assertEqual(rejected["reason"], "dom_evidence_incomplete")
        self.assertEqual(
            self.step_status(
                rejected["operation_plan"], "fresh_capture_revalidation"
            ),
            "blocked",
        )

        self.captures.snapshots["capture-current"] = copy.deepcopy(self.current)
        prepared = self.prepare()
        ready = self.preflight(prepared["plan_id"])
        final_snapshot = copy.deepcopy(self.current)
        final_snapshot["dom"]["truncated"] = True
        self.captures.snapshots["capture-current"] = final_snapshot
        rejected = self.service.execute(
            execution_token=ready["execution_token"], dry_run=True
        )
        self.assertEqual(rejected["reason"], "dom_evidence_truncated")
        self.assertEqual(
            self.step_status(
                rejected["operation_plan"], "final_revalidation"
            ),
            "blocked",
        )

    def test_missing_coverage_remains_backward_compatible(self):
        self.assertNotIn("coverage", self.initial["dom"])
        self.assertNotIn("stats", self.initial["dom"])

        prepared = self.prepare()
        ready = self.preflight(prepared["plan_id"])

        self.assertEqual(ready["status"], "ready")

    def test_get_plan_marks_unused_expired_plan_without_leaking_state(self):
        prepared = self.prepare()
        self.clock.value = 130.0

        status = self.service.get_plan(prepared["plan_id"])

        self.assertEqual(status["status"], "expired")
        self.assertEqual(status["reason"], "plan_expired")
        self.assertFalse(status["safe_for_execution"])

    def test_get_plan_marks_ready_plan_expired_with_its_token(self):
        prepared = self.prepare()
        ready = self.preflight(prepared["plan_id"])
        self.clock.value = ready["expires_at"]

        status = self.service.get_plan(prepared["plan_id"])

        self.assertEqual(status["status"], "expired")
        self.assertEqual(status["reason"], "token_expired")
        self.assertEqual(
            self.step_status(status["operation_plan"], "final_revalidation"),
            "blocked",
        )

    def test_malformed_shadow_coverage_fails_closed(self):
        malformed = copy.deepcopy(self.initial)
        malformed["dom"]["stats"] = {"open_shadow_root_count": "unknown"}
        self.captures.snapshots["capture-initial"] = malformed

        rejected = self.prepare()

        self.assertEqual(rejected["reason"], "dom_evidence_incomplete")

    def test_compressed_tree_can_still_have_complete_action_evidence(self):
        compressed = copy.deepcopy(self.initial)
        compressed["dom"]["coverage"] = {
            "source_complete": False,
            "compressed": True,
            "action_binding_complete": True,
        }
        self.captures.snapshots["capture-initial"] = compressed

        prepared = self.prepare()

        self.assertEqual(prepared["status"], "prepared")

    def test_explicit_action_coverage_gaps_fail_closed(self):
        cases = (
            (
                {"coverage": {"action_binding_complete": False}},
                "dom_evidence_incomplete",
            ),
            (
                {"stats": {"scan_truncated": True}},
                "dom_evidence_truncated",
            ),
            (
                {"stats": {"open_shadow_root_count": 1}},
                "dom_evidence_incomplete",
            ),
        )
        for dom_update, expected_reason in cases:
            with self.subTest(dom_update=dom_update):
                current = copy.deepcopy(self.initial)
                current["dom"].update(dom_update)
                self.captures.snapshots["capture-initial"] = current

                rejected = self.prepare()

                self.assertEqual(rejected["reason"], expected_reason)


if __name__ == "__main__":
    unittest.main()
