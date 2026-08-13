from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kt6_backend.browser_use_evaluation import (
    BrowserUseEvaluationConfig,
    BrowserUseRunResult,
    BrowserUseStep,
    deterministic_page_validation,
    run_browser_use_evaluation,
)
from kt6_backend.browser_use_evaluation_cli import main as cli_main
from kt6_backend.evaluation_executor import EvaluationExecutionError, ExecutionTask
from kt6_backend.evaluation_report import (
    build_suite_template,
    load_run_records,
    verify_run_evidence,
)


class _FakeBackend:
    def __init__(self, result: BrowserUseRunResult):
        self.result = result
        self.calls = []

    async def execute(self, task, config):
        self.calls.append((task, config))
        return self.result


class BrowserUseEvaluationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.suite = build_suite_template(
            suite_id="suite-1",
            title="Browser Use comparison",
            task_count=1,
            repetitions=1,
            planner_provider="test-gateway",
            planner_model="generic-model-test",
            environment_id="test-env",
        )
        self.suite["status"] = "ready"
        self.suite["tasks"][0].update(
            {
                "title": "Open alarms",
                "validation": {
                    "method": "page_state",
                    "description": "Alarm page is visible",
                },
            }
        )
        self.task = ExecutionTask(
            suite_id="suite-1",
            task_id="T01",
            instruction="Open the alarm page.",
            start_url="https://nce.example.test/home",
            allowed_hosts=frozenset({"nce.example.test"}),
            step_limit=2,
            validation={
                "method": "page_state",
                "assertions": [
                    {"kind": "url_contains", "value": "/alarms"},
                    {"kind": "text_contains", "value": "Critical alarm"},
                ],
            },
        )
        self.config = BrowserUseEvaluationConfig(
            base_url="http://127.0.0.1:9000/v1",
            api_key="secret-test-key",
            provider="test-gateway",
            model="generic-model-test",
            max_steps=2,
        )

    def tearDown(self):
        self.temp.cleanup()

    def result(self, *, outside_navigation=False):
        first_action = (
            {"navigate": {"url": "https://evil.example.test/"}}
            if outside_navigation
            else {"click": {"index": 4}}
        )
        return BrowserUseRunResult(
            steps=(
                BrowserUseStep(
                    response={"action": [first_action], "memory": "opened alarms"},
                    actions=(first_action,),
                    page_state={
                        "url": "https://nce.example.test/alarms",
                        "title": "Alarms",
                    },
                    elapsed_ms=101,
                ),
                BrowserUseStep(
                    response={"action": [{"done": {"text": "complete"}}]},
                    actions=({"done": {"text": "complete"}},),
                    page_state={
                        "url": "https://nce.example.test/alarms",
                        "title": "Alarms",
                    },
                    elapsed_ms=55,
                ),
            ),
            elements=(
                {
                    "node_id": "alarm-row-1",
                    "role": "row",
                    "name": "Critical alarm",
                },
            ),
            final_state={
                "url": "https://nce.example.test/alarms",
                "title": "Alarms",
            },
            model_claimed_success=True,
            input_tokens=120,
            output_tokens=30,
            browser_label="Chromium test",
        )

    def execute(self, result):
        return asyncio.run(
            run_browser_use_evaluation(
                suite=self.suite,
                runs_path=self.root / "evaluation" / "runs.jsonl",
                workspace_root=self.root / "raw",
                task=self.task,
                repetition=1,
                config=self.config,
                implementation={
                    "name": "KT6 Browser Use Model API",
                    "version": "browser-use-0.13.7",
                    "revision": "abc123",
                    "branch": "eval-browser-use",
                },
                environment={
                    "environment_id": "test-env",
                    "browser": "Chromium test",
                    "viewport": "1920x1080",
                },
                backend=_FakeBackend(result),
            )
        )

    def test_remote_api_and_non_loopback_cdp_require_explicit_safe_configuration(self):
        with self.assertRaises(EvaluationExecutionError):
            BrowserUseEvaluationConfig(
                base_url="http://127.0.0.1:9000/v1",
                api_key="key",
                provider=" ",
                model="generic-model-test",
            )
        with self.assertRaises(EvaluationExecutionError):
            BrowserUseEvaluationConfig(
                base_url="https://model-gateway.test/v1",
                api_key="key",
                provider="test-gateway",
                model="generic-model-test",
                api_allowed_hosts=frozenset({"model-gateway.test"}),
            )
        configured = BrowserUseEvaluationConfig(
            base_url="https://model-gateway.test/v1",
            api_key="key",
            provider="test-gateway",
            model="generic-model-test",
            api_allowed_hosts=frozenset({"model-gateway.test"}),
            allow_remote_model=True,
        )
        self.assertNotIn("key", repr(configured))
        with self.assertRaises(EvaluationExecutionError):
            BrowserUseEvaluationConfig(
                base_url="http://127.0.0.1:9000/v1",
                api_key="key",
                provider="test-gateway",
                model="generic-model-test",
                cdp_url="http://192.168.1.10:9222",
            )

    def test_deterministic_validation_does_not_trust_agent_success_claim(self):
        failed = self.result()
        failed = BrowserUseRunResult(
            **{
                **failed.__dict__,
                "final_state": {
                    "url": "https://nce.example.test/home",
                    "title": "Home",
                },
                "elements": ({"name": "Normal status"},),
                "model_claimed_success": True,
            }
        )
        self.assertFalse(deterministic_page_validation(self.task, failed))

    def test_archives_browser_use_dom_planner_actions_and_validation(self):
        recorded = self.execute(self.result())
        self.assertEqual(recorded["outcome"], "success")
        self.assertEqual(recorded["metrics"]["planner_model_calls"], 2)
        self.assertEqual(recorded["metrics"]["input_tokens"], 120)
        self.assertEqual(recorded["planner"]["provider"], "test-gateway")
        self.assertEqual(recorded["planner"]["model"], "generic-model-test")
        self.assertEqual(recorded["evidence"]["status"], "archived")
        loaded = load_run_records(self.root / "evaluation" / "runs.jsonl")
        self.assertEqual(len(loaded), 1)
        verification = verify_run_evidence(
            self.root / "evaluation", self.suite, loaded[0]
        )
        self.assertTrue(verification["verified"], verification["errors"])
        manifest_path = self.root / "evaluation" / recorded["evidence"]["manifest_ref"]
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        roles = {item["role"] for item in manifest["artifacts"]}
        self.assertTrue(
            {"dom_snapshot", "planner_result", "action_trace", "validation_result"}
            <= roles
        )

    def test_navigation_outside_task_allowlist_is_a_ranked_safety_failure(self):
        recorded = self.execute(self.result(outside_navigation=True))
        self.assertEqual(recorded["outcome"], "failure")
        self.assertEqual(recorded["metrics"]["safety_violation_count"], 1)
        self.assertFalse(recorded["validation"]["passed"])

    def test_cli_error_is_fixed_and_does_not_echo_secret_or_paths(self):
        task_path = self.root / "task-secret.json"
        task_path.write_text("{}", encoding="utf-8")
        with patch.dict(os.environ, {"KT6_MODEL_API_KEY": "SECRET-KEY"}, clear=True):
            with patch("sys.stderr") as stderr:
                status = cli_main(
                    [
                        "--suite",
                        str(self.root / "SECRET-suite.json"),
                        "--task",
                        str(task_path),
                        "--runs",
                        str(self.root / "runs.jsonl"),
                        "--workspace",
                        str(self.root / "raw"),
                        "--repetition",
                        "1",
                        "--implementation-revision",
                        "abc",
                        "--environment-id",
                        "test-env",
                    ]
                )
        output = "".join(str(call) for call in stderr.write.call_args_list)
        self.assertEqual(status, 3)
        self.assertNotIn("SECRET", output)
        self.assertNotIn(str(self.root), output)


if __name__ == "__main__":
    unittest.main()
