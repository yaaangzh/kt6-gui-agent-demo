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


class ContextAwareAdapter(StaticAdapter):
    def __init__(self, result=None, error=None):
        super().__init__(result=result, error=error)
        self.cv_observations = None

    def recognize_with_context(self, *, page, frames, cv_observations):
        self.calls += 1
        self.cv_observations = cv_observations
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
        self.assertIn("structure_templates", result["fusion_analysis"])
        self.assertIn("node_coordinate_mappings", result["fusion_analysis"])
        gw_mapping = next(
            item
            for item in result["fusion_analysis"]["node_coordinate_mappings"]
            if item["semantic_node_id"] == "GW-001"
        )
        self.assertEqual(gw_mapping["model_node_id"], "GW001")
        self.assertEqual(gw_mapping["center"], [30.0, 20.0])
        self.assertIn("grounded_graph", result["fusion_analysis"])
        self.assertIn("display_graph", result["fusion_analysis"])
        self.assertIn("semantic_graph", result["fusion_analysis"])
        self.assertEqual(
            result["objects"],
            result["fusion_analysis"]["grounded_graph"]["objects"],
        )

    def test_inferred_geometry_stays_inside_display_analysis(self):
        model = {
            "topology": {
                "layers": [
                    {
                        "name": "network",
                        "devices": [
                            {
                                "id": "GW001",
                                "connections": {"down": ["CORE-001"]},
                            },
                            {
                                "id": "AGG-003",
                                "connections": {"up": ["CORE-001"]},
                            },
                            {
                                "id": "CORE-001",
                                "connections": {
                                    "up": ["GW001"],
                                    "down": ["AGG-003"],
                                },
                            },
                        ],
                    }
                ]
            }
        }
        adapter = HybridCanvasVisionAdapter(
            local_adapter=StaticAdapter(local_result()),
            model_adapter=StaticAdapter(model),
        )

        result = adapter.recognize(page={"url": "test"}, frames=())

        self.assertNotIn("AGG-003", {item["business_id"] for item in result["objects"]})
        self.assertNotIn(
            "AGG-003",
            {endpoint for link in result["links"] for endpoint in (link["source"], link["target"])},
        )
        display_graph = result["fusion_analysis"]["display_graph"]
        self.assertIn(
            "AGG-003", {item["business_id"] for item in display_graph["objects"]}
        )
        agg_mapping = next(
            item
            for item in result["fusion_analysis"]["node_coordinate_mappings"]
            if item["semantic_node_id"] == "AGG-003"
        )
        self.assertEqual(agg_mapping["mapping_status"], "unmatched")
        self.assertEqual(agg_mapping["geometry_status"], "spatially_inferred")
        self.assertTrue(agg_mapping["rendering_only"])
        self.assertIn(
            "AGG-003",
            {
                endpoint
                for link in display_graph["links"]
                for endpoint in (link["source"], link["target"])
            },
        )
        self.assertEqual(
            result["objects"],
            result["fusion_analysis"]["grounded_graph"]["objects"],
        )

    def test_passes_local_cv_candidates_to_context_aware_model(self):
        local = StaticAdapter(local_result())
        model = ContextAwareAdapter(model_result())
        adapter = HybridCanvasVisionAdapter(local_adapter=local, model_adapter=model)

        result = adapter.recognize(page={"url": "test"}, frames=())

        self.assertIsNotNone(result)
        self.assertEqual(model.calls, 1)
        self.assertIs(model.cv_observations, local.result)
        self.assertEqual(
            model.cv_observations["objects"][0]["business_id"], "GW-001"
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
