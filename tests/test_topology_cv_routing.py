from __future__ import annotations

import json
import unittest

from kt6_backend.topology_cv_routing import (
    TopologyCVRoutingError,
    assess_cv_result,
    prepare_cv_payload_for_route,
)
from kt6_backend.topology_vision_contract import (
    CanvasVisionResponseError,
    RESPONSE_SCHEMA_VERSION,
    TopologyVisionContract,
)


TRUSTED_PROVENANCE = {
    "adapter_id": "local-cv-ocr",
    "adapter_version": "1.4",
}


def _assess(payload: dict, requested_profile: str) -> dict:
    return assess_cv_result(
        payload,
        requested_profile=requested_profile,
        trusted_provenance=TRUSTED_PROVENANCE,
    )


def _object(index: int, *, confidence: float = 0.93) -> dict:
    business_id = f"AP-{index:03d}"
    return {
        "business_id": business_id,
        "type": "access_point",
        "label": business_id,
        "canvas_id": "uploaded_topology",
        "bbox": [float(index * 10), 20.0, 8.0, 8.0],
        "confidence": confidence,
        "attributes": {
            "recognizer": "rapidocr",
            "source_region": "diagram",
            "ocr_text": business_id,
            "ocr_confidence": confidence,
        },
    }


def _link(
    source_index: int,
    target_index: int,
    *,
    evidence: str,
    confidence: float = 0.88,
) -> dict:
    source = f"AP-{source_index:03d}"
    target = f"AP-{target_index:03d}"
    return {
        "relation_id": f"local-line:{source}:{target}",
        "source": source,
        "target": target,
        "type": "topology_link",
        "confidence": confidence,
        "attributes": {
            "evidence": evidence,
            "direction": "undirected",
            "directed": False,
        },
    }


def _payload(
    object_count: int,
    *,
    links: list[dict] | None = None,
    scan_status: str = "complete",
    pixel_count: int = 0,
    component_count: int = 0,
    line_segment_count: int = 0,
    include_diagnostics: bool = True,
) -> dict:
    payload = {
        "schema_version": RESPONSE_SCHEMA_VERSION,
        "confidence": 0.91,
        "objects": [_object(index) for index in range(1, object_count + 1)],
        "links": list(links or []),
        "co_channel_relations": [],
    }
    if include_diagnostics:
        payload["diagnostics"] = {
            "producer": "local_cv_ocr",
            "connector_scan": {
                "status": scan_status,
                "pixel_count": pixel_count,
                "component_count": component_count,
                "line_segment_count": line_segment_count,
                "budget_exhausted": scan_status == "partial",
            },
        }
    return payload


