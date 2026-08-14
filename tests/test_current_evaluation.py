from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from kt6_backend.current_evaluation import (
    CurrentEvaluationConfig,
    CurrentModelResult,
    run_current_evaluation,
)
from kt6_backend.current_evaluation_cli import main as current_cli_main
from kt6_backend.evaluation_artifacts import verify_run_evidence
from kt6_backend.evaluation_executor import ExecutionTask
from kt6_backend.evaluation_report import build_suite_template, load_run_records
from kt6_backend.local_cv_canvas_vision import LocalCVTopologyVisionAdapter
from kt6_backend.openai_compatible_api import (
    ModelAPIResponseError,
    ModelAPITransportError,
)
from kt6_backend.topology_model_contract import MODEL_SCHEMA_VERSION
from kt6_backend.topology_vision_contract import RESPONSE_SCHEMA_VERSION


_ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class _CVAdapter:
    adapter_id = LocalCVTopologyVisionAdapter.adapter_id
    adapter_version = LocalCVTopologyVisionAdapter.adapter_version

    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def recognize(self, *, page, frames):
        return self.payload


class _SemanticModel:
    def __init__(self, result: CurrentModelResult | Exception) -> None:
        self.result = result
        self.calls = 0

    def enrich(self, *, page, frames, cv_observations):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class CurrentEvaluationTest(unittest.TestCase):
    def setUp(self) -> None:
        scratch = Path.cwd() / ".test-tmp"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.root = Path(self.temp.name).resolve()
        self.image = self.root / "topology.png"
        self.image.write_bytes(_ONE_PIXEL_PNG)
        suite = build_suite_template(
            suite_id="current-image-suite",
            title="Current image evaluation",
            task_count=1,
            repetitions=4,
            planner_provider="test-provider",
            planner_model="test-model",
            environment_id="lab-a",
        )
        suite["status"] = "ready"
        suite["tasks"][0].update(
            {
                "title": "Read the topology image",
                "scenario_type": "canvas",
                "validation": {
                    "method": "api_assertion",
                    "description": "The fused topology contains GW-001",
                },
            }
        )
        self.suite = suite
        self.task = ExecutionTask(
            suite_id="current-image-suite",
            task_id="T01",
            instruction="Read the topology image and find GW-001.",
            start_url="http://127.0.0.1/topology",
            allowed_hosts=frozenset({"127.0.0.1"}),
            step_limit=10,
            validation={
                "method": "api_assertion",
                "assertions": [{"kind": "object_exists", "value": "GW-001"}],
            },
        )
        self.config = CurrentEvaluationConfig(
            base_url="http://127.0.0.1:9999/v1",
            api_key="test-key",
            provider="test-provider",
            model="test-model",
            api_allowed_hosts=frozenset(),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_model_assist_run_archives_verified_evidence(self) -> None:
        model = _SemanticModel(
            CurrentModelResult(self._model_payload(), input_tokens=30, output_tokens=12)
        )
        record = self._run(
            repetition=1,
            cv_payload=self._cv_payload(structured=False),
            semantic_model=model,
        )

        self.assertEqual(record["outcome"], "success")
        self.assertEqual(record["metrics"]["cv_calls"], 1)
        self.assertEqual(record["metrics"]["vision_model_calls"], 1)
        self.assertEqual(record["metrics"]["input_tokens"], 30)
        self.assertEqual(model.calls, 1)
        verification = verify_run_evidence(self.root, self.suite, record)
        self.assertTrue(verification["verified"], verification["errors"])
        archived = self.root / "artifacts" / "current" / "T01" / "r001"
        self.assertTrue((archived / "manifest.json").is_file())
        self.assertTrue((archived / "vision-model-call-001.jsonl").is_file())
        self.assertTrue((archived / "ui-graph-001.json").is_file())
        process_image = archived / "processed-screenshot-001.png"
        self.assertTrue(process_image.is_file())
        self.assertTrue(process_image.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))

    def test_cv_only_run_does_not_call_model(self) -> None:
        model = _SemanticModel(AssertionError("model must not be called"))
        record = self._run(
            repetition=2,
            cv_payload=self._cv_payload(structured=True),
            semantic_model=model,
        )

        self.assertEqual(record["outcome"], "success")
        self.assertEqual(record["metrics"]["vision_model_calls"], 0)
        self.assertEqual(model.calls, 0)
        verification = verify_run_evidence(self.root, self.suite, record)
        self.assertTrue(verification["verified"], verification["errors"])

    def test_invalid_model_response_is_recorded_as_failed_run(self) -> None:
        record = self._run(
            repetition=3,
            cv_payload=self._cv_payload(structured=False),
            semantic_model=_SemanticModel(ModelAPIResponseError("SECRET response")),
        )

        self.assertEqual(record["outcome"], "failure")
        self.assertEqual(record["failure"]["category"], "model_api_invalid_response")
        self.assertEqual(len(load_run_records(self.root / "runs.jsonl")), 1)
        verification = verify_run_evidence(self.root, self.suite, record)
        self.assertTrue(verification["verified"], verification["errors"])
        archived = self.root / "artifacts" / "current" / "T01" / "r003"
        self.assertFalse((archived / "model-result-001.json").exists())
        self.assertFalse((archived / "fused-result-001.json").exists())

    def test_model_timeout_is_counted_and_preserved_as_timeout(self) -> None:
        error = ModelAPITransportError("request timed out")
        error.__cause__ = TimeoutError()
        record = self._run(
            repetition=4,
            cv_payload=self._cv_payload(structured=False),
            semantic_model=_SemanticModel(error),
        )

        self.assertEqual(record["outcome"], "timeout")
        self.assertEqual(record["metrics"]["timeout_count"], 1)
        self.assertEqual(record["failure"]["category"], "vision_timeout")
        verification = verify_run_evidence(self.root, self.suite, record)
        self.assertTrue(verification["verified"], verification["errors"])

    def test_cli_reads_model_configuration_and_calls_executor(self) -> None:
        suite_path = self.root / "suite.json"
        suite_path.write_text(json.dumps(self.suite), encoding="utf-8")
        task_path = self.root / "task.json"
        task_path.write_text(
            json.dumps(
                {
                    "schema_version": "kt6.evaluation-execution-task.v1",
                    "suite_id": self.task.suite_id,
                    "task_id": self.task.task_id,
                    "instruction": self.task.instruction,
                    "start_url": self.task.start_url,
                    "allowed_hosts": list(self.task.allowed_hosts),
                    "step_limit": self.task.step_limit,
                    "validation": self.task.validation,
                }
            ),
            encoding="utf-8",
        )
        captured = {}

        def fake_run(**kwargs):
            captured.update(kwargs)
            process_dir = kwargs["workspace_root"] / "current-T01-r1"
            process_dir.mkdir(parents=True)
            (process_dir / "processed-screenshot-001.png").write_bytes(_ONE_PIXEL_PNG)
            return {
                "run_id": "current-T01-r1",
                "outcome": "success",
                "evidence": {"status": "verified"},
            }

        env = {
            "KT6_MODEL_API_BASE_URL": "http://127.0.0.1:9999/v1",
            "KT6_MODEL_API_KEY": "test-key",
            "KT6_MODEL_API_PROVIDER": "test-provider",
            "KT6_MODEL_API_MODEL": "test-model",
            "KT6_MODEL_API_ALLOWED_HOSTS": "",
            "KT6_VISION_TIMEOUT_SECONDS": "60",
            "KT6_MODEL_API_MAX_TOKENS": "4096",
        }
        output = StringIO()
        with patch.dict(os.environ, env, clear=False), patch(
            "kt6_backend.current_evaluation_cli.load_project_env"
        ), patch(
            "kt6_backend.current_evaluation_cli.run_current_evaluation",
            side_effect=fake_run,
        ), redirect_stdout(output):
            code = current_cli_main(
                [
                    "--suite",
                    str(suite_path),
                    "--task",
                    str(task_path),
                    "--image",
                    str(self.image),
                    "--runs",
                    str(self.root / "runs.jsonl"),
                    "--workspace",
                    str(self.root / "raw"),
                    "--repetition",
                    "1",
                    "--implementation-revision",
                    "abc1234",
                    "--environment-id",
                    "lab-a",
                ]
            )

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "recorded")
        self.assertEqual(
            Path(json.loads(output.getvalue())["process_image"]).name,
            "processed-screenshot-001.png",
        )
        self.assertEqual(captured["config"].model, "test-model")
        self.assertEqual(captured["source_id"], "topology")

    def _run(self, *, repetition, cv_payload, semantic_model):
        runs = self.root / "runs.jsonl"
        with patch(
            "kt6_backend.current_evaluation.render_topology_process_overview",
            return_value=_ONE_PIXEL_PNG,
        ):
            return run_current_evaluation(
                suite=self.suite,
                runs_path=runs,
                workspace_root=self.root / "raw",
                task=self.task,
                repetition=repetition,
                image_path=self.image,
                source_id=f"fixture-{repetition}",
                config=self.config,
                implementation={
                    "name": "KT6 current test",
                    "version": "1.0",
                    "revision": "abc1234",
                    "branch": "eval-current",
                },
                environment={
                    "environment_id": "lab-a",
                    "browser": "offline image",
                    "viewport": "derived-from-image",
                },
                local_adapter=_CVAdapter(cv_payload),
                semantic_model=semantic_model,
            )

    @staticmethod
    def _cv_payload(*, structured: bool) -> dict[str, object]:
        objects = [
            {
                "business_id": "GW-001",
                "type": "gateway",
                "label": "GW-001",
                "canvas_id": "uploaded_topology",
                "bbox": [0.0, 0.0, 0.4 if structured else 1.0, 1.0],
                "confidence": 0.95,
                "attributes": {
                    "recognizer": "rapidocr",
                    "source_region": "diagram",
                    "ocr_text": "GW-001",
                    "ocr_confidence": 0.95,
                },
            }
        ]
        links: list[dict[str, object]] = []
        if structured:
            objects.append(
                {
                    "business_id": "AP-001",
                    "type": "access_point",
                    "label": "AP-001",
                    "canvas_id": "uploaded_topology",
                    "bbox": [0.6, 0.0, 0.4, 1.0],
                    "confidence": 0.94,
                    "attributes": {
                        "recognizer": "rapidocr",
                        "source_region": "diagram",
                        "ocr_text": "AP-001",
                        "ocr_confidence": 0.94,
                    },
                }
            )
            links.append(
                {
                    "relation_id": "local-line:GW-001:AP-001",
                    "source": "GW-001",
                    "target": "AP-001",
                    "type": "topology_link",
                    "confidence": 0.90,
                    "attributes": {
                        "evidence": "connected_pixel_path",
                        "direction": "undirected",
                        "directed": False,
                    },
                }
            )
        return {
            "schema_version": RESPONSE_SCHEMA_VERSION,
            "confidence": 0.92,
            "objects": objects,
            "links": links,
            "co_channel_relations": [],
            "diagnostics": {
                "producer": "local_cv_ocr",
                "connector_scan": {
                    "status": "complete",
                    "pixel_count": 10 if structured else 0,
                    "component_count": 1 if structured else 0,
                    "line_segment_count": 1 if structured else 0,
                    "budget_exhausted": False,
                },
            },
        }

    @staticmethod
    def _model_payload() -> dict[str, object]:
        return {
            "schema_version": MODEL_SCHEMA_VERSION,
            "confidence": 0.91,
            "nodes": [
                {
                    "id": "GW-001",
                    "type": "gateway",
                    "role": "edge_gateway",
                    "confidence": 0.94,
                }
            ],
            "links": [],
            "structure_templates": [],
            "negative_edges": [],
            "no_connections": True,
        }


if __name__ == "__main__":
    unittest.main()
