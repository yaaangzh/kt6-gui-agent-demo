from __future__ import annotations

import json
import unittest

from kt6_backend.topology_model_contract import (
    MODEL_SCHEMA_VERSION,
    TopologyModelContract,
    TopologyModelResponseError,
)


class TopologyModelContractTest(unittest.TestCase):
    def test_compact_cv_context_removes_geometry_and_pixel_attributes(self):
        context = TopologyModelContract.compact_cv_context(
            {
                "objects": [
                    {
                        "business_id": "GW-001",
                        "type": "gateway",
                        "label": "GW-001",
                        "center": [120.5, 80.5],
                        "bbox": [100, 60, 41, 41],
                        "confidence": 0.92,
                        "attributes": {
                            "ocr_polygon": [[1, 2], [3, 4]],
                            "pixel_path": [[5, 6], [7, 8]],
                        },
                    }
                ],
                "links": [
                    {
                        "source": "GW-001",
                        "target": "SW-001",
                        "type": "topology_link",
                        "confidence": 0.73,
                        "attributes": {"pixel_path": [[1, 1], [2, 2]]},
                    }
                ],
            }
        )

        self.assertEqual(
            context["objects"][0],
            {
                "business_id": "GW-001",
                "type": "gateway",
                "label": "GW-001",
                "center": [120.5, 80.5],
                "confidence": 0.92,
            },
        )
        self.assertNotIn("attributes", context["links"][0])

    def test_prompt_requests_semantics_without_full_pixel_schema(self):
        prompt = TopologyModelContract.prompt(
            [
                {
                    "canvas_id": "uploaded_topology",
                    "local_path": "C:\\staged\\frame.png",
                    "screenshot_sha256": "ignored",
                    "mime_type": "image/png",
                    "width": 1920,
                    "height": 1080,
                }
            ],
            cv_observations={"source": "local_cv_candidates", "objects": [], "links": []},
        )
        _heading, request_text = prompt.split("\n", 1)
        request = json.loads(request_text)

        self.assertEqual(request["operation"], "topology_semantic_enrichment")
        self.assertNotIn("output_schema", request)
        self.assertNotIn("screenshot_sha256", request["frames"][0])
        self.assertNotIn("mime_type", request["frames"][0])
        self.assertNotIn("bbox", json.dumps(request["output_shape"]))
        self.assertEqual(
            request["output_shape"]["schema_version"],
            MODEL_SCHEMA_VERSION,
        )

    def test_accepts_compact_semantic_response(self):
        payload = {
            "schema_version": MODEL_SCHEMA_VERSION,
            "confidence": 0.91,
            "nodes": [
                {
                    "id": "GW-001",
                    "type": "gateway",
                    "role": "edge_gateway",
                    "vendor": "ZTE",
                    "layer": "接入层",
                    "confidence": 0.95,
                }
            ],
            "links": [],
            "structure_templates": [
                {
                    "template_id": "layered-1",
                    "type": "layered",
                    "layers": [{"name": "接入层", "members": ["GW-001"]}],
                }
            ],
            "negative_edges": [],
            "no_connections": True,
        }

        result = TopologyModelContract.parse_response_bytes(
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
        )

        self.assertEqual(result, payload)

    def test_accepts_one_json_code_fence_without_surrounding_prose(self):
        payload = {
            "schema_version": MODEL_SCHEMA_VERSION,
            "nodes": [{"id": "GW-001", "type": "gateway"}],
            "links": [],
        }
        fenced = (
            "```json\n"
            + json.dumps(payload, ensure_ascii=False, indent=2)
            + "\n```\n"
        )

        result = TopologyModelContract.parse_response_bytes(
            fenced.encode("utf-8")
        )

        self.assertEqual(result, payload)

    def test_accepts_leading_json_with_trailing_commentary(self):
        payload = {
            "schema_version": MODEL_SCHEMA_VERSION,
            "nodes": [{"id": "GW-001", "type": "gateway"}],
            "links": [],
        }
        encoded = json.dumps(payload, ensure_ascii=False, indent=2)
        for suffix in (
            "\n```\n- Additional model analysis",
            "\n- Additional model analysis without a closing fence",
        ):
            with self.subTest(suffix=suffix):
                result = TopologyModelContract.parse_response_bytes(
                    ("```json\n" + encoded + suffix).encode("utf-8")
                )
                self.assertEqual(result, payload)

    def test_rejects_json_fence_with_leading_commentary(self):
        payload = {
            "schema_version": MODEL_SCHEMA_VERSION,
            "nodes": [],
            "links": [],
        }
        fenced = (
            "Here is the result:\n```json\n"
            + json.dumps(payload)
            + "\n```"
        )

        with self.assertRaises(TopologyModelResponseError):
            TopologyModelContract.parse_response_bytes(
                fenced.encode("utf-8")
            )

    def test_rejects_additional_fenced_block_after_json(self):
        payload = {
            "schema_version": MODEL_SCHEMA_VERSION,
            "nodes": [],
            "links": [],
        }
        response = (
            "```json\n"
            + json.dumps(payload)
            + "\n```\n```text\nsecond block\n```"
        )

        with self.assertRaises(TopologyModelResponseError):
            TopologyModelContract.parse_response_bytes(
                response.encode("utf-8")
            )

    def test_rejects_reintroduced_model_geometry(self):
        payload = {
            "schema_version": MODEL_SCHEMA_VERSION,
            "nodes": [
                {
                    "id": "GW-001",
                    "type": "gateway",
                    "bbox": [0, 0, 10, 10],
                }
            ],
            "links": [],
        }

        with self.assertRaisesRegex(
            TopologyModelResponseError, "unsupported field bbox"
        ):
            TopologyModelContract.parse_response_bytes(
                json.dumps(payload).encode("utf-8")
            )


if __name__ == "__main__":
    unittest.main()
