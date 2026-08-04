from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from kt6_backend.vision_cache_coordinator import VisionCacheCoordinator
from kt6_backend.vision_frame_matcher import FrameMatchResult
from kt6_backend.vision_recognition import CanvasFrame
from kt6_backend.vision_result_cache import SQLiteVisionResultCacheStore


class RecordingAdapter:
    adapter_id = "recording"
    adapter_version = "1.0"
    supports_actionable_grounding = False

    def __init__(self) -> None:
        self.calls = 0
        self.entered: threading.Event | None = None
        self.release: threading.Event | None = None

    def recognize(self, *, page, frames):
        self.calls += 1
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            self.release.wait(2)
        return {
            "objects": [
                {
                    "business_id": "ap-1",
                    "label": "AP1",
                    "bbox": [10, 10, 20, 20],
                    "center": [20, 20],
                    "canvas_id": frames[0].canvas_id,
                    "confidence": 0.9,
                }
            ],
            "links": [],
            "vision_routing": {
                "decision": "model_assist",
                "model_invoked": True,
                "execution_status": "model_completed",
            },
        }


class VisionCacheCoordinatorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.assets = self.root / "assets"
        self.assets.mkdir()
        self.db_path = self.root / "cache.sqlite3"
        self.store = SQLiteVisionResultCacheStore(self.db_path)
        self.adapter = RecordingAdapter()
        self.page = {"url": "https://example.invalid/topology", "ui_version": "v1"}

    def frame(
        self,
        name: str,
        data: bytes,
        *,
        width: int = 100,
        height: int = 100,
        canvas_id: str = "canvas-1",
    ) -> CanvasFrame:
        path = self.assets / name
        path.write_bytes(data)
        return CanvasFrame(
            canvas_id=canvas_id,
            screenshot_path=path,
            screenshot_sha256=hashlib.sha256(data).hexdigest(),
            mime_type="image/png",
            width=width,
            height=height,
            client_width=float(width),
            client_height=float(height),
            bbox=(0.0, 0.0, float(width), float(height)),
            source_kind="canvas",
            source_type="canvas",
            capture_kind="element",
            region_selector="#topology",
            roi_status="verified",
        )

    def coordinator(self, **kwargs) -> VisionCacheCoordinator:
        return VisionCacheCoordinator(
            self.store,
            asset_root=self.assets,
            **kwargs,
        )

    def test_exact_pixels_are_reused_before_adapter_and_survive_restart(self):
        first_frame = self.frame("first.png", b"same-pixels")
        second_frame = self.frame("second.png", b"same-pixels")
        first = self.coordinator().recognize(
            adapter=self.adapter, page=self.page, frames=(first_frame,)
        )
        restarted = VisionCacheCoordinator(
            SQLiteVisionResultCacheStore(self.db_path),
            asset_root=self.assets,
        )
        second = restarted.recognize(
            adapter=self.adapter, page=self.page, frames=(second_frame,)
        )

        self.assertEqual(self.adapter.calls, 1)
        self.assertEqual(first["vision_cache"]["status"], "miss")
        self.assertEqual(second["vision_cache"]["status"], "exact_hit")
        self.assertTrue(second["vision_cache"]["model_invocation_avoided"])
        self.assertFalse(second["vision_routing"]["model_invoked"])
        self.assertEqual(
            second["vision_routing"]["source_execution_status"],
            "model_completed",
        )

    def test_page_identity_and_force_refresh_do_not_reuse(self):
        frame = self.frame("frame.png", b"pixels")
        coordinator = self.coordinator()
        coordinator.recognize(adapter=self.adapter, page=self.page, frames=(frame,))
        coordinator.recognize(
            adapter=self.adapter,
            page={**self.page, "url": "https://example.invalid/other"},
            frames=(frame,),
        )
        forced = coordinator.recognize(
            adapter=self.adapter,
            page=self.page,
            frames=(frame,),
            force_refresh=True,
        )

        self.assertEqual(self.adapter.calls, 3)
        self.assertEqual(forced["vision_cache"]["status"], "miss")

    def test_concurrent_identical_requests_are_coalesced(self):
        frame = self.frame("frame.png", b"pixels")
        coordinator = self.coordinator(wait_timeout_seconds=2)
        self.adapter.entered = threading.Event()
        self.adapter.release = threading.Event()

        with ThreadPoolExecutor(max_workers=2) as executor:
            leader = executor.submit(
                coordinator.recognize,
                adapter=self.adapter,
                page=self.page,
                frames=(frame,),
            )
            self.assertTrue(self.adapter.entered.wait(1))
            follower = executor.submit(
                coordinator.recognize,
                adapter=self.adapter,
                page=self.page,
                frames=(frame,),
            )
            self.adapter.release.set()
            statuses = {
                leader.result()["vision_cache"]["status"],
                follower.result()["vision_cache"]["status"],
            }

        self.assertEqual(self.adapter.calls, 1)
        self.assertIn("miss", statuses)
        self.assertIn(statuses - {"miss"}, ({"coalesced"}, {"exact_hit"}))

    def test_safe_scale_translation_reprojects_without_adapter(self):
        first_frame = self.frame("first.png", b"old", canvas_id="old-canvas")
        second_frame = self.frame(
            "second.png",
            b"new",
            width=200,
            height=200,
            canvas_id="new-canvas",
        )

        def safe_match(*args):
            return FrameMatchResult(
                reusable=True,
                scale_x=2.0,
                scale_y=2.0,
                translate_x=0.0,
                translate_y=0.0,
                similarity=0.999,
                reason="test_scale",
                overlap_ratio=1.0,
            )

        coordinator = self.coordinator(frame_matcher=safe_match)
        coordinator.recognize(
            adapter=self.adapter, page=self.page, frames=(first_frame,)
        )
        result = coordinator.recognize(
            adapter=self.adapter, page=self.page, frames=(second_frame,)
        )

        self.assertEqual(self.adapter.calls, 1)
        self.assertEqual(result["vision_cache"]["status"], "reprojected")
        self.assertEqual(result["objects"][0]["bbox"], [20.0, 20.0, 40.0, 40.0])
        self.assertEqual(result["objects"][0]["canvas_id"], "new-canvas")
        self.assertTrue(result["objects"][0]["analysis_only"])
        self.assertFalse(result["vision_routing"]["model_invoked"])

    def test_semantic_pixel_change_runs_adapter_again(self):
        first_frame = self.frame("first.png", b"old")
        second_frame = self.frame("second.png", b"changed")

        def unsafe_match(*args):
            return FrameMatchResult(
                reusable=False,
                scale_x=1.0,
                scale_y=1.0,
                translate_x=0.0,
                translate_y=0.0,
                similarity=0.5,
                reason="pixel_or_color_change_detected",
            )

        coordinator = self.coordinator(frame_matcher=unsafe_match)
        coordinator.recognize(
            adapter=self.adapter, page=self.page, frames=(first_frame,)
        )
        result = coordinator.recognize(
            adapter=self.adapter, page=self.page, frames=(second_frame,)
        )

        self.assertEqual(self.adapter.calls, 2)
        self.assertEqual(result["vision_cache"]["status"], "miss")

    def test_tampered_candidate_file_cannot_be_reprojected(self):
        first_frame = self.frame("first.png", b"old")
        second_frame = self.frame("second.png", b"new")
        matcher_called = False

        def matcher(*args):
            nonlocal matcher_called
            matcher_called = True
            raise AssertionError("tampered file must not reach image matcher")

        coordinator = self.coordinator(frame_matcher=matcher)
        coordinator.recognize(
            adapter=self.adapter, page=self.page, frames=(first_frame,)
        )
        first_frame.screenshot_path.write_bytes(b"tampered")
        coordinator.recognize(
            adapter=self.adapter, page=self.page, frames=(second_frame,)
        )

        self.assertFalse(matcher_called)
        self.assertEqual(self.adapter.calls, 2)


if __name__ == "__main__":
    unittest.main()
