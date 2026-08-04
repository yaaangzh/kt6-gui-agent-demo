import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kt6_backend.vision_frame_matcher import (
    FrameMatchResult,
    match_vision_frames,
    remap_bbox,
    remap_center,
    remap_vision_result,
)


OPENCV_AVAILABLE = importlib.util.find_spec("cv2") is not None


class VisionFrameMatcherSafetyTests(unittest.TestCase):
    def test_missing_opencv_is_a_safe_miss(self):
        with patch(
            "kt6_backend.vision_frame_matcher._load_opencv",
            return_value=(None, None),
        ):
            result = match_vision_frames(
                "old.png",
                "new.png",
                (100, 80),
                (100, 80),
            )

        self.assertFalse(result.reusable)
        self.assertFalse(result.safe_to_reuse)
        self.assertEqual(result.reason, "opencv_unavailable")
        self.assertEqual(result.similarity, 0.0)

    def test_invalid_dimensions_and_aspect_ratio_are_rejected_before_decode(self):
        invalid = match_vision_frames("old.png", "new.png", (0, 80), (100, 80))
        mismatch = match_vision_frames(
            "old.png",
            "new.png",
            (100, 100),
            (200, 100),
        )

        self.assertEqual(invalid.reason, "invalid_dimensions")
        self.assertEqual(mismatch.reason, "aspect_ratio_mismatch")
        self.assertFalse(invalid.reusable)
        self.assertFalse(mismatch.reusable)

    def test_bbox_and_center_are_remapped_from_old_to_new_pixels(self):
        match = FrameMatchResult(
            reusable=True,
            scale_x=1.5,
            scale_y=1.25,
            translate_x=10.0,
            translate_y=-5.0,
            similarity=0.999,
            reason="scale_translation_match",
            overlap_ratio=0.97,
        )

        self.assertEqual(remap_bbox([20, 30, 40, 50], match), [40, 32.5, 60, 62.5])
        self.assertEqual(remap_center([40, 55], match), [70, 63.75])

    def test_recursive_remap_is_a_deep_copy_and_forces_analysis_only(self):
        match = FrameMatchResult(
            reusable=True,
            scale_x=2.0,
            scale_y=2.0,
            translate_x=3.0,
            translate_y=4.0,
            similarity=1.0,
            reason="full_frame_scale_match",
            overlap_ratio=1.0,
        )
        original = {
            "objects": [
                {
                    "business_id": "site-a",
                    "bbox": [1, 2, 3, 4],
                    "center": [2.5, 4],
                    "actionable": True,
                    "interaction_eligible": True,
                    "interaction": {
                        "status": "ready",
                        "safe_for_execution": True,
                    },
                    "evidence": {"bbox": [5, 6, 1, 2]},
                }
            ],
            "bindings": {
                "site-a": {
                    "canvas_ref": "canvas:site-a",
                    "actionable": True,
                    "safe_for_execution": True,
                }
            },
            "provenance": {"actionable_grounding": True},
            "actionable_grounding": True,
        }

        remapped = remap_vision_result(original, match)

        self.assertEqual(original["objects"][0]["bbox"], [1, 2, 3, 4])
        obj = remapped["objects"][0]
        self.assertEqual(obj["bbox"], [5, 8, 6, 8])
        self.assertEqual(obj["center"], [8, 12])
        self.assertEqual(obj["evidence"]["bbox"], [13, 16, 2, 4])
        self.assertTrue(obj["analysis_only"])
        self.assertFalse(obj["actionable"])
        self.assertFalse(obj["interaction_eligible"])
        self.assertEqual(obj["interaction"]["status"], "analysis_only")
        self.assertFalse(obj["interaction"]["safe_for_execution"])
        binding = remapped["bindings"]["site-a"]
        self.assertFalse(binding["actionable"])
        self.assertFalse(binding["safe_for_execution"])
        self.assertTrue(remapped["analysis_only"])
        self.assertFalse(remapped["actionable_grounding"])
        self.assertFalse(remapped["provenance"]["actionable_grounding"])
        self.assertEqual(
            remapped["provenance"]["geometry_source"],
            "cached_vision_frame_transform",
        )

    def test_unsafe_match_cannot_be_used_for_geometry(self):
        miss = FrameMatchResult(
            reusable=False,
            scale_x=1.0,
            scale_y=1.0,
            translate_x=0.0,
            translate_y=0.0,
            similarity=0.5,
            reason="pixel_or_color_change_detected",
        )

        with self.assertRaisesRegex(ValueError, "unsafe frame match"):
            remap_bbox([0, 0, 10, 10], miss)
        with self.assertRaisesRegex(ValueError, "unsafe frame match"):
            remap_vision_result({"objects": []}, miss)


