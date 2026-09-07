from __future__ import annotations

import base64
import json
from pathlib import Path
import tempfile
import unittest

from kt6_backend.topology_cv_cli import generate_cv_artifact
from kt6_backend.topology_vision_contract import RESPONSE_SCHEMA_VERSION


ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2ZQAAAABJRU5ErkJggg=="
)


class FakeCVAdapter:
    adapter_id = "local-cv-ocr"
    adapter_version = "1.5"

    def recognize(self, *, page, frames):
        self.page = page
        self.frames = frames
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


class TopologyCVCLITest(unittest.TestCase):
    def test_local_cv_artifact_is_written_as_utf8_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            image_path = root / "topology.png"
            output_path = root / "cv-result.json"
            image_path.write_bytes(ONE_PIXEL_PNG)
            adapter = FakeCVAdapter()

            result = generate_cv_artifact(
                image_path,
                source_id="中文拓扑",
                output_path=output_path,
                adapter=adapter,
            )

            self.assertEqual(result["objects"][0]["business_id"], "GW-001")
            self.assertEqual(
                json.loads(output_path.read_text(encoding="utf-8")),
                result,
            )
            self.assertEqual(adapter.frames[0].screenshot_path, image_path)
            self.assertIn("%E4%B8%AD%E6%96%87", adapter.page["url"])


if __name__ == "__main__":
    unittest.main()
