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
    CodeAgentProcessResult,
)
from kt6_backend.topology_cv_cli import generate_cv_artifact
from kt6_backend.topology_hybrid_cli import run_pipeline
from kt6_backend.topology_model_cli import generate_model_artifact
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
        "schema_version": RESPONSE_SCHEMA_VERSION,
        "confidence": 0.97,
        "objects": [
            {
                "business_id": "GW-001",
                "type": "gateway",
                "label": "GW-001",
                "canvas_id": "uploaded_topology",
                "bbox": [0, 0, 1, 1],
                "confidence": 0.97,
                "attributes": {"vendor": "ZTE"},
            }
        ],
        "links": [],
        "co_channel_relations": [],
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
        return CodeAgentProcessResult(returncode=0, stdout=stdout, stderr=b"")


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
        runner = SuccessfulModelRunner()

        result = generate_model_artifact(
            self.image_path,
            source_id="hybrid-v1",
            output_path=output_path,
            events_path=events_path,
            cv_path=cv_path,
            executable=sys.executable,
            timeout_seconds=600,
            workdir=self.root,
            runner=runner,
        )

        self.assertEqual(result["objects"][0]["attributes"]["vendor"], "ZTE")
        self.assertEqual(
            json.loads(output_path.read_text(encoding="utf-8")),
            result,
        )
        event_lines = events_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(event_lines), 3)
        prompt = runner.call["stdin"].decode("utf-8")
        _heading, request_text = prompt.split("\n", 1)
        request = json.loads(request_text)
        self.assertEqual(
            request["cv_observations"]["objects"][0]["business_id"],
            "GW-001",
        )
        self.assertEqual(runner.call["timeout_seconds"], 600.0)

    def test_pipeline_keeps_all_three_artifacts(self):
        output_dir = self.root / "artifacts"

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
            **_kwargs,
        ):
            output_path.write_text(json.dumps(_model_result()), encoding="utf-8")
            events_path.write_text('{"type":"result"}\n', encoding="utf-8")
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

        self.assertEqual(set(paths), {"cv", "model", "events", "fused"})
        for path in paths.values():
            self.assertTrue(path.is_file(), path)
        fused = json.loads(paths["fused"].read_text(encoding="utf-8"))
        self.assertEqual(fused["summary"]["confirmed_object_count"], 1)

    def test_standalone_adapter_allows_longer_timeout_than_http_path(self):
        adapter = CodeAgentCanvasVisionAdapter(
            workdir=self.root,
            executable=sys.executable,
            timeout_seconds=600,
            runner=SuccessfulModelRunner(),
        )
        self.assertEqual(adapter.timeout_seconds, 600.0)


if __name__ == "__main__":
    unittest.main()
