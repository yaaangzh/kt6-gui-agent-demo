from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from kt6_backend.deepseek_topology_model import DeepSeekTopologySemanticAdapter
from kt6_backend.openai_compatible_api import (
    ModelHTTPResponse,
    OpenAICompatibleChatClient,
)
from kt6_backend.vision_recognition import CanvasFrame


class StubTransport:
    def __init__(self, content):
        self.content = content
        self.calls = []

    def post(self, **kwargs):
        self.calls.append(kwargs)
        return ModelHTTPResponse(
            status=200,
            headers={"Content-Type": "application/json"},
            body=json.dumps(
                {
                    "id": "call-1",
                    "model": "deepseek-test",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(self.content),
                            },
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 20,
                        "completion_tokens": 5,
                        "total_tokens": 25,
                    },
                }
            ).encode(),
        )


class DeepSeekTopologySemanticAdapterTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        image = Path(self.temp.name) / "frame.png"
        image.write_bytes(b"not-sent-to-model")
        self.frame = CanvasFrame(
            canvas_id="canvas-1",
            screenshot_path=image,
            screenshot_sha256="a" * 64,
            mime_type="image/png",
            width=100,
            height=80,
            client_width=100,
            client_height=80,
            bbox=(0, 0, 100, 80),
        )

    def tearDown(self):
        self.temp.cleanup()

    def adapter(self, transport, calls=None):
        client = OpenAICompatibleChatClient(
            base_url="https://api.deepseek.test/v1",
            api_key="secret-key",
            model="deepseek-test",
            allowed_hosts=["api.deepseek.test"],
            transport=transport,
        )
        return DeepSeekTopologySemanticAdapter(
            client,
            call_sink=(calls.append if calls is not None else None),
        )

    def test_sends_only_bounded_cv_text_and_parses_strict_contract(self):
        transport = StubTransport(
            {
                "schema_version": "kt6.topology-model.v1",
                "confidence": 0.9,
                "nodes": [
                    {"id": "AP-1", "type": "access_point", "label": "AP-1"}
                ],
                "links": [],
            }
        )
        calls = []
        result = self.adapter(transport, calls).recognize_with_context(
            page={"url": "https://sensitive.example/path"},
            frames=(self.frame,),
            cv_observations={
                "objects": [
                    {
                        "business_id": "AP-1",
                        "type": "device",
                        "label": "AP-1",
                        "canvas_id": "canvas-1",
                        "center": [10, 20],
                        "confidence": 0.95,
                    }
                ],
                "links": [],
                "ocr_text_anchors": [],
            },
        )
        self.assertEqual(result["nodes"][0]["id"], "AP-1")
        request_body = transport.calls[0]["body"]
        self.assertNotIn(b"not-sent-to-model", request_body)
        self.assertNotIn(b"sensitive.example", request_body)
        self.assertNotIn(str(self.frame.screenshot_path).encode(), request_body)
        outer_request = json.loads(request_body)
        semantic_request = json.loads(outer_request["messages"][1]["content"])
        self.assertFalse(semantic_request["screenshot_sent_to_model"])
        self.assertEqual(calls[0].input_mode, "cv_text")
        self.assertFalse(calls[0].screenshot_sent_to_model)
        self.assertEqual(calls[0].usage["total_tokens"], 25)

    def test_refuses_raw_image_route_and_empty_cv_candidates(self):
        adapter = self.adapter(StubTransport({}))
        with self.assertRaisesRegex(ValueError, "requires trusted CV/OCR"):
            adapter.recognize(page={}, frames=(self.frame,))
        with self.assertRaisesRegex(ValueError, "requires CV/OCR candidates"):
            adapter.recognize_with_context(
                page={},
                frames=(self.frame,),
                cv_observations={"objects": [], "links": [], "ocr_text_anchors": []},
            )

    def test_rejects_model_output_outside_topology_contract(self):
        transport = StubTransport({"nodes": [], "links": [], "unexpected": True})
        adapter = self.adapter(transport)
        with self.assertRaises(ValueError):
            adapter.recognize_with_context(
                page={},
                frames=(self.frame,),
                cv_observations={
                    "objects": [
                        {
                            "business_id": "AP-1",
                            "label": "AP-1",
                            "confidence": 0.9,
                        }
                    ],
                    "links": [],
                    "ocr_text_anchors": [],
                },
            )


if __name__ == "__main__":
    unittest.main()
