from __future__ import annotations

import copy
import math
from collections.abc import Mapping, Sequence
from typing import Any

from .topology_vision_contract import TopologyVisionContract


ROUTING_SCHEMA_VERSION = "kt6.topology-routing.v1"
ROUTING_POLICY_VERSION = "cv-route-v1"

TASK_PROFILES = frozenset(
    {
        "auto",
        "nodes_only",
        "visible_topology",
        "semantic_enrichment",
        "connectivity_query",
    }
)
ROUTE_DECISIONS = frozenset({"cv_only", "model_assist", "insufficient"})
SCENE_TYPES = frozenset(
    {
        "structured_topology",
        "scatter_nodes",
        "complex_topology",
        "unusable",
    }
)

_STRONG_PIXEL_LINK_EVIDENCE = frozenset(
    {
        "connected_pixel_path",
        "multi_angle_pixel_connector",
        "orthogonal_pixel_connector",
        "pixel_corridor_connector",
    }
)
_WEAK_PIXEL_LINK_EVIDENCE = frozenset(
    {
        "legacy_layered_pixel_component",
        "legacy_padded_hough_segment",
        "directional_probe_component",
    }
)

_HIGH_NODE_CONFIDENCE = 0.75
_NODE_MEDIAN_THRESHOLD = 0.82
_NODE_P10_THRESHOLD = 0.72
_MAX_LOW_NODE_RATIO = 0.15
_MAX_CORRECTED_NODE_RATIO = 0.15
_LINK_P10_THRESHOLD = 0.76
_SCATTER_MIN_OBJECTS = 12


class TopologyCVRoutingError(ValueError):
    """The trusted CV artifact cannot be assessed deterministically."""