@unittest.skipUnless(OPENCV_AVAILABLE, "optional OpenCV runtime is not installed")
class VisionFrameMatcherOpenCVTests(unittest.TestCase):
    def setUp(self):
        import cv2
        import numpy as np

        self.cv2 = cv2
        self.np = np
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _feature_image(self, width=240, height=180):
        image = self.np.full((height, width, 3), 245, dtype=self.np.uint8)
        for x in range(20, width - 19, 30):
            self.cv2.line(image, (x, 20), (x, height - 21), (40, 40, 40), 1)
        for y in range(20, height - 19, 25):
            self.cv2.line(image, (20, y), (width - 21, y), (80, 80, 80), 1)
        for index, (x, y) in enumerate(((40, 40), (110, 55), (185, 45), (70, 125), (170, 125))):
            color = (30 + index * 20, 120, 210 - index * 20)
            self.cv2.circle(image, (x, y), 8, color, -1)
            self.cv2.putText(
                image,
                str(index),
                (x - 4, y + 4),
                self.cv2.FONT_HERSHEY_SIMPLEX,
                0.4,
                (0, 0, 0),
                1,
                self.cv2.LINE_AA,
            )
        return image

    def _write(self, name, image):
        path = self.root / name
        self.assertTrue(self.cv2.imwrite(str(path), image))
        return path

    def test_exact_pixels_are_reusable(self):
        image = self._feature_image()
        old_path = self._write("old.png", image)
        new_path = self._write("new.png", image.copy())

        result = match_vision_frames(old_path, new_path, (240, 180), (240, 180))

        self.assertTrue(result.reusable)
        self.assertEqual(result.reason, "exact_pixel_match")
        self.assertEqual(result.similarity, 1.0)

    def test_pure_full_frame_scaling_is_reusable(self):
        old = self._feature_image(200, 150)
        new = self.cv2.resize(old, (240, 180), interpolation=self.cv2.INTER_LINEAR)
        old_path = self._write("old-scale.png", old)
        new_path = self._write("new-scale.png", new)

        result = match_vision_frames(old_path, new_path, (200, 150), (240, 180))

        self.assertTrue(result.reusable)
        self.assertEqual(result.reason, "full_frame_scale_match")
        self.assertAlmostEqual(result.scale_x, 1.2)
        self.assertAlmostEqual(result.scale_y, 1.2)

    def test_small_translation_with_blank_new_border_is_reusable(self):
        old = self._feature_image()
        transform = self.np.float32([[1, 0, 7], [0, 1, 4]])
        new = self.cv2.warpAffine(
            old,
            transform,
            (240, 180),
            flags=self.cv2.INTER_LINEAR,
            borderMode=self.cv2.BORDER_CONSTANT,
            borderValue=(245, 245, 245),
        )
        old_path = self._write("old-pan.png", old)
        new_path = self._write("new-pan.png", new)

        result = match_vision_frames(old_path, new_path, (240, 180), (240, 180))

        self.assertTrue(result.reusable, result.to_dict())
        self.assertEqual(result.reason, "scale_translation_match")
        self.assertAlmostEqual(result.translate_x, 7, delta=0.75)
        self.assertAlmostEqual(result.translate_y, 4, delta=0.75)

    def test_small_color_change_is_not_reusable(self):
        old = self._feature_image()
        new = old.copy()
        self.cv2.rectangle(new, (100, 70), (116, 86), (0, 0, 255), -1)
        old_path = self._write("old-color.png", old)
        new_path = self._write("new-color.png", new)

        result = match_vision_frames(old_path, new_path, (240, 180), (240, 180))

        self.assertFalse(result.reusable, result.to_dict())


if __name__ == "__main__":
    unittest.main()
