from __future__ import annotations

import base64
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from kt6_backend.codeagent_canvas_vision import (
    CodeAgentCanvasVisionAdapter,
    CodeAgentVisionError,
    CodeAgentProcessResult,
)
from kt6_backend.topology_cv_cli import generate_cv_artifact
from kt6_backend.topology_hybrid_cli import (
    build_parser as build_hybrid_parser,
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


def _scatter_cv_result() -> dict:
    objects = [
        {
            "business_id": f"AP-{index:03d}",
            "type": "access_point",
            "label": f"AP-{index:03d}",
            "canvas_id": "uploaded_topology",
            "bbox": [0, 0, 1, 1],
            "confidence": 0.93,
            "attributes": {
                "recognizer": "rapidocr",
                "source_region": "diagram",
            },
        }
        for index in range(1, 21)
    ]
    return {
        "schema_version": RESPONSE_SCHEMA_VERSION,
        "confidence": 0.91,
        "objects": objects,
        "links": [
            {
                "relation_id": "local-line:AP-001:AP-002",
                "source": "AP-001",
                "target": "AP-002",
                "type": "topology_link",
                "confidence": 0.8,
                "attributes": {
                    "evidence": "legacy_layered_pixel_component",
                    "direction": "undirected",
                    "directed": False,
                },
            }
        ],
        "co_channel_relations": [],
        "diagnostics": {
            "producer": "local_cv_ocr",
            "connector_scan": {
                "status": "complete",
                "pixel_count": 80,
                "component_count": 1,
                "line_segment_count": 1,
                "budget_exhausted": False,
            },
        },
    }


def _empty_nce_anchor_cv_result() -> dict:
    return {
        "confidence": 0.0,
        "objects": [],
        "links": [],
        "co_channel_relations": [],
        "no_connections": False,
        "diagnostics": {
            "producer": "local_cv_ocr",
            "connector_scan": {
                "status": "not_applicable",
                "pixel_count": 0,
                "component_count": 0,
                "line_segment_count": 0,
                "budget_exhausted": False,
            },
            "ocr_text_anchors": {
                "schema_version": "kt6.local-ocr-text-anchors.v1",
                "truncated": False,
                "items": [
                    {
                        "text": "CSG1",
                        "canvas_id": "uploaded_topology",
                        "bbox": [0.0, 0.0, 0.4, 0.4],
                        "confidence": 0.97,
                    },
                    {
                        "text": "PTN7900E-12-01",
                        "canvas_id": "uploaded_topology",
                        "bbox": [0.5, 0.0, 0.4, 0.4],
                        "confidence": 0.96,
                    },
                ],
            },
        },
    }


def _nce_model_result() -> dict:
    result = _linked_model_result()
    result["nodes"][0]["id"] = "CSG1"
    result["nodes"][1]["id"] = "PTN7900E-12-01"
    result["links"][0]["source"] = "CSG1"
    result["links"][0]["target"] = "PTN7900E-12-01"
    return result


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


def _linked_model_result() -> dict:
    return {
        "schema_version": MODEL_SCHEMA_VERSION,
        "confidence": 0.94,
        "nodes": [
            {
                "id": "AP-001",
                "type": "access_point",
                "label": "AP-001",
                "role": "leaf",
                "confidence": 0.94,
            },
            {
                "id": "AP-002",
                "type": "access_point",
                "label": "AP-002",
                "role": "leaf",
                "confidence": 0.94,
            },
        ],
        "links": [
            {
                "source": "AP-001",
                "target": "AP-002",
                "type": "topology_link",
                "confidence": 0.94,
                "attributes": {
                    "direction": "undirected",
                    "directed": False,
                },
            }
        ],
        "structure_templates": [],
        "negative_edges": [],
        "no_connections": False,
    }


class FakeCVAdapter:
    adapter_id = "local-cv-ocr"
    adapter_version = "1.5"

    def __init__(self, result=None):
        self.result = _cv_result() if result is None else result

    def recognize(self, *, page, frames):
        self.page = page
        self.frames = frames
        return self.result


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
        self.assertEqual(hybrid_args.requested_profile, "auto")

    def test_pipeline_keeps_all_artifacts(self):
        output_dir = self.root / "artifacts"

        def fake_cv(
            image_path, *, source_id, output_path, metadata_output_path
        ):
            return generate_cv_artifact(
                image_path,
                source_id=source_id,
                output_path=output_path,
                metadata_output_path=metadata_output_path,
                adapter=FakeCVAdapter(),
            )

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
            {
                "cv",
                "cv_metadata",
                "routing",
                "model",
                "events",
                "stderr",
                "fused",
            },
        )
        for path in paths.values():
            self.assertIsNotNone(path)
            self.assertTrue(path.is_file(), path)
        fused = json.loads(paths["fused"].read_text(encoding="utf-8"))
        self.assertEqual(fused["summary"]["confirmed_object_count"], 1)
        self.assertEqual(fused["routing"]["decision"], "model_assist")
        self.assertEqual(fused["routing"]["result_status"], "insufficient")
        self.assertEqual(fused["routing"]["execution_status"], "model_completed")
        self.assertFalse(fused["routing"]["requirement_satisfied"])

    def test_empty_cv_uses_model_and_grounds_nce_nodes_from_ocr_anchors(self):
        output_dir = self.root / "nce-empty-cv"

        def fake_cv(
            image_path, *, source_id, output_path, metadata_output_path
        ):
            return generate_cv_artifact(
                image_path,
                source_id=source_id,
                output_path=output_path,
                metadata_output_path=metadata_output_path,
                adapter=FakeCVAdapter(_empty_nce_anchor_cv_result()),
            )

        def fake_model(
            image_path,
            *,
            output_path,
            events_path,
            stderr_path,
            **_kwargs,
        ):
            result = _nce_model_result()
            output_path.write_text(
                json.dumps(result),
                encoding="utf-8",
            )
            events_path.write_text(
                '{"type":"result"}\n',
                encoding="utf-8",
            )
            stderr_path.write_text("", encoding="utf-8")
            return result

        with patch(
            "kt6_backend.topology_hybrid_cli.generate_cv_artifact",
            side_effect=fake_cv,
        ), patch(
            "kt6_backend.topology_hybrid_cli.generate_model_artifact",
            side_effect=fake_model,
        ):
            paths = run_pipeline(
                self.image_path,
                source_id="nce-empty-cv",
                output_dir=output_dir,
            )

        routing = json.loads(
            paths["routing"].read_text(encoding="utf-8")
        )
        fused = json.loads(paths["fused"].read_text(encoding="utf-8"))
        self.assertEqual(routing["decision"], "model_assist")
        self.assertEqual(routing["execution_status"], "model_completed")
        self.assertTrue(routing["requirement_satisfied"])
        self.assertEqual(
            fused["summary"]["ocr_anchor_grounded_object_count"],
            2,
        )
        self.assertEqual(fused["summary"]["grounded_object_count"], 2)
        self.assertEqual(fused["summary"]["grounded_link_count"], 1)
        self.assertEqual(len(fused["result"]["objects"]), 2)
        self.assertEqual(fused["unlocated_objects"], [])
        self.assertIn(
            "object_identity",
            routing["satisfied_capabilities"],
        )
        self.assertIn(
            "analysis_only_image_geometry",
            routing["satisfied_capabilities"],
        )
        self.assertIn(
            "verified_connectivity",
            routing["satisfied_capabilities"],
        )
        self.assertEqual(
            routing["missing_capabilities"],
            ["actionable_canvas_binding"],
        )

    def test_model_failure_is_persisted_in_routing_artifact(self):
        output_dir = self.root / "model-failed"

        def fake_cv(
            image_path, *, source_id, output_path, metadata_output_path
        ):
            return generate_cv_artifact(
                image_path,
                source_id=source_id,
                output_path=output_path,
                metadata_output_path=metadata_output_path,
                adapter=FakeCVAdapter(_scatter_cv_result()),
            )

        model_error = CodeAgentVisionError(
            "model unavailable",
            error_code="transport_failure",
            category="transport",
            retryable=True,
        )
        with patch(
            "kt6_backend.topology_hybrid_cli.generate_cv_artifact",
            side_effect=fake_cv,
        ), patch(
            "kt6_backend.topology_hybrid_cli.generate_model_artifact",
            side_effect=model_error,
        ):
            with self.assertRaises(CodeAgentVisionError):
                run_pipeline(
                    self.image_path,
                    source_id="model-failed",
                    output_dir=output_dir,
                    requested_profile="visible_topology",
                )

        routing = json.loads(
            (output_dir / "routing-result.json").read_text(encoding="utf-8")
        )
        self.assertEqual(routing["execution_status"], "model_failed")
        self.assertEqual(routing["result_status"], "failed")
        self.assertFalse(routing["requirement_satisfied"])
        self.assertEqual(routing["failure"]["stage"], "model")
        self.assertEqual(routing["failure"]["error_code"], "transport_failure")
        self.assertTrue(routing["failure"]["retryable"])

    def test_model_assist_finalizes_a_grounded_visible_topology(self):
        output_dir = self.root / "model-finalized"

        def fake_cv(
            image_path, *, source_id, output_path, metadata_output_path
        ):
            return generate_cv_artifact(
                image_path,
                source_id=source_id,
                output_path=output_path,
                metadata_output_path=metadata_output_path,
                adapter=FakeCVAdapter(_scatter_cv_result()),
            )

        def fake_model(
            image_path,
            *,
            output_path,
            events_path,
            stderr_path,
            **_kwargs,
        ):
            result = _linked_model_result()
            output_path.write_text(json.dumps(result), encoding="utf-8")
            events_path.write_text('{"type":"result"}\n', encoding="utf-8")
            stderr_path.write_text("", encoding="utf-8")
            return result

        with patch(
            "kt6_backend.topology_hybrid_cli.generate_cv_artifact",
            side_effect=fake_cv,
        ), patch(
            "kt6_backend.topology_hybrid_cli.generate_model_artifact",
            side_effect=fake_model,
        ):
            paths = run_pipeline(
                self.image_path,
                source_id="model-finalized",
                output_dir=output_dir,
                requested_profile="visible_topology",
            )

        routing = json.loads(paths["routing"].read_text(encoding="utf-8"))
        fused = json.loads(paths["fused"].read_text(encoding="utf-8"))
        self.assertTrue(routing["requirement_satisfied"])
        self.assertEqual(routing["result_status"], "complete")
        self.assertEqual(routing["execution_status"], "model_completed")
        self.assertGreater(
            routing["post_model_metrics"]["grounded_link_count"],
            0,
        )
        self.assertEqual(fused["routing"], routing)

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
        metadata_path = output_dir / "cv-metadata.json"
        generate_cv_artifact(
            self.image_path,
            source_id="pipeline-v1",
            output_path=cv_path,
            metadata_output_path=metadata_path,
            adapter=FakeCVAdapter(),
        )

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

    def test_auto_scatter_skips_model_and_keeps_weak_link_as_disputed(self):
        output_dir = self.root / "cv-only"
        output_dir.mkdir()
        generate_cv_artifact(
            self.image_path,
            source_id="scatter-v1",
            output_path=output_dir / "cv-result.json",
            metadata_output_path=output_dir / "cv-metadata.json",
            adapter=FakeCVAdapter(_scatter_cv_result()),
        )
        stale_paths = [
            output_dir / "model-result.json",
            output_dir / "codeagent-events.jsonl",
            output_dir / "codeagent-stderr.log",
            output_dir / "codeagent-events.attempt-1.jsonl",
            output_dir / "codeagent-stderr.attempt-1.log",
        ]
        for stale_path in stale_paths:
            stale_path.write_text("stale", encoding="utf-8")

        with patch(
            "kt6_backend.topology_hybrid_cli.generate_model_artifact"
        ) as model_generator:
            paths = run_pipeline(
                self.image_path,
                source_id="scatter-v1",
                output_dir=output_dir,
                reuse_cv=True,
            )

        model_generator.assert_not_called()
        self.assertIsNone(paths["model"])
        self.assertIsNone(paths["events"])
        self.assertIsNone(paths["stderr"])
        self.assertTrue(all(not path.exists() for path in stale_paths))
        routing = json.loads(paths["routing"].read_text(encoding="utf-8"))
        fused = json.loads(paths["fused"].read_text(encoding="utf-8"))
        self.assertEqual(routing["decision"], "cv_only")
        self.assertEqual(routing["scene_type"], "scatter_nodes")
        self.assertEqual(routing["requested_profile"], "auto")
        self.assertEqual(routing["effective_profile"], "nodes_only")
        self.assertEqual(fused["result"]["links"], [])
        self.assertEqual(len(fused["disputed_links"]), 1)
        self.assertFalse(
            fused["disputed_links"][0]["attributes"]["interaction_eligible"]
        )

    def test_reuse_cv_rejects_a_different_image_hash(self):
        output_dir = self.root / "hash-mismatch"
        output_dir.mkdir()
        generate_cv_artifact(
            self.image_path,
            source_id="same-source",
            output_path=output_dir / "cv-result.json",
            metadata_output_path=output_dir / "cv-metadata.json",
            adapter=FakeCVAdapter(),
        )
        old_fused = output_dir / "fused-result.json"
        old_model = output_dir / "model-result.json"
        old_fused.write_text("old fused", encoding="utf-8")
        old_model.write_text("old model", encoding="utf-8")
        self.image_path.write_bytes(ONE_PIXEL_PNG + b"changed")

        with self.assertRaisesRegex(
            ValueError,
            "does not match the current image",
        ):
            run_pipeline(
                self.image_path,
                source_id="same-source",
                output_dir=output_dir,
                reuse_cv=True,
            )

        self.assertEqual(old_fused.read_text(encoding="utf-8"), "old fused")
        self.assertEqual(old_model.read_text(encoding="utf-8"), "old model")

    def test_reuse_cv_rejects_a_tampered_cv_artifact(self):
        output_dir = self.root / "cv-hash-mismatch"
        output_dir.mkdir()
        cv_path = output_dir / "cv-result.json"
        generate_cv_artifact(
            self.image_path,
            source_id="same-source",
            output_path=cv_path,
            metadata_output_path=output_dir / "cv-metadata.json",
            adapter=FakeCVAdapter(),
        )
        cv_path.write_text(json.dumps(_scatter_cv_result()), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "payload hash"):
            run_pipeline(
                self.image_path,
                source_id="same-source",
                output_dir=output_dir,
                reuse_cv=True,
            )

    def test_pipeline_rejects_input_path_collision_without_deleting_image(self):
        output_dir = self.root / "collision"
        output_dir.mkdir()
        colliding_image = output_dir / "fused-result.json"
        colliding_image.write_bytes(ONE_PIXEL_PNG)

        with self.assertRaisesRegex(ValueError, "artifact paths must be distinct"):
            run_pipeline(
                colliding_image,
                source_id="collision",
                output_dir=output_dir,
            )

        self.assertEqual(colliding_image.read_bytes(), ONE_PIXEL_PNG)

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
