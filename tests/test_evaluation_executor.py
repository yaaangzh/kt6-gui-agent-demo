from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from kt6_backend.evaluation_executor import (
    EXECUTION_TASK_SCHEMA_VERSION,
    EvaluationExecutionError,
    EvaluationWorkspace,
    action_event,
    build_final_run_record,
    load_execution_task,
    validation_result,
)
from kt6_backend.evaluation_report import build_suite_template


class EvaluationExecutorTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def task_payload(self):
        return {
            "schema_version": EXECUTION_TASK_SCHEMA_VERSION,
            "suite_id": "suite-1",
            "task_id": "T01",
            "instruction": "Open the alarm page.",
            "start_url": "https://nce.example.test/home",
            "allowed_hosts": ["nce.example.test"],
            "step_limit": 10,
            "validation": {
                "method": "page_state",
                "description": "Alarm page is visible",
            },
        }

    def write_task(self, payload=None):
        path = self.root / "task.json"
        path.write_text(json.dumps(payload or self.task_payload()), encoding="utf-8")
        return path

    def test_loads_task_and_enforces_navigation_allowlist(self):
        task = load_execution_task(self.write_task())
        self.assertTrue(task.allows_url("https://nce.example.test/alarm"))
        self.assertFalse(task.allows_url("https://other.example.test/"))
        self.assertFalse(task.allows_url("javascript:alert(1)"))

    def test_rejects_remote_http_and_start_url_outside_allowlist(self):
        for start_url in (
            "http://nce.example.test/home",
            "https://other.example.test/home",
            "https://user:pass@nce.example.test/home",
        ):
            payload = self.task_payload()
            payload["start_url"] = start_url
            with self.subTest(start_url=start_url), self.assertRaises(
                EvaluationExecutionError
            ):
                load_execution_task(self.write_task(payload))

    def test_workspace_writes_explicit_numbered_artifacts(self):
        workspace = EvaluationWorkspace(self.root / "runs", "current-T01-r1")
        first = workspace.write_json("cv_result", {"value": 1})
        second = workspace.write_json("cv_result", {"value": 2})
        trace = workspace.write_jsonl(
            "action_trace",
            [
                action_event(
                    run_id="current-T01-r1",
                    step_index=1,
                    action="locate",
                    elapsed_ms=5,
                )
            ],
        )
        self.assertEqual(first.name, "cv-result-001.json")
        self.assertEqual(second.name, "cv-result-002.json")
        self.assertEqual(trace.name, "action-trace-001.jsonl")
        self.assertEqual(len(workspace.artifact_sources["cv_result"]), 2)

    def test_workspace_is_exclusive_and_rejects_role_extension_mismatch(self):
        EvaluationWorkspace(self.root / "runs", "current-T01-r1")
        with self.assertRaises(EvaluationExecutionError):
            EvaluationWorkspace(self.root / "runs", "current-T01-r1")
        other = EvaluationWorkspace(self.root / "runs", "current-T01-r2")
        with self.assertRaises(EvaluationExecutionError):
            other.write_bytes("original_screenshot", b"image", extension="json")

    def test_workspace_accepts_physical_image_suffix_for_image_role(self):
        workspace = EvaluationWorkspace(self.root / "runs", "ui_tars-T01-r1")
        screenshot = workspace.write_bytes(
            "original_screenshot", b"png-bytes", extension="png"
        )
        self.assertEqual(screenshot.name, "original-screenshot-001.png")

    def test_builds_contract_valid_final_record(self):
        suite = build_suite_template(
            suite_id="suite-1",
            title="Comparison",
            task_count=1,
            repetitions=1,
            planner_provider="deepseek",
            planner_model="test-model",
            environment_id="test-env",
        )
        suite["status"] = "ready"
        suite["tasks"][0].update(
            {
                "title": "Open alarm page",
                "validation": {
                    "method": "page_state",
                    "description": "Alarm page is visible",
                },
            }
        )
        record = build_final_run_record(
            suite,
            scheme_id="browser_use",
            task_id="T01",
            repetition=1,
            outcome="success",
            started_at="2026-08-13T00:00:00Z",
            duration_ms=1234,
            step_count=1,
            implementation={
                "name": "Browser Use API",
                "version": "1.0",
                "revision": "abc123",
                "branch": "eval-browser-use",
            },
            planner={
                "provider": "deepseek",
                "model": "test-model",
                "adapter_prompt_version": "browser-use-v1",
            },
            environment={
                "environment_id": "test-env",
                "browser": "Chromium test",
                "viewport": "1920x1080",
            },
            metrics={
                "first_target_hit": True,
                "planner_model_calls": 1,
                "input_tokens": 10,
                "output_tokens": 5,
            },
            validation_passed=True,
            validation_method="page_state",
        )
        self.assertEqual(record["record_status"], "final")
        self.assertEqual(record["model_calls"], 1)
        self.assertIsNone(record["failure"])

    def test_validation_envelope_uses_required_identity(self):
        result = validation_result(
            run_id="ui_tars-T01-r1",
            task_id="T01",
            method="page_state",
            passed=False,
        )
        self.assertEqual(result["evidence_id"], "validation_result-001")
        self.assertFalse(result["passed"])


if __name__ == "__main__":
    unittest.main()