def assess_cv_result(
    cv_payload: Mapping[str, Any],
    *,
    requested_profile: str = "auto",
    trusted_provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify trusted local-CV evidence and choose the cheapest safe route."""

    profile = str(requested_profile).strip()
    if profile not in TASK_PROFILES:
        supported = ", ".join(sorted(TASK_PROFILES))
        raise TopologyCVRoutingError(
            f"requested_profile must be one of: {supported}"
        )
    if not isinstance(cv_payload, Mapping):
        raise TopologyCVRoutingError("CV routing input must be an object")

    provenance = trusted_provenance or {}
    if not isinstance(provenance, Mapping):
        raise TopologyCVRoutingError("trusted_provenance must be an object")
    trusted_adapter = (
        provenance.get("adapter_id") == "local-cv-ocr"
        and provenance.get("adapter_version") == "1.4"
    )
    raw_no_connections = cv_payload.get("no_connections", False)
    if not isinstance(raw_no_connections, bool):
        raise TopologyCVRoutingError("CV no_connections must be boolean")
    if raw_no_connections:
        raise TopologyCVRoutingError(
            "local CV artifacts must not assert protocol-level no_connections"
        )

    objects = _records(
        cv_payload.get("objects", []),
        "objects",
        TopologyVisionContract.MAX_OBJECTS,
    )
    links = _records(
        cv_payload.get("links", []),
        "links",
        TopologyVisionContract.MAX_RELATIONS,
    )

    object_ids: set[str] = set()
    node_confidences: list[float] = []
    corrected_count = 0
    pass_through_count = 0
    diagram_count = 0
    local_cv_object_count = 0
    for index, item in enumerate(objects):
        business_id = str(item.get("business_id", "")).strip()
        if not business_id:
            raise TopologyCVRoutingError(
                f"objects[{index}].business_id is required"
            )
        if business_id in object_ids:
            raise TopologyCVRoutingError(
                f"duplicate CV business_id: {business_id}"
            )
        object_ids.add(business_id)
        node_confidences.append(
            _confidence(item.get("confidence"), f"objects[{index}].confidence")
        )
        attributes = _attributes(item.get("attributes", {}), f"objects[{index}]")
        if attributes.get("ocr_identifier_corrected") is True:
            corrected_count += 1
        if attributes.get("pixel_role") == "pass_through_ocr_candidate":
            pass_through_count += 1
        if str(attributes.get("source_region", "")) == "diagram":
            diagram_count += 1
        if str(attributes.get("recognizer", "")).strip() == "rapidocr":
            local_cv_object_count += 1

    pixel_links: list[Mapping[str, Any]] = []
    strong_links: list[Mapping[str, Any]] = []
    weak_links: list[Mapping[str, Any]] = []
    unknown_pixel_links: list[Mapping[str, Any]] = []
    table_link_count = 0
    link_confidences: list[float] = []
    linked_object_ids: set[str] = set()
    for index, item in enumerate(links):
        source = str(item.get("source", "")).strip()
        target = str(item.get("target", "")).strip()
        if source not in object_ids or target not in object_ids or source == target:
            raise TopologyCVRoutingError(
                f"links[{index}] has invalid endpoints"
            )
        confidence = _confidence(
            item.get("confidence"), f"links[{index}].confidence"
        )
        attributes = _attributes(item.get("attributes", {}), f"links[{index}]")
        evidence = str(attributes.get("evidence", "")).strip().casefold()
        if evidence == "device_detail_table":
            table_link_count += 1
            continue
        pixel_links.append(item)
        link_confidences.append(confidence)
        linked_object_ids.update((source, target))
        if evidence in _STRONG_PIXEL_LINK_EVIDENCE:
            strong_links.append(item)
        elif evidence in _WEAK_PIXEL_LINK_EVIDENCE:
            weak_links.append(item)
        else:
            unknown_pixel_links.append(item)

    object_count = len(objects)
    pixel_link_count = len(pixel_links)
    node_median = _percentile(node_confidences, 0.5)
    node_p10 = _percentile(node_confidences, 0.1)
    low_node_ratio = _ratio(
        sum(value < _HIGH_NODE_CONFIDENCE for value in node_confidences),
        object_count,
    )
    corrected_ratio = _ratio(corrected_count, object_count)
    diagram_ratio = _ratio(diagram_count, object_count)
    local_cv_ratio = _ratio(local_cv_object_count, object_count)
    link_p10 = _percentile(link_confidences, 0.1)
    linked_object_ratio = _ratio(len(linked_object_ids), object_count)
    strong_link_ratio = _ratio(len(strong_links), pixel_link_count)
    node_quality_sufficient = (
        object_count > 0
        and node_median >= _NODE_MEDIAN_THRESHOLD
        and node_p10 >= _NODE_P10_THRESHOLD
        and low_node_ratio <= _MAX_LOW_NODE_RATIO
        and corrected_ratio <= _MAX_CORRECTED_NODE_RATIO
    )

    diagnostics = cv_payload.get("diagnostics", {})
    if diagnostics is None:
        diagnostics = {}
    if not isinstance(diagnostics, Mapping):
        raise TopologyCVRoutingError("CV diagnostics must be an object")
    diagnostics_producer = str(diagnostics.get("producer", "")).strip()
    connector_scan = diagnostics.get("connector_scan", {})
    if connector_scan is None:
        connector_scan = {}
    if not isinstance(connector_scan, Mapping):
        raise TopologyCVRoutingError(
            "CV diagnostics.connector_scan must be an object"
        )
    scan_status = str(connector_scan.get("status", "unknown")).strip()
    connector_pixel_count = _non_negative_int(
        connector_scan.get("pixel_count", 0),
        "diagnostics.connector_scan.pixel_count",
    )
    connector_component_count = _non_negative_int(
        connector_scan.get("component_count", 0),
        "diagnostics.connector_scan.component_count",
    )
    line_segment_count = _non_negative_int(
        connector_scan.get("line_segment_count", 0),
        "diagnostics.connector_scan.line_segment_count",
    )
    budget_exhausted = connector_scan.get("budget_exhausted", False)
    if not isinstance(budget_exhausted, bool):
        raise TopologyCVRoutingError(
            "diagnostics.connector_scan.budget_exhausted must be boolean"
        )
    trusted_local_cv = (
        object_count > 0
        and trusted_adapter
        and diagnostics_producer == "local_cv_ocr"
        and local_cv_ratio == 1.0
        and scan_status in {"complete", "partial", "not_applicable"}
    )

    scatter_weak_link_limit = max(1, math.floor(object_count * 0.10))
    scatter_signature = (
        trusted_local_cv
        and scan_status == "complete"
        and not budget_exhausted
        and object_count >= _SCATTER_MIN_OBJECTS
        and node_quality_sufficient
        and diagram_ratio >= 0.9
        and table_link_count == 0
        and not strong_links
        and not unknown_pixel_links
        and len(weak_links) <= scatter_weak_link_limit
        and linked_object_ratio <= 0.25
        and pass_through_count == 0
    )
    no_detected_connector_pixels = (
        scatter_signature
        and pixel_link_count == 0
        and connector_pixel_count == 0
        and connector_component_count == 0
        and line_segment_count == 0
    )
    structured_signature = (
        trusted_local_cv
        and scan_status == "complete"
        and not budget_exhausted
        and object_count >= 2
        and node_quality_sufficient
        and pixel_link_count > 0
        and table_link_count == 0
        and not unknown_pixel_links
        and not weak_links
        and strong_link_ratio == 1.0
        and link_p10 >= _LINK_P10_THRESHOLD
        and linked_object_ratio == 1.0
        and pixel_link_count / object_count <= 1.0
        and pass_through_count == 0
    )

    if object_count == 0:
        scene_type = "unusable"
        connectivity_status = "unknown"
        scene_reasons = ["cv_found_no_topology_objects"]
    elif scatter_signature:
        scene_type = "scatter_nodes"
        connectivity_status = (
            "no_detected_connector_pixels"
            if no_detected_connector_pixels
            else "ambiguous_sparse_weak_evidence"
        )
        scene_reasons = [
            "high_quality_node_inventory",
            (
                "complete_scan_observed_no_connector_pixels"
                if no_detected_connector_pixels
                else "only_sparse_weak_connector_candidates"
            ),
        ]
    elif structured_signature:
        scene_type = "structured_topology"
        connectivity_status = "verified_direct_pixel_links"
        scene_reasons = [
            "high_quality_node_inventory",
            "direct_pixel_links_cover_most_nodes",
        ]
    else:
        scene_type = "complex_topology"
        connectivity_status = "ambiguous_or_partial"
        scene_reasons = ["cv_evidence_requires_additional_interpretation"]

    effective_profile = profile
    if scene_type == "unusable":
        decision = "insufficient"
        requirement_satisfied = False
        reason_codes = scene_reasons
    elif profile == "auto":
        if scene_type == "scatter_nodes":
            effective_profile = "nodes_only"
            decision = "cv_only"
            requirement_satisfied = True
            reason_codes = scene_reasons + [
                "auto_selected_nodes_only_for_scatter_scene",
                "scatter_connectivity_intentionally_not_inferred",
            ]
        elif scene_type == "structured_topology":
            effective_profile = "visible_topology"
            decision = "cv_only"
            requirement_satisfied = True
            reason_codes = scene_reasons + [
                "auto_selected_visible_topology_for_structured_scene"
            ]
        else:
            effective_profile = "visible_topology"
            decision = "model_assist"
            requirement_satisfied = False
            reason_codes = scene_reasons + [
                "auto_selected_model_for_complex_scene"
            ]
    elif profile == "semantic_enrichment":
        decision = "model_assist"
        requirement_satisfied = False
        reason_codes = scene_reasons + ["semantic_enrichment_requires_model"]
    elif profile == "nodes_only":
        if node_quality_sufficient and trusted_local_cv:
            decision = "cv_only"
            requirement_satisfied = True
            reason_codes = scene_reasons + ["requested_profile_needs_nodes_only"]
        else:
            decision = "model_assist"
            requirement_satisfied = False
            reason_codes = scene_reasons + [
                "node_inventory_does_not_meet_cv_quality_gate"
            ]
    elif profile == "visible_topology":
        if scene_type == "structured_topology":
            decision = "cv_only"
            requirement_satisfied = True
            reason_codes = scene_reasons + [
                "visible_topology_satisfied_by_local_cv"
            ]
        else:
            decision = "model_assist"
            requirement_satisfied = False
            reason_codes = scene_reasons + [
                "visible_topology_has_unresolved_pixel_evidence"
            ]
    else:
        if scene_type == "structured_topology":
            decision = "cv_only"
            requirement_satisfied = True
            reason_codes = scene_reasons + [
                "connectivity_query_satisfied_by_direct_pixel_links"
            ]
        elif no_detected_connector_pixels:
            decision = "insufficient"
            requirement_satisfied = False
            reason_codes = scene_reasons + [
                "connectivity_query_has_no_visible_connector_evidence"
            ]
        else:
            decision = "model_assist"
            requirement_satisfied = False
            reason_codes = scene_reasons + [
                "connectivity_query_has_ambiguous_pixel_evidence"
            ]

    if decision not in ROUTE_DECISIONS or scene_type not in SCENE_TYPES:
        raise AssertionError("invalid internal CV routing decision")

    satisfied_capabilities = (
        ["object_identity", "analysis_only_image_geometry"]
        if object_count
        else []
    )
    if scene_type == "structured_topology":
        satisfied_capabilities.append("direct_visible_connectivity")
    missing_capabilities: list[str] = []
    if connectivity_status in {
        "unknown",
        "ambiguous_or_partial",
        "ambiguous_sparse_weak_evidence",
        "no_detected_connector_pixels",
    }:
        missing_capabilities.append("verified_connectivity")
    if object_count:
        missing_capabilities.append("actionable_canvas_binding")
    if profile == "semantic_enrichment":
        missing_capabilities.append("semantic_enrichment")

    return {
        "schema_version": ROUTING_SCHEMA_VERSION,
        "policy_version": ROUTING_POLICY_VERSION,
        "decision": decision,
        "scene_type": scene_type,
        "requested_profile": profile,
        "effective_profile": effective_profile,
        "requirement_satisfied": requirement_satisfied,
        "result_status": (
            "complete"
            if decision == "cv_only" and effective_profile != "nodes_only"
            else "partial"
            if decision == "cv_only"
            else "insufficient"
            if decision == "insufficient"
            else "pending_model"
        ),
        "connectivity_status": connectivity_status,
        "reason_codes": list(dict.fromkeys(reason_codes)),
        "satisfied_capabilities": satisfied_capabilities,
        "missing_capabilities": list(dict.fromkeys(missing_capabilities)),
        "model_invoked": decision == "model_assist",
        "metrics": {
            "object_count": object_count,
            "pixel_link_count": pixel_link_count,
            "table_link_count": table_link_count,
            "strong_pixel_link_count": len(strong_links),
            "weak_pixel_link_count": len(weak_links),
            "unknown_pixel_link_count": len(unknown_pixel_links),
            "linked_object_ratio": round(linked_object_ratio, 4),
            "object_confidence_median": round(node_median, 4),
            "object_confidence_p10": round(node_p10, 4),
            "low_confidence_object_ratio": round(low_node_ratio, 4),
            "corrected_identifier_ratio": round(corrected_ratio, 4),
            "diagram_object_ratio": round(diagram_ratio, 4),
            "pass_through_object_count": pass_through_count,
            "local_cv_object_ratio": round(local_cv_ratio, 4),
            "connector_scan": scan_status,
            "connector_pixel_count": connector_pixel_count,
            "connector_component_count": connector_component_count,
            "line_segment_count": line_segment_count,
            "connector_scan_budget_exhausted": budget_exhausted,
            "trusted_adapter": trusted_adapter,
        },
    }


def prepare_cv_payload_for_route(
    cv_payload: Mapping[str, Any],
    decision: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Remove only untrusted links from a CV-only result and retain audit copies."""

    prepared = copy.deepcopy(dict(cv_payload))
    raw_links = prepared.get("links", [])
    if not isinstance(raw_links, list):
        raise TopologyCVRoutingError("CV links must be a list")
    if decision.get("decision") != "cv_only":
        return prepared, []
    prepared["no_connections"] = False

    profile = str(
        decision.get(
            "effective_profile",
            decision.get("requested_profile", ""),
        )
    )
    retained: list[dict[str, Any]] = []
    disputed: list[dict[str, Any]] = []
    for index, raw_link in enumerate(raw_links):
        if not isinstance(raw_link, Mapping):
            raise TopologyCVRoutingError(f"links[{index}] must be an object")
        item = copy.deepcopy(dict(raw_link))
        attributes = item.get("attributes", {})
        if not isinstance(attributes, dict):
            raise TopologyCVRoutingError(
                f"links[{index}].attributes must be an object"
            )
        evidence = str(attributes.get("evidence", "")).strip().casefold()
        should_dispute = (
            profile == "nodes_only"
            or evidence in _WEAK_PIXEL_LINK_EVIDENCE
        )
        if not should_dispute:
            retained.append(item)
            continue
        attributes.update(
            {
                "fusion_status": "routing_disputed",
                "relation_state": "disputed",
                "interaction_eligible": False,
                "evidence_sources": ["local_cv"],
                "routing_reason": (
                    "nodes_only_profile_does_not_validate_connectivity"
                    if profile == "nodes_only"
                    else "scatter_scene_contains_only_weak_connector_evidence"
                ),
            }
        )
        item["attributes"] = attributes
        disputed.append(item)

    prepared["links"] = retained
    return prepared, disputed


def _records(value: Any, name: str, maximum: int) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(
        value, (str, bytes, bytearray)
    ):
        raise TopologyCVRoutingError(f"CV {name} must be a list")
    if len(value) > maximum:
        raise TopologyCVRoutingError(f"CV {name} exceeds the routing limit")
    records: list[Mapping[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise TopologyCVRoutingError(f"CV {name}[{index}] must be an object")
        records.append(item)
    return records


def _attributes(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TopologyCVRoutingError(f"{context}.attributes must be an object")
    return value


def _confidence(value: Any, context: str) -> float:
    if isinstance(value, bool):
        raise TopologyCVRoutingError(f"{context} must be a number in [0, 1]")
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise TopologyCVRoutingError(
            f"{context} must be a number in [0, 1]"
        ) from exc
    if not math.isfinite(numeric) or not 0 <= numeric <= 1:
        raise TopologyCVRoutingError(f"{context} must be a number in [0, 1]")
    return numeric


def _non_negative_int(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TopologyCVRoutingError(f"{context} must be a non-negative integer")
    return value


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.floor((len(ordered) - 1) * fraction)))
    return ordered[index]


__all__ = [
    "ROUTE_DECISIONS",
    "ROUTING_POLICY_VERSION",
    "ROUTING_SCHEMA_VERSION",
    "SCENE_TYPES",
    "TASK_PROFILES",
    "TopologyCVRoutingError",
    "assess_cv_result",
    "prepare_cv_payload_for_route",
]
