import unittest

from kt6_backend.hybrid_canvas_vision import (
    HybridCanvasVisionAdapter,
    HybridCanvasVisionError,
)


class StaticAdapter:
    adapter_id = "static"
    adapter_version = "1"
    supports_actionable_grounding = False

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = 0

    def recognize(self, *, page, frames):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


def local_result():
    return {
        "objects": [
            {
                "business_id": "GW-001",
                "type": "gateway",
                "label": "GW-001",
                "canvas_id": "c1",
                "bbox": [10, 10, 40, 20],
                "confidence": 0.95,
                "attributes": {"recognizer": "rapidocr"},
            },
            {
                "business_id": "CORE-001",
                "type": "core_switch",
                "label": "CORE-001",
                "canvas_id": "c1",
                "bbox": [10, 80, 60, 20],
                "confidence": 0.93,
                "attributes": {"recognizer": "rapidocr"},
            },
        ],
        "links": [
            {
                "source": "GW-001",
                "target": "CORE-001",
                "type": "topology_link",
                "confidence": 0.82,
                "attributes": {"evidence": "connected_pixel_path"},
            }
        ],
    }


def model_result():
    return {
        "objects": [
            {
                "business_id": "GW001",
                "type": "gateway",
                "label": "GW001",
                "canvas_id": "c1",
                "bbox": [10, 10, 40, 20],
                "confidence": 0.9,
                "attributes": {"vendor": "ZTE"},
            },
            {
                "business_id": "CORE-001",
                "type": "core_switch",
                "label": "CORE-001",
                "canvas_id": "c1",
                "bbox": [10, 80, 60, 20],
                "confidence": 0.9,
                "attributes": {"model": "S5731S-H24T4S-A"},
            },
        ],
        "links": [
            {
                "source": "GW001",
                "target": "CORE-001",
                "type": "topology_link",
                "confidence": 0.91,
                "attributes": {"direction": "downstream"},
            }
        ],
    }


class HybridCanvasVisionAdapterTest(unittest.TestCase):
    def test_fuses_local_geometry_and_model_semantics(self):
        local = StaticAdapter(local_result())
        model = StaticAdapter(model_result())
        adapter = HybridCanvasVisionAdapter(
            local_adapter=local,
            model_adapter=model,
        )

        result = adapter.recognize(page={"url": "test"}, frames=())

        self.assertIsNotNone(result)
        self.assertEqual(local.calls, 1)
        self.assertEqual(model.calls, 1)
        self.assertEqual(result["fusion_summary"]["confirmed_object_count"], 2)
        self.assertEqual(result["fusion_summary"]["confirmed_link_count"], 1)
        gw = next(item for item in result["objects"] if item["business_id"] == "GW-001")
        self.assertEqual(gw["bbox"], [10.0, 10.0, 40.0, 20.0])
        self.assertEqual(gw["attributes"]["model_semantics"]["vendor"], "ZTE")
        self.assertEqual(
            result["links"][0]["attributes"]["fusion_status"], "confirmed"
        )

    def test_model_failure_degrades_to_local_cv(self):
        adapter = HybridCanvasVisionAdapter(
            local_adapter=StaticAdapter(local_result()),
            model_adapter=StaticAdapter(error=RuntimeError("model unavailable")),
        )

        result = adapter.recognize(page={}, frames=())

        self.assertEqual(result["fusion_summary"]["degraded_to"], "local_cv")
        self.assertEqual(
            result["objects"][0]["attributes"]["fusion_status"], "cv_only"
        )

    def test_local_failure_degrades_to_model(self):
        adapter = HybridCanvasVisionAdapter(
            local_adapter=StaticAdapter(error=RuntimeError("cv unavailable")),
            model_adapter=StaticAdapter(model_result()),
        )

        result = adapter.recognize(page={}, frames=())

        self.assertEqual(
            result["fusion_summary"]["degraded_to"], "multimodal_model"
        )
        self.assertEqual(
            result["objects"][0]["attributes"]["fusion_status"], "model_only"
        )

    def test_both_fail_without_exposing_branch_error_details(self):
        adapter = HybridCanvasVisionAdapter(
            local_adapter=StaticAdapter(error=RuntimeError("local secret")),
            model_adapter=StaticAdapter(error=RuntimeError("model secret")),
        )

        with self.assertRaises(HybridCanvasVisionError) as raised:
            adapter.recognize(page={}, frames=())

        self.assertNotIn("secret", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
