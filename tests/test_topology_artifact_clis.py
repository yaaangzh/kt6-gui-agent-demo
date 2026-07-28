from __future__ import annotations

import base64
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from kt6_backend.codeagent_canvas_vision import (
    CodeAgentCanvasVisionAdapter,
    CodeAgentProcessResult,
    CodeAgentVisionIdleTimeoutError,
    CodeAgentVisionError,
    CodeAgentVisionResponseError,
    CodeAgentVisionTransportError,
)
from kt6_backend.topology_cv_cli import generate_cv_artifact
from kt6_backend.topology_fusion import TopologyFusionError
from kt6_backend.topology_hybrid_cli import (
    build_parser as build_hybrid_parser,
    main as hybrid_main,
    run_pipeline,
)
from kt6_backend.topology_model_cli import (
    DEFAULT_MODEL_IDLE_TIMEOUT_SECONDS,
    DEFAULT_MODEL_MAX_ATTEMPTS,
    build_parser as build_model_parser,
    generate_model_artifact,
)
from kt6_backend.topology_model_contract import MODEL_SCHEMA_VERSION
from kt6_backend.topology_vision_contract import RESPONSE_SCHEMA_VERSION


ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2ZQAAAABJRU5ErkJggg=="
)


def _cv_result() -> dict:
    return {
        "schema_version": RESPONSE_SCHEMA_VERSION,
        "confidence": 0.91,
        "objects": [
            {
                "business_id": "GW-001",
                "type": "gateway",
                "label": "GW-001",
                "canvas_id": "uploaded_topology",
                "bbox": [0, 0, 1, 1],
                "confidence": 0.91,
                "attributes": {},
            }
        ],
        "links": [],
        "co_channel_relations": [],
    }


def _model_result() -> dict:
    return {
        "schema_version": MODEL_SCHEMA_VERSION,
        "confidence": 0.97,
        "nodes": [
            {
                "id": "GW-001",
                "type": "gateway",
                "label": "GW-001",
                "role": "edge_gateway",
                "vendor": "ZTE",
                "confidence": 0.97,
            }
        ],
        "links": [],
        "structure_templates": [],
        "negative_edges": [],
        "no_connections": True,
    }


class FakeCVAdapter:
    def recognize(self, *, page, frames):
        self.page = page
        self.frames = frames
        return _cv_result()


class SuccessfulModelRunner:
    def __init__(self):
        self.call = None

    def run(self, **kwargs):
        self.call = kwargs
        prompt = kwargs["stdin"].decode("utf-8")
        _heading, request_text = prompt.split("\n", 1)
        request = json.loads(request_text)
        frame_path = request["frames"][0]["local_path"]
        response_text = json.dumps(
            _model_result(),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        events = [
            {
                "type": "tool_use",
                "part": {
                    "tool": "read",
                    "state": {
                        "status": "completed",
                        "input": {"filePath": frame_path},
                    },
                },
            },
            {"type": "text", "part": {"text": response_text}},
            {"type": "step_finish", "part": {}},
        ]
        stdout = (
            "\n".join(
                json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                for event in events
            )
            + "\n"
        ).encode("utf-8")
        return CodeAgentProcessResult(
            returncode=0,
            stdout=stdout,
            stderr=b"model diagnostic\n",
        )


class AssistantCandidateRunner:
    def __init__(self):
        self.calls: list[dict] = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        prompt = kwargs["stdin"].decode("utf-8")
        _heading, request_text = prompt.split("\n", 1)
        request = json.loads(request_text)
        frame_path = request["frames"][0]["local_path"]
        response_text = json.dumps(
            _model_result(),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return CodeAgentProcessResult(
            returncode=0,
            stdout=(
                "\n".join(
                    json.dumps(event, separators=(",", ":"))
                    for event in (
                        {
                            "type": "tool_use",
                            "part": {
                                "tool": "read",
                                "state": {
                                    "status": "completed",
                                    "input": {"filePath": frame_path},
                                },
                            },
                        },
                        {
                            "type": "assistant",
                            "message": {
                                "content": [
                                    {"type": "text", "text": response_text}
                                ]
                            },
                        },
                        {
                            "type": "result",
                            "subtype": "success",
                            "is_error": False,
                            "result": "analysis finished",
                        },
                    )
                )
                + "\n"
            ).encode("utf-8"),
            stderr=b"",
        )


class VisionDescriptionThenModelRunner:
    def __init__(self, *, include_final: bool = True):
        self.calls: list[dict] = []
        self.include_final = include_final

    def run(self, **kwargs):
        self.calls.append(kwargs)
        prompt = kwargs["stdin"].decode("utf-8")
        _heading, request_text = prompt.split("\n", 1)
        request = json.loads(request_text)
        frame_path = request["frames"][0]["local_path"]
        response_text = json.dumps(
            _model_result(),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        events = (
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "read-scatter",
                            "name": "Read",
                            "input": {"file_path": frame_path},
                        }
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "read-scatter",
                            "content": {
                                "type": "image/png",
                                "originalSize": 968508,
                            },
                            "vlDescription": (
                                "产品物理拓扑为无规律散点布局；可见 OSS、"
                                "CameraRoot 和多个 Subnet 节点，没有可见连接线。"
                            ),
                        }
                    ]
                },
            },
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": response_text}
                    ]
                },
            },
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": response_text,
            },
        )
        if not self.include_final:
            events = (
                *events[:2],
                {"type": "result", "subtype": "success", "is_error": False},
            )
        return CodeAgentProcessResult(
            returncode=0,
            stdout=(
                "\n".join(
                    json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                    for event in events
                )
                + "\n"
            ).encode("utf-8"),
            stderr=b"",
        )


