from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from kt6_backend.nce_benchmark_adapter import NCEBenchmarkAdapter


ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2ZQAAAABJRU5ErkJggg=="
)


class NCEBenchmarkAdapterTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        run_dir = self.root / "runs" / "gui_20260710_231011"
        screenshots = run_dir / "screenshots"
        screenshots.mkdir(parents=True)
        (run_dir / "collector_config.json").write_text(
            json.dumps(
                {"intent": "Open Network Digital Map and inspect topology"}
            ),
            encoding="utf-8",
        )
        (run_dir / "episode_0.json").write_text(
            json.dumps(
                [
                    {
                        "type": "state",
                        "data": {
                            "info": {
                                "page": {
                                    "url": "https://nce.local/network-map"
                                }
                            }
                        },
                    }
                ]
            ),
            encoding="utf-8",
        )
        (run_dir / "validation_result.json").write_text(
            json.dumps({"passed": True}),
            encoding="utf-8",
        )
        self.screenshot_path = screenshots / "turn_000_initial.png"
        self.screenshot_path.write_bytes(ONE_PIXEL_PNG)
        (self.root / "benchmark_results.json").write_text(
            json.dumps(
                {
                    "results": [
                        {
                            "run_dir": str(run_dir),
                            "difficulty": "medium",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        self.adapter = NCEBenchmarkAdapter(self.root)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_reads_run_and_builds_verified_canvas_frame(self):
        runs = self.adapter.list_runs()

        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertEqual(run.category, "topology")
        self.assertTrue(run.passed)
        self.assertEqual(run.difficulty, "medium")
        self.assertEqual(len(run.screenshots), 1)
        self.assertTrue(run.screenshots[0].is_initial)
        frame = self.adapter.screenshot_to_canvas_frame(run.screenshots[0])
        self.assertEqual(frame.canvas_id, "nce_turn_000_initial")
        self.assertEqual((frame.width, frame.height), (1, 1))
        self.assertEqual(
            frame.screenshot_sha256,
            hashlib.sha256(ONE_PIXEL_PNG).hexdigest(),
        )
        self.assertEqual(
            self.adapter.page_url_for_turn(run, 0),
            "https://nce.local/network-map",
        )
        self.assertTrue(
            self.adapter.is_topology_page(
                self.adapter.page_url_for_turn(run, 0)
            )
        )

    def test_rejects_path_traversal_and_invalid_png(self):
        self.assertIsNone(self.adapter.get_run("../gui_20260710_231011"))
        run = self.adapter.list_runs()[0]
        self.screenshot_path.write_bytes(b"not a png")

        with self.assertRaisesRegex(ValueError, "valid PNG"):
            self.adapter.screenshot_to_canvas_frame(run.screenshots[0])


if __name__ == "__main__":
    unittest.main()
