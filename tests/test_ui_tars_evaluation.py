from __future__ import annotations

import asyncio
import binascii
import hashlib
import json
import os
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from kt6_backend.evaluation_executor import EvaluationExecutionError, ExecutionTask
from kt6_backend.evaluation_report import (
    build_suite_template,
    load_run_records,
    verify_run_evidence,
)
from kt6_backend.ui_tars_evaluation import (
    BrowserObservation,
    ModelEndpointConfig,
    PlannerDecision,
    UITarsAPIModel,
    UITarsAction,
    UITarsEvaluationConfig,
    UITarsPrediction,
    map_ui_tars_point,
    parse_ui_tars_action,
    run_ui_tars_evaluation,
)
from kt6_backend.ui_tars_evaluation_cli import main as cli_main


def _png(width=2, height=2):
    def chunk(kind, body):
        return (
            struct.pack(">I", len(body))
            + kind
            + body
            + struct.pack(">I", binascii.crc32(kind + body) & 0xFFFFFFFF)
        )

    rows = b"".join(b"\x00" + b"\xff\x00\x00\xff" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


class _Planner:
    def __init__(self):
        self.calls = []

    def plan(self, *, task, observation_ref, action_history):
        self.calls.append((task, observation_ref, list(action_history)))
        return PlannerDecision(
            payload={"goal": "Open and verify alarms", "status": "continue"},
            input_tokens=10,
            output_tokens=3,
        )


class _Vision:
    def __init__(self, actions=None, fail=False):
        self.actions = list(
            actions
            or [
                UITarsAction(kind="click", start=(500, 500)),
                UITarsAction(kind="finished"),
            ]
        )
        self.fail = fail
        self.calls = []

    def predict(self, *, task, goal, observation, action_history):
        self.calls.append((task, goal, observation, list(action_history)))
        if self.fail:
            raise EvaluationExecutionError("SECRET model output")
        action = self.actions[len(self.calls) - 1]
        return UITarsPrediction(
            raw_text=f"Thought: next\nAction: {action.kind}()",
            response={"prediction": "bounded", "parsed_action": action.as_dict()},
            action=action,
            input_tokens=20,
            output_tokens=5,
        )


class _Operator:
    def __init__(self):
        self.started = False
        self.closed = False
        self.executed = []
        self.state = {
            "url": "https://nce.example.test/home",
            "title": "Home",
            "body_text": "Home",
        }

    async def start(self, task):
        self.started = True

    async def observe(self):
        return BrowserObservation(
            png=_png(),
            width=1000,
            height=800,
            url=self.state["url"],
            title=self.state["title"],
        )

    async def execute(self, action, observation, config):
        self.executed.append(action)
        if config.execute_actions and action.kind == "click":
            self.state = {
                "url": "https://nce.example.test/alarms",
                "title": "Alarms",
                "body_text": "Critical alarm",
            }
        return {"status": "completed" if config.execute_actions else "dry_run"}

    async def final_state(self):
        return dict(self.state)

    async def close(self):
        self.closed = True


class _ChatResult:
    content = "Thought: click target\nAction: click(start_box='(500,250)')"
    response_id = "response-1"
    model = "ui-tars-test"
    usage = {"input_tokens": 7, "output_tokens": 2, "total_tokens": 9}


class _ChatClient:
    def __init__(self):
        self.calls = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        return _ChatResult()


class UITarsEvaluationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.suite = build_suite_template(
            suite_id="suite-1",
            title="UI-TARS comparison",
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
                "scenario_type": "mixed",
                "validation": {
                    "method": "page_state",
                    "description": "Alarm page visible",
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
        self.planner_endpoint = ModelEndpointConfig(
            base_url="http://127.0.0.1:9001/v1",
            api_key="planner-secret",
            provider="test-gateway",
            model="generic-model-test",
        )
        self.vision_endpoint = ModelEndpointConfig(
            base_url="http://127.0.0.1:9002/v1",
            api_key="vision-secret",
            provider="ui-tars",
            model="ui-tars-test",
        )

    def tearDown(self):
        self.temp.cleanup()

    def config(self, *, execute=True):
        return UITarsEvaluationConfig(
            planner=self.planner_endpoint,
            vision=self.vision_endpoint,
            execute_actions=execute,
            max_steps=2,
            viewport_width=1000,
            viewport_height=800,
        )

    def execute(self, *, config=None, vision=None, operator=None):
        return asyncio.run(
            run_ui_tars_evaluation(
                suite=self.suite,
                runs_path=self.root / "evaluation" / "runs.jsonl",
                workspace_root=self.root / "raw",
                task=self.task,
                repetition=1,
                config=config or self.config(),
                implementation={
                    "name": "KT6 UI-TARS API",
                    "version": "1.0",
                    "revision": "abc123",
                    "branch": "eval-ui-tars",
                },
                environment={
                    "environment_id": "test-env",
                    "browser": "Playwright Chromium test",
                    "viewport": "1000x800",
                },
                planner=_Planner(),
                vision_model=vision or _Vision(),
                operator=operator or _Operator(),
            )
        )

    def test_strict_action_parser_and_coordinate_mapping(self):
        action = parse_ui_tars_action(
            "Thought: click the button\nAction: click(start_box='[400,200,600,400]')"
        )
        self.assertEqual(action.start, (500.0, 300.0))
        self.assertEqual(
            map_ui_tars_point(action.start, width=1920, height=1080, mode="scale_1000"),
            (960.0, 324.0),
        )
        for value in (
            "Action: click(start_box=__import__('os').system('whoami'))",
            "Action: hotkey(key='CTRL+L')",
            "Action: wait()\nAction: finished()",
        ):
            with self.subTest(value=value), self.assertRaises(EvaluationExecutionError):
                parse_ui_tars_action(value)
        with self.assertRaises(EvaluationExecutionError):
            map_ui_tars_point((1001, 500), width=1000, height=800, mode="scale_1000")

    def test_remote_endpoint_requires_exact_host_and_explicit_data_egress(self):
        endpoint = ModelEndpointConfig(
            base_url="https://ui-tars.example.test/v1",
            api_key="secret",
            provider="ui-tars",
            model="ui-tars-test",
            allowed_hosts=frozenset({"ui-tars.example.test"}),
        )
        with self.assertRaises(EvaluationExecutionError):
            endpoint.client()
        allowed = ModelEndpointConfig(
            **{**endpoint.__dict__, "allow_remote": True}
        )
        self.assertNotIn("secret", repr(allowed))
        self.assertTrue(allowed.client().endpoint.endswith("/chat/completions"))

    def test_ui_tars_api_sends_png_as_image_content_and_parses_prediction(self):
        model = UITarsAPIModel(self.vision_endpoint)
        fake = _ChatClient()
        model.client = fake
        prediction = model.predict(
            task=self.task,
            goal="click alarm",
            observation=BrowserObservation(
                png=_png(), width=1000, height=800, url="https://nce.example.test", title="NCE"
            ),
            action_history=[],
        )
        content = fake.calls[0]["messages"][1]["content"]
        self.assertEqual(content[1]["type"], "image_url")
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertEqual(prediction.action.kind, "click")

    def test_archives_two_api_calls_screenshots_actions_and_validation(self):
        operator = _Operator()
        recorded = self.execute(operator=operator)
        self.assertEqual(recorded["outcome"], "success")
        self.assertEqual(recorded["metrics"]["planner_model_calls"], 2)
        self.assertEqual(recorded["metrics"]["vision_model_calls"], 2)
        self.assertEqual(recorded["planner"]["provider"], "test-gateway")
        self.assertEqual(recorded["planner"]["model"], "generic-model-test")
        self.assertEqual(recorded["model_calls"], 4)
        self.assertTrue(operator.closed)
        loaded = load_run_records(self.root / "evaluation" / "runs.jsonl")
        verification = verify_run_evidence(
            self.root / "evaluation", self.suite, loaded[0]
        )
        self.assertTrue(verification["verified"], verification["errors"])
        manifest_path = self.root / "evaluation" / recorded["evidence"]["manifest_ref"]
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        roles = [item["role"] for item in manifest["artifacts"]]
        self.assertEqual(roles.count("original_screenshot"), 2)
        self.assertIn("ui_tars_response", roles)
        self.assertIn("planner_result", roles)
        self.assertIn("action_trace", roles)

    def test_dry_run_and_model_failure_are_archived_as_failures(self):
        dry_run = self.execute(config=self.config(execute=False))
        self.assertEqual(dry_run["outcome"], "failure")
        self.assertEqual(dry_run["failure"]["category"], "dry_run_only")

        # Use a second repetition key in a fresh root because evidence is immutable.
        self.tearDown()
        self.setUp()
        model_failure = self.execute(vision=_Vision(fail=True))
        self.assertEqual(model_failure["outcome"], "failure")
        self.assertEqual(model_failure["failure"]["category"], "vision_model_error")
        loaded = load_run_records(self.root / "evaluation" / "runs.jsonl")
        verification = verify_run_evidence(
            self.root / "evaluation", self.suite, loaded[0]
        )
        self.assertTrue(verification["verified"], verification["errors"])

    def test_cli_error_is_fixed_and_does_not_echo_secret_or_paths(self):
        task_path = self.root / "task-secret.json"
        task_path.write_text("{}", encoding="utf-8")
        with patch.dict(
            os.environ,
            {
                "KT6_MODEL_API_KEY": "PLANNER-SECRET",
                "KT6_UI_TARS_API_KEY": "VISION-SECRET",
            },
            clear=True,
        ):
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