class InvalidThenSuccessfulModelRunner:
    def __init__(self):
        self.calls: list[dict] = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        prompt = kwargs["stdin"].decode("utf-8")
        _heading, request_text = prompt.split("\n", 1)
        request = json.loads(request_text)
        frame_path = request["frames"][0]["local_path"]
        response_text = (
            "unable to produce protocol"
            if len(self.calls) == 1
            else json.dumps(_model_result(), separators=(",", ":"))
        )
        events = (
            {
                "type": "tool_use",
                "part": {
                    "tool": "read",
                    "state": {
                        "status": "completed",
                        "input": {"filePath": frame_path},
                    },
                },
            },
            {"type": "text", "part": {"text": response_text}},
            {"type": "step_finish", "part": {}},
        )
        return CodeAgentProcessResult(
            returncode=0,
            stdout=(
                "\n".join(
                    json.dumps(event, separators=(",", ":")) for event in events
                )
                + "\n"
            ).encode("utf-8"),
            stderr=(
                b"invalid first response\n"
                if len(self.calls) == 1
                else b"successful retry\n"
            ),
        )
class TopologyArtifactCLITest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.image_path = self.root / "topology.png"
        self.image_path.write_bytes(ONE_PIXEL_PNG)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_local_cv_artifact_is_written_as_utf8_json(self):
        output_path = self.root / "cv-result.json"
        adapter = FakeCVAdapter()

        result = generate_cv_artifact(
            self.image_path,
            source_id="中文拓扑",
            output_path=output_path,
            adapter=adapter,
        )

        self.assertEqual(result["objects"][0]["business_id"], "GW-001")
        self.assertEqual(
            json.loads(output_path.read_text(encoding="utf-8")),
            result,
        )
        self.assertEqual(adapter.frames[0].screenshot_path, self.image_path)
        self.assertIn("%E4%B8%AD%E6%96%87", adapter.page["url"])

    def test_model_artifact_records_events_and_receives_cv_candidates(self):
        cv_path = self.root / "cv-result.json"
        cv_path.write_text(
            json.dumps(_cv_result(), ensure_ascii=False),
            encoding="utf-8",
        )
        output_path = self.root / "model-result.json"
        events_path = self.root / "codeagent-events.jsonl"
        stderr_path = self.root / "codeagent-stderr.log"
        runner = SuccessfulModelRunner()

        result = generate_model_artifact(
            self.image_path,
            source_id="hybrid-v1",
            output_path=output_path,
            events_path=events_path,
            stderr_path=stderr_path,
            cv_path=cv_path,
            executable=sys.executable,
            permission_mode="bypassPermissions",
            timeout_seconds=600,
            workdir=self.root,
            runner=runner,
        )

        self.assertEqual(result["nodes"][0]["vendor"], "ZTE")
        self.assertEqual(
            json.loads(output_path.read_text(encoding="utf-8")),
            result,
        )
        event_lines = events_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(event_lines), 3)
        self.assertEqual(stderr_path.read_bytes(), b"model diagnostic\n")
        prompt = runner.call["stdin"].decode("utf-8")
        _heading, request_text = prompt.split("\n", 1)
        request = json.loads(request_text)
        self.assertEqual(
            request["cv_observations"]["objects"][0]["business_id"],
            "GW-001",
        )
        self.assertEqual(
            request["cv_observations"]["objects"][0]["canvas_id"],
            "uploaded_topology",
        )
        self.assertEqual(
            request["cv_observations"]["objects"][0]["center"],
            [0.5, 0.5],
        )
        self.assertNotIn(
            "bbox",
            request["cv_observations"]["objects"][0],
        )
        self.assertNotIn(
            "attributes",
            request["cv_observations"]["objects"][0],
        )
        self.assertNotIn("output_schema", request)
        self.assertEqual(
            request["output_shape"]["schema_version"],
            MODEL_SCHEMA_VERSION,
        )
        self.assertGreater(runner.call["timeout_seconds"], 0)
        self.assertLessEqual(runner.call["timeout_seconds"], 600.0)
        args = runner.call["args"]
        permission_index = args.index("--permission-mode")
        self.assertEqual(args[permission_index + 1], "bypassPermissions")
        self.assertNotIn("--add-dir", args)

    def test_model_recovers_valid_assistant_candidate_before_invalid_result(self):
        output_path = self.root / "model-result.json"
        events_path = self.root / "codeagent-events.jsonl"
        runner = AssistantCandidateRunner()

        result = generate_model_artifact(
            self.image_path,
            source_id="candidate-recovery",
            output_path=output_path,
            events_path=events_path,
            executable=sys.executable,
            timeout_seconds=30,
            max_attempts=1,
            workdir=self.root,
            runner=runner,
        )

        self.assertEqual(result, _model_result())
        self.assertEqual(len(runner.calls), 1)

    def test_model_accepts_read_vl_description_before_strict_json(self):
        output_path = self.root / "model-result.json"
        events_path = self.root / "codeagent-events.jsonl"
        runner = VisionDescriptionThenModelRunner()

        result = generate_model_artifact(
            self.image_path,
            source_id="scatter-vl-description",
            output_path=output_path,
            events_path=events_path,
            executable=sys.executable,
            timeout_seconds=30,
            max_attempts=1,
            workdir=self.root,
            runner=runner,
        )

        self.assertEqual(result, _model_result())
        self.assertEqual(len(runner.calls), 1)
        self.assertIn(
            "无规律散点布局",
            events_path.read_text(encoding="utf-8"),
        )

    def test_model_reports_missing_final_text_after_read_vl_description(self):
        output_path = self.root / "missing-final-model-result.json"
        events_path = self.root / "missing-final-events.jsonl"
        runner = VisionDescriptionThenModelRunner(include_final=False)

        with self.assertRaises(CodeAgentVisionResponseError) as raised:
            generate_model_artifact(
                self.image_path,
                source_id="scatter-vl-description-without-final-json",
                output_path=output_path,
                events_path=events_path,
                executable=sys.executable,
                timeout_seconds=30,
                max_attempts=1,
                workdir=self.root,
                runner=runner,
            )

        self.assertEqual(raised.exception.error_code, "missing_final_text")
        self.assertEqual(raised.exception.category, "model_response")
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(len(runner.calls), 1)
        event_payloads = [
            json.loads(line)
            for line in events_path.read_text(encoding="utf-8").splitlines()
        ]
        tool_result = event_payloads[1]["message"]["content"][0]
        self.assertEqual(tool_result["type"], "tool_result")
        self.assertIn("vlDescription", tool_result)

    def test_invalid_model_response_is_retried_and_first_events_are_archived(self):
        output_path = self.root / "model-result.json"
        events_path = self.root / "codeagent-events.jsonl"
        stderr_path = self.root / "codeagent-stderr.log"
        runner = InvalidThenSuccessfulModelRunner()

        result = generate_model_artifact(
            self.image_path,
            source_id="automatic-retry",
            output_path=output_path,
            events_path=events_path,
            stderr_path=stderr_path,
            executable=sys.executable,
            timeout_seconds=30,
            max_attempts=2,
            workdir=self.root,
            runner=runner,
        )

        self.assertEqual(result, _model_result())
        self.assertEqual(len(runner.calls), 2)
        self.assertLess(runner.calls[1]["timeout_seconds"], 30)
        archived_events = self.root / "codeagent-events.attempt-1.jsonl"
        archived_stderr = self.root / "codeagent-stderr.attempt-1.log"
        self.assertIn("unable to produce protocol", archived_events.read_text())
        self.assertEqual(
            archived_stderr.read_bytes(),
            b"invalid first response\n",
        )
        self.assertIn("successful retry", stderr_path.read_text())

    def test_model_cli_retry_defaults_and_explicit_disable(self):
        model_args = build_model_parser().parse_args(
            [
                str(self.image_path),
                "--source-id",
                "defaults",
                "--out",
                str(self.root / "model.json"),
                "--events",
                str(self.root / "events.jsonl"),
            ]
        )
        hybrid_args = build_hybrid_parser().parse_args(
            [
                str(self.image_path),
                "--source-id",
                "defaults",
                "--out-dir",
                str(self.root / "out"),
                "--idle-timeout",
                "0",
                "--max-attempts",
                "1",
            ]
        )

        self.assertEqual(
            model_args.idle_timeout,
            DEFAULT_MODEL_IDLE_TIMEOUT_SECONDS,
        )
        self.assertEqual(model_args.max_attempts, DEFAULT_MODEL_MAX_ATTEMPTS)
        self.assertEqual(hybrid_args.idle_timeout, 0)
        self.assertEqual(hybrid_args.max_attempts, 1)

    def test_pipeline_keeps_all_artifacts(self):
        output_dir = self.root / "artifacts"
        output_dir.mkdir()
        stale_model_error = output_dir / "model-error.json"
        stale_model_error.write_text('{"stale":true}', encoding="utf-8")

        def fake_cv(image_path, *, source_id, output_path):
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(_cv_result()), encoding="utf-8")
            return _cv_result()

        def fake_model(
            image_path,
            *,
            source_id,
            output_path,
            events_path,
            stderr_path,
            **_kwargs,
        ):
            output_path.write_text(json.dumps(_model_result()), encoding="utf-8")
            events_path.write_text('{"type":"result"}\n', encoding="utf-8")
            stderr_path.write_text("", encoding="utf-8")
            return _model_result()

        with patch(
            "kt6_backend.topology_hybrid_cli.generate_cv_artifact",
            side_effect=fake_cv,
        ), patch(
            "kt6_backend.topology_hybrid_cli.generate_model_artifact",
            side_effect=fake_model,
        ):
            paths = run_pipeline(
                self.image_path,
                source_id="pipeline-v1",
                output_dir=output_dir,
                executable=sys.executable,
                workdir=self.root,
            )

        self.assertEqual(
            set(paths),
            {"cv", "model", "events", "stderr", "fused"},
        )
        for path in paths.values():
            self.assertTrue(path.is_file(), path)
        fused = json.loads(paths["fused"].read_text(encoding="utf-8"))
        self.assertEqual(fused["summary"]["confirmed_object_count"], 1)
        self.assertFalse(stale_model_error.exists())

    def test_pipeline_degrades_to_local_cv_when_model_stage_fails(self):
        output_dir = self.root / "degraded"

        def fake_cv(image_path, *, source_id, output_path):
            cv_result = _cv_result()
            cv_result["objects"].append(
                {
                    "business_id": "SW-001",
                    "type": "switch",
                    "label": "SW-001",
                    "canvas_id": "uploaded_topology",
                    "bbox": [2, 0, 1, 1],
                    "confidence": 0.82,
                    "attributes": {},
                }
            )
            cv_result["links"].append(
                {
                    "source": "GW-001",
                    "target": "SW-001",
                    "type": "topology_link",
                    "confidence": 0.61,
                    "attributes": {"evidence": "local_line_path"},
                }
            )
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(cv_result), encoding="utf-8")
            return cv_result

        def failed_model(
            image_path,
            *,
            events_path,
            stderr_path,
            **_kwargs,
        ):
            events_path.write_text(
                '{"type":"result","subtype":"success"}\n',
                encoding="utf-8",
            )
            stderr_path.write_text("", encoding="utf-8")
            raise CodeAgentVisionResponseError(
                "codeagent returned no final JSON text after reading the Canvas frames",
                error_code="missing_final_text",
                category="model_response",
                retryable=True,
            )

        with patch(
            "kt6_backend.topology_hybrid_cli.generate_cv_artifact",
            side_effect=fake_cv,
        ), patch(
            "kt6_backend.topology_hybrid_cli.generate_model_artifact",
            side_effect=failed_model,
        ):
            paths = run_pipeline(
                self.image_path,
                source_id="scatter-local-fallback",
                output_dir=output_dir,
                executable=sys.executable,
                workdir=self.root,
            )

        self.assertNotIn("model", paths)
        self.assertIn("model_error", paths)
        for name in ("cv", "events", "stderr", "fused", "model_error"):
            self.assertTrue(paths[name].is_file(), paths[name])

        fused = json.loads(paths["fused"].read_text(encoding="utf-8"))
        self.assertEqual(
            [item["business_id"] for item in fused["result"]["objects"]],
            ["GW-001", "SW-001"],
        )
        self.assertEqual(len(fused["result"]["links"]), 1)
        self.assertEqual(
            fused["result"]["links"][0]["attributes"]["relation_state"],
            "disputed",
        )
        self.assertFalse(
            fused["result"]["links"][0]["attributes"]["interaction_eligible"]
        )
        self.assertEqual(len(fused["disputed_links"]), 1)
        self.assertEqual(fused["summary"]["accepted_link_count"], 0)
        self.assertEqual(fused["summary"]["disputed_link_count"], 1)
        self.assertEqual(fused["summary"]["degraded_to"], "local_cv")
        self.assertEqual(
            fused["summary"]["model_error_code"],
            "missing_final_text",
        )
        model_error = json.loads(
            paths["model_error"].read_text(encoding="utf-8")
        )
        self.assertEqual(model_error["category"], "model_response")
        self.assertTrue(model_error["retryable"])

        stdout = io.StringIO()
        with patch(
            "kt6_backend.topology_hybrid_cli.run_pipeline",
            return_value=paths,
        ), redirect_stdout(stdout):
            exit_code = hybrid_main(
                [
                    str(self.image_path),
                    "--source-id",
                    "scatter-local-fallback",
                    "--out-dir",
                    str(output_dir),
                ]
            )
        cli_payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(cli_payload["status"], "degraded")
        self.assertEqual(cli_payload["degraded_to"], "local_cv")
        self.assertIsNone(cli_payload["model"])

    def test_pipeline_uses_none_for_missing_degraded_diagnostics(self):
        output_dir = self.root / "degraded-missing-diagnostics"

        def fake_cv(image_path, *, source_id, output_path):
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(_cv_result()), encoding="utf-8")
            return _cv_result()

        def failed_model(*_args, **_kwargs):
            raise CodeAgentVisionResponseError(
                "codeagent returned no final JSON text after reading the Canvas frames",
                error_code="missing_final_text",
                category="model_response",
                retryable=True,
            )

        with patch(
            "kt6_backend.topology_hybrid_cli.generate_cv_artifact",
            side_effect=fake_cv,
        ), patch(
            "kt6_backend.topology_hybrid_cli.generate_model_artifact",
            side_effect=failed_model,
        ):
            paths = run_pipeline(
                self.image_path,
                source_id="degraded-missing-diagnostics",
                output_dir=output_dir,
                executable=sys.executable,
                workdir=self.root,
            )

        self.assertIsNone(paths["events"])
        self.assertIsNone(paths["stderr"])
        self.assertTrue(paths["fused"].is_file())
        self.assertTrue(paths["model_error"].is_file())

        stdout = io.StringIO()
        with patch(
            "kt6_backend.topology_hybrid_cli.run_pipeline",
            return_value=paths,
        ), redirect_stdout(stdout):
            exit_code = hybrid_main(
                [
                    str(self.image_path),
                    "--source-id",
                    "degraded-missing-diagnostics",
                    "--out-dir",
                    str(output_dir),
                ]
            )
        cli_payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(cli_payload["status"], "degraded")
        self.assertEqual(cli_payload["degraded_to"], "local_cv")
        self.assertIsNone(cli_payload["events"])
        self.assertIsNone(cli_payload["stderr"])
        self.assertIsNone(cli_payload["model"])

    def test_pipeline_refuses_degraded_success_for_empty_cv(self):
        output_dir = self.root / "degraded-empty-cv"

        def fake_cv(image_path, *, source_id, output_path):
            cv_result = _cv_result()
            cv_result["objects"] = []
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(cv_result), encoding="utf-8")
            return cv_result

        def failed_model(*_args, **_kwargs):
            raise CodeAgentVisionResponseError(
                "codeagent returned no final JSON text after reading the Canvas frames",
                error_code="missing_final_text",
                category="model_response",
                retryable=True,
            )

        with patch(
            "kt6_backend.topology_hybrid_cli.generate_cv_artifact",
            side_effect=fake_cv,
        ), patch(
            "kt6_backend.topology_hybrid_cli.generate_model_artifact",
            side_effect=failed_model,
        ):
            with self.assertRaisesRegex(
                TopologyFusionError,
                "without grounded topology objects",
            ):
                run_pipeline(
                    self.image_path,
                    source_id="degraded-empty-cv",
                    output_dir=output_dir,
                    executable=sys.executable,
                    workdir=self.root,
                )

        self.assertTrue((output_dir / "model-error.json").is_file())
        self.assertFalse((output_dir / "fused-result.json").exists())

    def test_pipeline_does_not_degrade_transport_or_integrity_errors(self):
        failures = (
            (
                "missing-read-proof",
                CodeAgentVisionResponseError(
                    "codeagent did not prove a completed image read",
                    error_code="missing_read_proof",
                    category="model_response",
                    retryable=True,
                ),
            ),
            (
                "post-read-idle-timeout",
                CodeAgentVisionIdleTimeoutError(
                    "codeagent stopped after reading the image",
                    error_code="post_read_idle_timeout",
                    category="transient_transport",
                    retryable=True,
                ),
            ),
            (
                "retry-deadline-exhausted",
                CodeAgentVisionError(
                    "CodeAgent model attempts exhausted the total timeout",
                    error_code="model_retry_deadline_exhausted",
                    category="transient_transport",
                    retryable=True,
                ),
            ),
            (
                "transport-timeout",
                CodeAgentVisionTransportError(
                    "codeagent perception timed out",
                    error_code="transport_timeout",
                    category="transient_transport",
                    retryable=True,
                ),
            ),
            (
                "nonretryable-response",
                CodeAgentVisionResponseError(
                    "nonretryable invalid response",
                    error_code="invalid_model_response",
                    category="model_response",
                    retryable=False,
                ),
            ),
        )

        def fake_cv(image_path, *, source_id, output_path):
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(_cv_result()), encoding="utf-8")
            return _cv_result()

        for name, failure in failures:
            with self.subTest(name=name):
                output_dir = self.root / f"unsafe-degrade-{name}"
                with patch(
                    "kt6_backend.topology_hybrid_cli.generate_cv_artifact",
                    side_effect=fake_cv,
                ), patch(
                    "kt6_backend.topology_hybrid_cli.generate_model_artifact",
                    side_effect=failure,
                ):
                    with self.assertRaises(type(failure)) as raised:
                        run_pipeline(
                            self.image_path,
                            source_id=name,
                            output_dir=output_dir,
                            executable=sys.executable,
                            workdir=self.root,
                        )

                self.assertEqual(raised.exception.error_code, failure.error_code)
                self.assertFalse((output_dir / "model-error.json").exists())
                self.assertFalse((output_dir / "fused-result.json").exists())

    def test_standalone_adapter_allows_longer_timeout_than_http_path(self):
        adapter = CodeAgentCanvasVisionAdapter(
            workdir=self.root,
            executable=sys.executable,
            timeout_seconds=600,
            runner=SuccessfulModelRunner(),
        )
        self.assertEqual(adapter.timeout_seconds, 600.0)

    def test_pipeline_can_reuse_cv_artifact_for_model_retry(self):
        output_dir = self.root / "retry"
        output_dir.mkdir()
        cv_path = output_dir / "cv-result.json"
        cv_path.write_text(json.dumps(_cv_result()), encoding="utf-8")

        def fake_model(
            image_path,
            *,
            output_path,
            events_path,
            stderr_path,
            **_kwargs,
        ):
            output_path.write_text(json.dumps(_model_result()), encoding="utf-8")
            events_path.write_text('{"type":"result"}\n', encoding="utf-8")
            stderr_path.write_text("", encoding="utf-8")
            return _model_result()

        with patch(
            "kt6_backend.topology_hybrid_cli.generate_cv_artifact"
        ) as cv_generator, patch(
            "kt6_backend.topology_hybrid_cli.generate_model_artifact",
            side_effect=fake_model,
        ):
            paths = run_pipeline(
                self.image_path,
                source_id="pipeline-v1",
                output_dir=output_dir,
                executable=sys.executable,
                workdir=self.root,
                reuse_cv=True,
            )

        cv_generator.assert_not_called()
        self.assertEqual(
            json.loads(paths["cv"].read_text(encoding="utf-8")),
            _cv_result(),
        )
        self.assertTrue(paths["fused"].is_file())

    def test_pipeline_rejects_reuse_when_cv_artifact_is_missing(self):
        with self.assertRaisesRegex(ValueError, "missing CV artifact"):
            run_pipeline(
                self.image_path,
                source_id="pipeline-v1",
                output_dir=self.root / "missing-retry",
                executable=sys.executable,
                workdir=self.root,
                reuse_cv=True,
            )


if __name__ == "__main__":
    unittest.main()