class TopologyCVRoutingTest(unittest.TestCase):
    def test_scatter_nodes_only_skips_model_and_disputes_weak_links(self):
        weak_links = [
            _link(
                1,
                2,
                evidence="legacy_layered_pixel_component",
                confidence=0.82,
            ),
            _link(
                3,
                4,
                evidence="legacy_padded_hough_segment",
                confidence=0.79,
            ),
            _link(
                5,
                6,
                evidence="directional_probe_component",
                confidence=0.78,
            ),
        ]
        payload = _payload(
            34,
            links=weak_links,
            pixel_count=183,
            component_count=3,
            line_segment_count=3,
        )

        routing = _assess(payload, "nodes_only")
        prepared, disputed = prepare_cv_payload_for_route(payload, routing)

        self.assertEqual(routing["scene_type"], "scatter_nodes")
        self.assertEqual(routing["decision"], "cv_only")
        self.assertEqual(routing["result_status"], "partial")
        self.assertEqual(
            routing["connectivity_status"],
            "ambiguous_sparse_weak_evidence",
        )
        self.assertEqual(prepared["links"], [])
        self.assertFalse(prepared.get("no_connections", False))
        self.assertEqual(len(disputed), 3)
        self.assertTrue(
            all(
                item["attributes"]["relation_state"] == "disputed"
                and item["attributes"]["interaction_eligible"] is False
                for item in disputed
            )
        )

    def test_auto_scatter_uses_cv_nodes_without_inferring_connections(self):
        payload = _payload(
            34,
            links=[
                _link(
                    1,
                    2,
                    evidence="legacy_layered_pixel_component",
                    confidence=0.82,
                )
            ],
            pixel_count=80,
            component_count=1,
            line_segment_count=1,
        )

        routing = _assess(payload, "auto")
        prepared, disputed = prepare_cv_payload_for_route(payload, routing)

        self.assertEqual(routing["scene_type"], "scatter_nodes")
        self.assertEqual(routing["decision"], "cv_only")
        self.assertEqual(routing["effective_profile"], "nodes_only")
        self.assertTrue(routing["requirement_satisfied"])
        self.assertEqual(prepared["links"], [])
        self.assertEqual(len(disputed), 1)
        self.assertIn(
            "scatter_connectivity_intentionally_not_inferred",
            routing["reason_codes"],
        )

    def test_sparse_weak_scatter_requires_model_for_visible_topology(self):
        payload = _payload(
            34,
            links=[
                _link(
                    1,
                    2,
                    evidence="legacy_layered_pixel_component",
                    confidence=0.82,
                )
            ],
            pixel_count=80,
            component_count=1,
            line_segment_count=1,
        )

        routing = _assess(payload, "visible_topology")

        self.assertEqual(routing["scene_type"], "scatter_nodes")
        self.assertEqual(routing["decision"], "model_assist")
        self.assertIn(
            "verified_connectivity",
            routing["missing_capabilities"],
        )

    def test_complete_empty_scan_is_cv_only_but_not_a_hidden_link_answer(self):
        payload = _payload(20)

        nodes = _assess(payload, "nodes_only")
        visible = _assess(payload, "visible_topology")
        connectivity = _assess(payload, "connectivity_query")
        prepared, disputed = prepare_cv_payload_for_route(payload, nodes)

        self.assertEqual(nodes["decision"], "cv_only")
        self.assertEqual(visible["decision"], "model_assist")
        self.assertEqual(
            visible["connectivity_status"],
            "no_detected_connector_pixels",
        )
        self.assertFalse(prepared["no_connections"])
        self.assertEqual(disputed, [])
        self.assertEqual(connectivity["decision"], "insufficient")
        self.assertFalse(connectivity["requirement_satisfied"])

    def test_structured_direct_pixel_topology_is_cv_only(self):
        links = [
            _link(
                index,
                index + 1,
                evidence="connected_pixel_path",
                confidence=0.9,
            )
            for index in range(1, 10)
        ]
        payload = _payload(
            10,
            links=links,
            pixel_count=950,
            component_count=1,
            line_segment_count=9,
        )

        visible = _assess(payload, "visible_topology")
        connectivity = _assess(payload, "connectivity_query")

        self.assertEqual(visible["scene_type"], "structured_topology")
        self.assertEqual(visible["decision"], "cv_only")
        self.assertEqual(connectivity["decision"], "cv_only")
        self.assertIn(
            "direct_visible_connectivity",
            visible["satisfied_capabilities"],
        )

        automatic = _assess(payload, "auto")
        self.assertEqual(automatic["decision"], "cv_only")
        self.assertEqual(automatic["effective_profile"], "visible_topology")
        self.assertEqual(automatic["result_status"], "complete")

    def test_semantic_enrichment_always_uses_model(self):
        payload = _payload(20)

        routing = _assess(payload, "semantic_enrichment")

        self.assertEqual(routing["decision"], "model_assist")
        self.assertIn(
            "semantic_enrichment",
            routing["missing_capabilities"],
        )

    def test_missing_or_partial_diagnostics_do_not_claim_complete_topology(self):
        legacy_payload = _payload(20, include_diagnostics=False)
        partial_payload = _payload(
            20,
            scan_status="partial",
            line_segment_count=5000,
        )

        legacy = _assess(legacy_payload, "nodes_only")
        partial = _assess(partial_payload, "visible_topology")

        self.assertEqual(legacy["decision"], "model_assist")
        self.assertEqual(partial["decision"], "model_assist")
        self.assertEqual(partial["scene_type"], "complex_topology")

    def test_empty_cv_is_insufficient(self):
        routing = _assess(_payload(0), "nodes_only")

        self.assertEqual(routing["scene_type"], "unusable")
        self.assertEqual(routing["decision"], "insufficient")

    def test_rejects_duplicate_business_ids(self):
        payload = _payload(2)
        payload["objects"][1]["business_id"] = payload["objects"][0]["business_id"]

        with self.assertRaisesRegex(
            TopologyCVRoutingError,
            "duplicate CV business_id",
        ):
            _assess(payload, "nodes_only")

    def test_budget_exhaustion_fails_closed_for_visible_topology(self):
        payload = _payload(20)
        payload["diagnostics"]["connector_scan"]["budget_exhausted"] = True

        routing = _assess(payload, "visible_topology")

        self.assertEqual(routing["decision"], "model_assist")
        self.assertEqual(routing["scene_type"], "complex_topology")

    def test_raw_no_connections_is_rejected(self):
        payload = _payload(20)
        payload["no_connections"] = True

        with self.assertRaisesRegex(
            TopologyCVRoutingError,
            "must not assert",
        ):
            _assess(payload, "nodes_only")

    def test_structured_topology_rejects_any_weak_edge(self):
        links = [
            _link(
                index,
                index + 1,
                evidence=(
                    "legacy_layered_pixel_component"
                    if index == 5
                    else "connected_pixel_path"
                ),
                confidence=0.9,
            )
            for index in range(1, 10)
        ]
        payload = _payload(
            10,
            links=links,
            pixel_count=950,
            component_count=1,
            line_segment_count=9,
        )

        routing = _assess(payload, "visible_topology")

        self.assertEqual(routing["decision"], "model_assist")
        self.assertEqual(routing["scene_type"], "complex_topology")

    def test_untrusted_payload_cannot_skip_model(self):
        payload = _payload(20)

        routing = assess_cv_result(payload, requested_profile="nodes_only")

        self.assertEqual(routing["decision"], "model_assist")
        self.assertFalse(routing["metrics"]["trusted_adapter"])

    def test_shared_contract_rejects_local_only_diagnostics(self):
        payload = _payload(2)

        with self.assertRaisesRegex(
            CanvasVisionResponseError, "unsupported fields"
        ):
            TopologyVisionContract().parse_response_bytes(
                json.dumps(payload).encode("utf-8"),
                {"uploaded_topology": (1000, 1000)},
            )


if __name__ == "__main__":
    unittest.main()
