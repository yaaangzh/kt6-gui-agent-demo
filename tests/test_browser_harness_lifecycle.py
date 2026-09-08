"""Targeted lifecycle regressions; no browser, network or model calls."""

import copy
import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from kt6_backend.execution.browser_executor import HarnessBrowserExecutor
from kt6_backend.execution.browser_harness_client import BrowserHarnessClient, BrowserHarnessError
from kt6_backend.execution.grounding import TargetGrounderRegistry
from kt6_backend.execution.models import BrowserAction
from kt6_backend.execution.scenario_runner import ScenarioExecutionError, ScenarioRunner
from kt6_backend.execution.scenario_service import ExecutionScenarioService, ExecutionScenarioServiceError
from kt6_backend.execution.verifier import UIGraphOutcomeVerifier
from tests.test_execution_scenario import (
    TASK, URL, custom_menu_graph, graph, public_url_policy, semantic_plan,
)


class HarnessLifecycleTest(unittest.TestCase):
    def make_client(self, **kwargs):
        return BrowserHarnessClient(
            workspace=Path("runtime_data/harness-lifecycle-unit"),
            url_policy=public_url_policy(),
            **kwargs,
        )

    def make_service(self):
        runner = Mock()
        runner.inspect.return_value = {
            "capture_id": "plan-capture", "graph_id": "plan-graph",
            "ui_graph": graph("plan-capture"), "capture_metrics": {},
        }
        runner.run.return_value = {"steps": []}
        planner = Mock(planner_id="unit", planner_model="unit")
        planner.plan.side_effect = lambda **kw: semantic_plan(
            start_url=kw["start_url"], user_request=kw["user_request"]
        )
        return ExecutionScenarioService(
            root=Path("runtime_data/harness-lifecycle-unit"), runner=runner,
            planner=planner, url_policy=public_url_policy(),
        )

    def test_queued_run_excludes_planning_and_other_runs_before_worker_starts(self):
        service = self.make_service()
        with patch("kt6_backend.execution.scenario_service.threading.Thread.start"):
            queued = service.start_run(plan=semantic_plan(), confirmed=True)
        self.assertEqual(queued["status"], "queued")
        for operation in (
            lambda: service.generate_plan(start_url=URL, user_request=TASK),
            lambda: service.start_run(plan=semantic_plan(), confirmed=True),
            lambda: service.run_sync(semantic_plan()),
        ):
            with self.assertRaisesRegex(ExecutionScenarioServiceError, "execution_runner_busy"):
                operation()
        service.runner.inspect.assert_not_called()
        service.planner.plan.assert_not_called()
        service._run(queued["run_id"], semantic_plan(), "")
        self.assertEqual(service.get_run(queued["run_id"])["status"], "success")
        service.generate_plan(start_url=URL, user_request=TASK)
        service.planner.plan.assert_called_once()

    def test_planning_excludes_other_requests_and_releases_after_failure(self):
        service = self.make_service()
        entered = threading.Event()
        release = threading.Event()

        def plan(**_kwargs):
            entered.set()
            if not release.wait(2):
                raise AssertionError("test did not release planner")
            raise ValueError("invalid plan")

        service.planner.plan.side_effect = plan
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(service.generate_plan, start_url=URL, user_request=TASK)
            try:
                self.assertTrue(entered.wait(2))
                for operation in (
                    lambda: service.generate_plan(start_url=URL, user_request=TASK),
                    lambda: service.start_run(plan=semantic_plan(), confirmed=True),
                ):
                    with self.assertRaisesRegex(ExecutionScenarioServiceError, "execution_runner_busy"):
                        operation()
                service.runner.inspect.assert_called_once()
            finally:
                release.set()
            with self.assertRaises(ValueError):
                future.result(timeout=2)
        self.assertEqual(service.run_sync(semantic_plan())["status"], "success")

    def test_worker_errors_always_finish_and_release_even_if_evidence_write_fails(self):
        for error, code in (
            (BrowserHarnessError("browser_harness_daemon_unavailable"), "browser_harness_daemon_unavailable"),
            (RuntimeError("private dependency details"), "execution_scenario_failed"),
        ):
            with self.subTest(code=code):
                service = self.make_service()
                service.runner.run.side_effect = error
                with patch("pathlib.Path.exists", return_value=True), patch(
                    "kt6_backend.execution.scenario_service._write_json", side_effect=OSError("disk full")
                ), self.assertRaisesRegex(ExecutionScenarioServiceError, code):
                    service.run_sync(semantic_plan())
                failed = service.get_run(next(iter(service._runs)))
                self.assertEqual(failed["status"], "failed")
                self.assertEqual(failed["evidence_error_code"], "execution_evidence_write_failed")
                self.assertNotIn("private dependency details", str(failed))
                self.assertEqual(service.health()["active_runs"], 0)
                service.runner.run.side_effect = None
                self.assertEqual(service.run_sync(semantic_plan())["status"], "success")

    def test_worker_start_failure_releases_reservation(self):
        service = self.make_service()
        with patch(
            "kt6_backend.execution.scenario_service.threading.Thread.start", side_effect=RuntimeError()
        ), self.assertRaisesRegex(ExecutionScenarioServiceError, "execution_worker_start_failed"):
            service.start_run(plan=semantic_plan(), confirmed=True)
        self.assertEqual(service.health()["active_runs"], 0)
        self.assertEqual(service.run_sync(semantic_plan())["status"], "success")

    def test_queued_run_can_be_cancelled_without_starting_browser_actions(self):
        service = self.make_service()
        with patch("kt6_backend.execution.scenario_service.threading.Thread.start"):
            queued = service.start_run(plan=semantic_plan(), confirmed=True)

        cancelling = service.cancel_run(queued["run_id"])
        self.assertEqual(cancelling["status"], "cancelling")
        self.assertEqual(service.health()["active_runs"], 1)

        service._run(queued["run_id"], semantic_plan(), "")

        cancelled = service.get_run(queued["run_id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["error_code"], "execution_cancelled")
        self.assertEqual(cancelled["error_category"], "cancelled")
        self.assertEqual(service.health()["active_runs"], 0)
        service.runner.run.assert_not_called()

    def test_runner_maps_initial_binding_error_and_releases_session(self):
        client = self.make_client(cdp_call=Mock(), click_call=Mock())
        runner = ScenarioRunner(
            page_perception=Mock(), browser_executor=HarnessBrowserExecutor(client),
            grounders=Mock(), verifiers=Mock(), url_policy=public_url_policy(),
        )
        with patch.object(
            client, "open_or_bind_target", side_effect=BrowserHarnessError("browser_harness_permission_required")
        ), patch("pathlib.Path.mkdir"), patch("kt6_backend.execution.scenario_runner._write_json"):
            with self.assertRaisesRegex(ScenarioExecutionError, "browser_harness_permission_required"):
                runner.run(semantic_plan(), run_id="unit", out_dir=Path("unused"), confirmed=True)
        with ThreadPoolExecutor(max_workers=1) as pool:
            def acquire():
                with client.exclusive_session():
                    return True
            self.assertTrue(pool.submit(acquire).result(timeout=2))

    def test_session_lease_is_reentrant_but_rejects_another_thread(self):
        client = self.make_client(cdp_call=Mock(), click_call=Mock())
        executor = HarnessBrowserExecutor(client)
        target = TargetGrounderRegistry().resolve(semantic_plan()["steps"][0]["target"], graph("unit"))
        with client.exclusive_session(), client.exclusive_session(), ThreadPoolExecutor(max_workers=1) as pool:
            def acquire():
                with client.exclusive_session():
                    self.fail("another thread entered the active session")
            with self.assertRaisesRegex(BrowserHarnessError, "execution_runner_busy"):
                pool.submit(acquire).result(timeout=2)
            result = pool.submit(executor.execute, BrowserAction("click", target)).result(timeout=2)
            self.assertFalse(result.success)
            self.assertEqual(result.error_code, "execution_runner_busy")
            client._cdp_call.assert_not_called()
            client._click_call.assert_not_called()

    def test_official_adapter_pins_page_and_mouse_commands_to_explicit_session(self):
        client = self.make_client()
        calls = []
        active = ["target-unit"]

        def cdp(method, session_id=None, **params):
            calls.append((method, session_id, params))
            if method == "Target.getTargets":
                return {"targetInfos": [{"targetId": "target-unit", "type": "page", "url": URL}]}
            if method == "Page.getFrameTree":
                return {"frameTree": {"frame": {"url": URL}}}
            # Simulate an external default-session change between mouse events.
            active[0] = "target-other"
            return {}

        helpers = SimpleNamespace(
            AGENT_WORKSPACE=client.workspace, cdp=cdp,
            switch_tab=Mock(return_value="session-unit"),
            current_tab=lambda: {"targetId": active[0]},
        )
        admin = SimpleNamespace(ensure_daemon=Mock())
        with patch.dict(os.environ, {}, clear=True), patch("pathlib.Path.mkdir"), patch(
            "kt6_backend.execution.browser_harness_client.importlib.import_module",
            side_effect=lambda name: {"browser_harness.helpers": helpers, "browser_harness.admin": admin}[name],
        ):
            client.open_or_bind_target(URL, target_id="target-unit")
            client._click_call(20, 30)
        page_calls = [call for call in calls if call[0].startswith(("Page.", "Input."))]
        self.assertEqual([call[1] for call in page_calls], ["session-unit"] * 3)
        mouse = [call for call in calls if call[0] == "Input.dispatchMouseEvent"]
        self.assertEqual([call[2]["type"] for call in mouse], ["mousePressed", "mouseReleased"])
        self.assertEqual(len(calls), 4)
        admin.ensure_daemon.assert_called_once()

    def test_new_binding_recovers_dead_daemon_without_replaying_actions(self):
        def cdp(method, **_params):
            if method == "Browser.getVersion":
                raise ConnectionError("disconnected")
            if method == "Target.getTargets":
                return {"targetInfos": [{"targetId": "target-unit", "type": "page", "url": URL}]}
            return {"frameTree": {"frame": {"url": URL}}}
        ensure = Mock()
        click = Mock()
        client = self.make_client(
            cdp_call=cdp, click_call=click, ensure_daemon_call=ensure,
            switch_tab_call=Mock(), current_tab_call=lambda: "target-unit",
        )
        client._ready = True
        client.open_or_bind_target(URL, target_id="target-unit")
        ensure.assert_called_once()
        click.assert_not_called()

    def test_navigation_waits_past_repeated_old_url_without_full_captures(self):
        urls = iter([URL, URL, URL, "about:blank", URL + "/next", URL + "/next"])
        cdp = Mock(side_effect=lambda *_args, **_kw: {"frameTree": {"frame": {"url": next(urls)}}})
        client = self.make_client(cdp_call=cdp, click_call=Mock())
        with patch("kt6_backend.execution.browser_harness_client.time.sleep"):
            self.assertEqual(client.wait_for_page_settle(previous_url=URL), URL + "/next")
        self.assertEqual(cdp.call_count, 6)
        self.assertTrue(all(call.args == ("Page.getFrameTree",) for call in cdp.call_args_list))

    def test_navigation_with_no_change_still_fails(self):
        client = self.make_client(
            cdp_call=Mock(return_value={"frameTree": {"frame": {"url": URL}}}), click_call=Mock(),
        )
        with patch("kt6_backend.execution.browser_harness_client.time.monotonic", side_effect=[0, 0, 4]), patch(
            "kt6_backend.execution.browser_harness_client.time.sleep"
        ), self.assertRaisesRegex(BrowserHarnessError, "browser_navigation_timeout"):
            client.wait_for_page_settle(previous_url=URL)

    def test_repeated_input_and_selection_are_postconditions_with_fresh_evidence(self):
        verifier = UIGraphOutcomeVerifier()
        before = graph("before")
        after = graph("after")
        for value in (before, after):
            value["nodes"][0].update(name="搜索框", role="textbox")
            value["nodes"][0]["attributes"]["value"] = "ainfra"
            value["nodes"][0]["attributes"]["aria-selected"] = "true"
        for expected in (
            {"type": "input_value", "target": {"query": "搜索框", "role": "textbox"}, "value": "ainfra"},
            {"type": "selected", "target": {"query": "搜索框"}},
        ):
            with self.subTest(expected=expected):
                self.assertTrue(verifier.verify(expected=expected, before=before, after=after))
                self.assertFalse(verifier.verify(expected=expected, before=after, after=after))
                ambiguous = copy.deepcopy(after)
                ambiguous["nodes"].append(dict(ambiguous["nodes"][0], id="cdp:duplicate"))
                self.assertFalse(verifier.verify(expected=expected, before=before, after=ambiguous))
        after["nodes"][0]["attributes"]["value"] = "wrong value"
        self.assertFalse(verifier.verify(
            expected={"type": "input_value", "target": {"query": "搜索框"}, "value": "ainfra"},
            before=before, after=after,
        ))

    def test_reselecting_custom_menu_value_accepts_unchanged_trigger_after_menu_closes(self):
        expected = {"type": "selected", "target": {"query": "近7天", "role": "menuitem"}}
        verifier = UIGraphOutcomeVerifier()
        before = custom_menu_graph("before", menu_open=True, selected=True)
        after = custom_menu_graph("after", selected=True)
        self.assertTrue(verifier.verify(expected=expected, before=before, after=after))
        self.assertFalse(verifier.verify(
            expected=expected, before=before, after=custom_menu_graph("wrong", selected=False)
        ))

    def test_multiline_request_survives_plan_validation_without_extra_model_call(self):
        service = self.make_service()
        request = "点击时间维度\n选择近7天，  然后导出"
        generated = service.generate_plan(start_url=URL, user_request=request)
        self.assertEqual(generated["plan"]["user_request"], request)
        service.planner.plan.assert_called_once()


if __name__ == "__main__":
    unittest.main()
