from __future__ import annotations

import copy
import math
import re
import unicodedata
from collections.abc import Mapping
from typing import Any

from .topology_vision_contract import RESPONSE_SCHEMA_VERSION, TopologyVisionContract


FUSION_SCHEMA_VERSION = "kt6.topology-fusion.v1"
DEFAULT_MODEL_CONFIDENCE = 0.5

_RESERVED_ATTRIBUTE_KEYS = frozenset(
    {
        "actionable",
        "actionability",
        "actionable_grounding",
        "pixel_inference_performed",
        "pixel_verified",
        "provenance",
        "semantic_source",
    }
)
_GENERIC_OBJECT_TYPES = frozenset({"", "object", "device", "network_device", "unknown"})
_LINE_STYLES = frozenset({"solid", "dashed", "dotted", "dash", "dot"})


class TopologyFusionError(ValueError):
    """Raised when an offline CV or model result cannot be normalized safely."""


def fuse_topology_payloads(
    cv_payload: Mapping[str, Any],
    model_payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Fuse grounded CV geometry with model-proposed topology semantics.

    Only CV-grounded objects are placed in ``result.objects``. Model-only objects
    and links with ungrounded endpoints are retained separately so they cannot be
    mistaken for actionable pixel coordinates.
    """

    cv_objects, cv_links, canvas_id = _normalize_cv_payload(cv_payload)
    model_nodes, model_links, model_metadata = _normalize_model_payload(model_payload)
    cv_index = _CVIdentifierIndex(cv_objects)

    model_matches: dict[str, tuple[str, dict[str, Any]]] = {}
    unlocated_objects: list[dict[str, Any]] = []
    for model_id, model_node in model_nodes.items():
        cv_id = cv_index.resolve(model_id)
        if cv_id is None:
            unlocated_objects.append(_unlocated_object(model_id, model_node))
            continue
        existing = model_matches.get(cv_id)
        if existing is None:
            model_matches[cv_id] = (model_id, copy.deepcopy(model_node))
        else:
            existing[1].update(copy.deepcopy(model_node))

    fused_objects = [
        _fuse_object(cv_object, model_matches.get(str(cv_object["business_id"])), canvas_id)
        for cv_object in cv_objects
    ]

    normalized_model_links: list[dict[str, Any]] = []
    unresolved_links: list[dict[str, Any]] = []
    for model_link in model_links:
        source = cv_index.resolve(str(model_link["source"]))
        target = cv_index.resolve(str(model_link["target"]))
        if source is None or target is None:
            confidence = (
                float(model_link["confidence"])
                if model_link.get("confidence") is not None
                else DEFAULT_MODEL_CONFIDENCE
            )
            attributes = _safe_attributes(model_link.get("attributes", {}))
            attributes.update(
                {
                    "fusion_status": "model_only",
                    "evidence_sources": ["multimodal_model"],
                    "geometry_status": "unresolved_endpoint",
                    "resolution_reason": "model_endpoint_has_no_cv_geometry",
                    "confidence_basis": (
                        "model_reported"
                        if model_link.get("confidence") is not None
                        else "uncalibrated_default"
                    ),
                }
            )
            unresolved_links.append(
                {
                    "relation_id": (
                        f"fusion-unresolved:{source or model_link['source']}:"
                        f"{target or model_link['target']}"
                    ),
                    "source": source or str(model_link["source"]),
                    "target": target or str(model_link["target"]),
                    "type": str(model_link.get("type", "topology_link")),
                    "confidence": round(confidence, 4),
                    "attributes": attributes,
                }
            )
            continue
        if source == target:
            continue
        item = copy.deepcopy(model_link)
        item["source"] = source
        item["target"] = target
        normalized_model_links.append(item)

    model_layers_by_cv_id = {
        cv_id: str(model_node.get("layer", "")).strip()
        for cv_id, (_model_id, model_node) in model_matches.items()
        if str(model_node.get("layer", "")).strip()
    }
    fused_links = _fuse_links(
        cv_links,
        normalized_model_links,
        model_layers_by_cv_id=model_layers_by_cv_id,
    )
    confidence_values = [float(item["confidence"]) for item in fused_objects]
    confidence_values.extend(float(item["confidence"]) for item in fused_links)
    global_confidence = (
        round(sum(confidence_values) / len(confidence_values), 4)
        if confidence_values
        else 0.0
    )

    object_statuses = _status_counts(fused_objects)
    link_statuses = _status_counts(fused_links)
    result = {
        "schema_version": RESPONSE_SCHEMA_VERSION,
        "confidence": global_confidence,
        "objects": fused_objects,
        "links": fused_links,
        "co_channel_relations": [],
    }
    semantic_nodes = copy.deepcopy(fused_objects) + copy.deepcopy(unlocated_objects)
    semantic_links = copy.deepcopy(fused_links) + copy.deepcopy(unresolved_links)
    return {
        "schema_version": FUSION_SCHEMA_VERSION,
        "summary": {
            "cv_object_count": len(cv_objects),
            "model_object_count": len(model_nodes),
            "fused_object_count": len(fused_objects),
            "confirmed_object_count": object_statuses.get("confirmed", 0),
            "cv_only_object_count": object_statuses.get("cv_only", 0),
            "unlocated_model_object_count": len(unlocated_objects),
            "cv_link_count": len(cv_links),
            "model_link_count": len(model_links),
            "fused_link_count": len(fused_links),
            "confirmed_link_count": link_statuses.get("confirmed", 0),
            "cv_only_link_count": link_statuses.get("cv_only", 0),
            "model_only_link_count": link_statuses.get("model_only", 0),
            "conflict_link_count": link_statuses.get("conflict", 0),
            "unresolved_model_link_count": len(unresolved_links),
        },
        "model_metadata": model_metadata,
        "result": result,
        "semantic_graph": {
            "nodes": semantic_nodes,
            "links": semantic_links,
        },
        "unlocated_objects": sorted(
            unlocated_objects, key=lambda item: _identifier_sort_key(item["business_id"])
        ),
        "unresolved_links": sorted(
            unresolved_links,
            key=lambda item: (
                _identifier_sort_key(item["source"]),
                _identifier_sort_key(item["target"]),
            ),
        ),
    }


class _CVIdentifierIndex:
    def __init__(self, objects: list[dict[str, Any]]) -> None:
        self._exact: dict[str, str] = {}
        compact_candidates: dict[str, list[str]] = {}
        for item in objects:
            business_id = str(item["business_id"])
            exact = _exact_identifier_key(business_id)
            if exact in self._exact:
                raise TopologyFusionError(f"duplicate CV business_id: {business_id}")
            self._exact[exact] = business_id
            compact_candidates.setdefault(_compact_identifier_key(business_id), []).append(
                business_id
            )
        self._compact = {
            key: values[0] for key, values in compact_candidates.items() if len(values) == 1
        }

    def resolve(self, value: str) -> str | None:
        exact = self._exact.get(_exact_identifier_key(value))
        if exact is not None:
            return exact
        return self._compact.get(_compact_identifier_key(value))


def _normalize_cv_payload(
    payload: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    if not isinstance(payload, Mapping):
        raise TopologyFusionError("CV JSON root must be an object")
    scene = payload.get("scene")
    source: Mapping[str, Any]
    if isinstance(scene, Mapping):
        source = scene
    else:
        source = payload

    raw_objects = source.get("objects")
    if not isinstance(raw_objects, list):
        raw_objects = source.get("elements")
    if not isinstance(raw_objects, list):
        raise TopologyFusionError("CV JSON must contain objects or elements")
    if len(raw_objects) > TopologyVisionContract.MAX_OBJECTS:
        raise TopologyFusionError("CV JSON contains too many objects")

    canvas_id = _infer_canvas_id(source)
    objects: list[dict[str, Any]] = []
    object_ids: set[str] = set()
    for index, raw_object in enumerate(raw_objects):
        if not isinstance(raw_object, Mapping):
            raise TopologyFusionError(f"CV object {index} must be an object")
        business_id = _required_identifier(
            raw_object.get("business_id", raw_object.get("id")), f"CV object {index}"
        )
        exact_key = _exact_identifier_key(business_id)
        if exact_key in object_ids:
            raise TopologyFusionError(f"duplicate CV business_id: {business_id}")
        object_ids.add(exact_key)
        bbox = _bbox(raw_object.get("bbox"), f"CV object {business_id}")
        confidence = _confidence(raw_object.get("confidence"), default=0.5)
        attributes = _safe_attributes(raw_object.get("attributes", {}))
        objects.append(
            {
                "business_id": business_id,
                "type": _text(raw_object.get("type"), "network_device", 100),
                "label": _text(raw_object.get("label"), business_id, 500),
                "canvas_id": _text(raw_object.get("canvas_id"), canvas_id, 200),
                "bbox": bbox,
                "confidence": confidence,
                "attributes": attributes,
            }
        )

    raw_links = source.get("links")
    if not isinstance(raw_links, list):
        raw_links = source.get("relations", [])
    if not isinstance(raw_links, list):
        raise TopologyFusionError("CV links or relations must be a list")
    if len(raw_links) > TopologyVisionContract.MAX_RELATIONS:
        raise TopologyFusionError("CV JSON contains too many links")
    canonical_ids = {
        _exact_identifier_key(item["business_id"]): item["business_id"] for item in objects
    }
    links: list[dict[str, Any]] = []
    for index, raw_link in enumerate(raw_links):
        if not isinstance(raw_link, Mapping):
            raise TopologyFusionError(f"CV link {index} must be an object")
        source_id = _required_identifier(raw_link.get("source"), f"CV link {index}.source")
        target_id = _required_identifier(raw_link.get("target"), f"CV link {index}.target")
        source_id = canonical_ids.get(_exact_identifier_key(source_id), source_id)
        target_id = canonical_ids.get(_exact_identifier_key(target_id), target_id)
        if source_id not in canonical_ids.values() or target_id not in canonical_ids.values():
            raise TopologyFusionError(f"CV link {source_id}->{target_id} has a dangling endpoint")
        links.append(
            {
                "source": source_id,
                "target": target_id,
                "type": _text(raw_link.get("type"), "topology_link", 100),
                "confidence": _confidence(raw_link.get("confidence"), default=0.5),
                "attributes": _safe_attributes(raw_link.get("attributes", {})),
            }
        )
    return objects, links, canvas_id


def _normalize_model_payload(
    payload: Mapping[str, Any],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    if not isinstance(payload, Mapping):
        raise TopologyFusionError("model JSON root must be an object")
    topology = payload.get("topology", payload)
    if not isinstance(topology, Mapping):
        raise TopologyFusionError("model JSON topology must be an object")

    nodes: dict[str, dict[str, Any]] = {}
    node_ids_by_key: dict[str, str] = {}
    links: list[dict[str, Any]] = []

    def add_node(raw_node: Mapping[str, Any], *, layer: str | None = None) -> str:
        node_id = _required_identifier(
            raw_node.get("id", raw_node.get("business_id")), "model node"
        )
        key = _compact_identifier_key(node_id)
        canonical_model_id = node_ids_by_key.get(key, node_id)
        node_ids_by_key.setdefault(key, canonical_model_id)
        attributes = _safe_attributes(raw_node.get("attributes", {}))
        attributes.update(
            {
                str(name): _safe_json(value)
                for name, value in raw_node.items()
                if name
                not in {
                    "id",
                    "business_id",
                    "connections",
                    "attributes",
                    "bbox",
                    "canvas_id",
                }
                and str(name).strip().lower() not in _RESERVED_ATTRIBUTE_KEYS
            }
        )
        if layer:
            attributes["layer"] = layer
        nodes.setdefault(canonical_model_id, {}).update(attributes)
        return canonical_model_id

    raw_nodes = topology.get("nodes", topology.get("objects", []))
    if raw_nodes is not None and not isinstance(raw_nodes, list):
        raise TopologyFusionError("model topology.nodes or objects must be a list")
    connected_nodes: list[tuple[str, Mapping[str, Any]]] = []
    for raw_node in raw_nodes or []:
        if not isinstance(raw_node, Mapping):
            raise TopologyFusionError("model topology.nodes entries must be objects")
        connected_nodes.append((add_node(raw_node), raw_node))

    raw_layers = topology.get("layers", [])
    if raw_layers is not None and not isinstance(raw_layers, list):
        raise TopologyFusionError("model topology.layers must be a list")
    for layer in raw_layers or []:
        if not isinstance(layer, Mapping):
            raise TopologyFusionError("model topology.layers entries must be objects")
        layer_name = _text(layer.get("name"), "", 200)
        devices = layer.get("devices", [])
        if not isinstance(devices, list):
            raise TopologyFusionError("model layer devices must be a list")
        for device in devices:
            if not isinstance(device, Mapping):
                raise TopologyFusionError("model layer devices entries must be objects")
            model_id = add_node(device, layer=layer_name or None)
            connected_nodes.append((model_id, device))

    for model_id, device in connected_nodes:
        connections = device.get("connections", {})
        if connections is None:
            continue
        if isinstance(connections, Mapping):
            connection_groups = list(connections.items())
        elif isinstance(connections, list):
            connection_groups = [("related", connections)]
        else:
            raise TopologyFusionError(
                f"model node {model_id} connections must be an object or list"
            )
        for direction, raw_targets in connection_groups:
            targets = raw_targets if isinstance(raw_targets, list) else [raw_targets]
            for raw_target in targets:
                connection_attributes: dict[str, Any] = {
                    "connection_direction": str(direction)
                }
                explicit_source: str | None = None
                if isinstance(raw_target, Mapping):
                    target_value = raw_target.get(
                        "target", raw_target.get("id", raw_target.get("nodeId"))
                    )
                    if raw_target.get("source") is not None:
                        explicit_source = _required_identifier(
                            raw_target.get("source"),
                            f"model node {model_id} connection source",
                        )
                    connection_attributes.update(
                        {
                            str(name): _safe_json(value)
                            for name, value in raw_target.items()
                            if name not in {"source", "target", "id", "nodeId"}
                            and str(name).strip().lower() not in _RESERVED_ATTRIBUTE_KEYS
                        }
                    )
                else:
                    target_value = raw_target
                target_id = _required_identifier(
                    target_value, f"model node {model_id} connection"
                )
                target_key = _compact_identifier_key(target_id)
                canonical_target = node_ids_by_key.get(target_key, target_id)
                if target_key not in node_ids_by_key:
                    node_ids_by_key[target_key] = canonical_target
                    nodes[canonical_target] = {"implicit": True}
                if explicit_source is not None:
                    source_key = _compact_identifier_key(explicit_source)
                    source = node_ids_by_key.get(source_key, explicit_source)
                    if source_key not in node_ids_by_key:
                        node_ids_by_key[source_key] = source
                        nodes[source] = {"implicit": True}
                    target = canonical_target
                elif str(direction).strip().lower() == "up":
                    source, target = canonical_target, model_id
                else:
                    source, target = model_id, canonical_target
                links.append(
                    {
                        "source": source,
                        "target": target,
                        "type": "topology_link",
                        "confidence": None,
                        "attributes": connection_attributes,
                    }
                )

    raw_edges = topology.get("edges", [])
    raw_topology_links = topology.get("links", [])
    if raw_edges is not None and not isinstance(raw_edges, list):
        raise TopologyFusionError("model topology.edges must be a list")
    if raw_topology_links is not None and not isinstance(raw_topology_links, list):
        raise TopologyFusionError("model topology.links must be a list")
    all_raw_edges = list(raw_edges or []) + list(raw_topology_links or [])
    for index, raw_edge in enumerate(all_raw_edges):
        if not isinstance(raw_edge, Mapping):
            raise TopologyFusionError("model topology.edges entries must be objects")
        source = _required_identifier(raw_edge.get("source"), f"model edge {index}.source")
        target = _required_identifier(raw_edge.get("target"), f"model edge {index}.target")
        source = node_ids_by_key.get(_compact_identifier_key(source), source)
        target = node_ids_by_key.get(_compact_identifier_key(target), target)
        for endpoint in (source, target):
            key = _compact_identifier_key(endpoint)
            if key not in node_ids_by_key:
                node_ids_by_key[key] = endpoint
                nodes[endpoint] = {"implicit": True}
        raw_type = _text(raw_edge.get("type"), "topology_link", 100)
        attributes = _safe_attributes(raw_edge.get("attributes", {}))
        attributes.update(
            {
                str(name): _safe_json(value)
                for name, value in raw_edge.items()
                if name not in {"source", "target", "type", "confidence", "attributes"}
                and str(name).strip().lower() not in _RESERVED_ATTRIBUTE_KEYS
            }
        )
        relation_type = raw_type
        if raw_type.lower() in _LINE_STYLES:
            relation_type = "topology_link"
            attributes["line_style"] = raw_type
        links.append(
            {
                "source": source,
                "target": target,
                "type": relation_type,
                "confidence": _optional_confidence(raw_edge.get("confidence")),
                "attributes": attributes,
            }
        )

    raw_alarms = topology.get("alarms", [])
    if raw_alarms is not None and not isinstance(raw_alarms, list):
        raise TopologyFusionError("model topology.alarms must be a list")
    for raw_alarm in raw_alarms or []:
        if not isinstance(raw_alarm, Mapping):
            continue
        alarm_node_id = raw_alarm.get(
            "nodeId", raw_alarm.get("business_id", raw_alarm.get("id"))
        )
        if alarm_node_id is None:
            continue
        alarm_key = _compact_identifier_key(str(alarm_node_id))
        canonical_alarm_node = node_ids_by_key.get(alarm_key)
        if canonical_alarm_node is None:
            continue
        alarm_attributes = {
            str(name): _safe_json(value)
            for name, value in raw_alarm.items()
            if name not in {"nodeId", "business_id", "id"}
            and str(name).strip().lower() not in _RESERVED_ATTRIBUTE_KEYS
        }
        nodes[canonical_alarm_node].setdefault("alarms", []).append(alarm_attributes)

    if len(nodes) > TopologyVisionContract.MAX_OBJECTS:
        raise TopologyFusionError("model JSON contains too many nodes")
    if len(links) > TopologyVisionContract.MAX_RELATIONS:
        raise TopologyFusionError("model JSON contains too many links")

    metadata = {
        str(name): _safe_json(value)
        for name, value in topology.items()
        if name not in {"nodes", "objects", "edges", "links", "layers"}
        and str(name).strip().lower() not in _RESERVED_ATTRIBUTE_KEYS
    }
    return nodes, _deduplicate_model_links(links), metadata


def _fuse_object(
    cv_object: dict[str, Any],
    model_match: tuple[str, dict[str, Any]] | None,
    default_canvas_id: str,
) -> dict[str, Any]:
    result = copy.deepcopy(cv_object)
    result["canvas_id"] = _text(result.get("canvas_id"), default_canvas_id, 200)
    attributes = _safe_attributes(result.get("attributes", {}))
    attributes["cv_confidence"] = result["confidence"]
    attributes["geometry_status"] = "cv_grounded"
    if model_match is None:
        attributes["fusion_status"] = "cv_only"
        attributes["evidence_sources"] = ["local_cv"]
    else:
        model_id, model_node = model_match
        model_semantics = _safe_attributes(model_node)
        model_type = _text(model_semantics.get("type"), "", 100)
        if model_type and str(result.get("type", "")).lower() in _GENERIC_OBJECT_TYPES:
            result["type"] = model_type
        model_label = _text(model_semantics.get("label"), "", 500)
        if model_label and not str(result.get("label", "")).strip():
            result["label"] = model_label
        attributes["fusion_status"] = "confirmed"
        attributes["evidence_sources"] = ["local_cv", "multimodal_model"]
        attributes["model_business_id"] = model_id
        attributes["model_semantics"] = model_semantics
    result["attributes"] = attributes
    return result


def _fuse_links(
    cv_links: list[dict[str, Any]],
    model_links: list[dict[str, Any]],
    *,
    model_layers_by_cv_id: Mapping[str, str],
) -> list[dict[str, Any]]:
    cv_by_pair: dict[tuple[str, str], dict[str, Any]] = {}
    for link in cv_links:
        key = _undirected_pair(link["source"], link["target"])
        previous = cv_by_pair.get(key)
        if previous is None or float(link["confidence"]) > float(previous["confidence"]):
            cv_by_pair[key] = link

    model_by_pair: dict[tuple[str, str], dict[str, Any]] = {}
    for link in model_links:
        key = _undirected_pair(link["source"], link["target"])
        model_by_pair.setdefault(key, link)

    fused: list[dict[str, Any]] = []
    for key in sorted(set(cv_by_pair) | set(model_by_pair)):
        cv_link = cv_by_pair.get(key)
        model_link = model_by_pair.get(key)
        if cv_link is not None and model_link is not None:
            source = str(model_link["source"])
            target = str(model_link["target"])
            relation_type = _preferred_relation_type(cv_link, model_link)
            confidence = float(cv_link["confidence"])
            attributes = _safe_attributes(cv_link.get("attributes", {}))
            attributes.update(
                {
                    "fusion_status": "confirmed",
                    "evidence_sources": ["local_cv", "multimodal_model"],
                    "cv_confidence": confidence,
                    "model_attributes": _safe_attributes(
                        model_link.get("attributes", {})
                    ),
                }
            )
            if model_link.get("confidence") is not None:
                attributes["model_confidence"] = model_link["confidence"]
        elif cv_link is not None:
            source = str(cv_link["source"])
            target = str(cv_link["target"])
            relation_type = str(cv_link["type"])
            confidence = float(cv_link["confidence"])
            attributes = _safe_attributes(cv_link.get("attributes", {}))
            source_layer = model_layers_by_cv_id.get(source)
            target_layer = model_layers_by_cv_id.get(target)
            same_model_layer = bool(source_layer and source_layer == target_layer)
            attributes.update(
                {
                    "fusion_status": "conflict" if same_model_layer else "cv_only",
                    "evidence_sources": (
                        ["local_cv", "multimodal_model"]
                        if same_model_layer
                        else ["local_cv"]
                    ),
                    "cv_confidence": confidence,
                }
            )
            if same_model_layer:
                attributes.update(
                    {
                        "conflict_reason": (
                            "model_places_both_endpoints_in_same_layer_without_peer_link"
                        ),
                        "model_layer": source_layer,
                    }
                )
        else:
            assert model_link is not None
            source = str(model_link["source"])
            target = str(model_link["target"])
            relation_type = str(model_link["type"])
            confidence = (
                float(model_link["confidence"])
                if model_link.get("confidence") is not None
                else DEFAULT_MODEL_CONFIDENCE
            )
            attributes = _safe_attributes(model_link.get("attributes", {}))
            attributes.update(
                {
                    "fusion_status": "model_only",
                    "evidence_sources": ["multimodal_model"],
                    "visual_evidence_status": "not_confirmed_by_local_cv",
                    "confidence_basis": (
                        "model_reported"
                        if model_link.get("confidence") is not None
                        else "uncalibrated_default"
                    ),
                }
            )
        fused.append(
            {
                "relation_id": f"fusion:{source}:{target}",
                "source": source,
                "target": target,
                "type": relation_type,
                "confidence": round(confidence, 4),
                "attributes": attributes,
            }
        )
    return fused


def _deduplicate_model_links(links: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduplicated: dict[tuple[str, str], dict[str, Any]] = {}
    for link in links:
        if link["source"] == link["target"]:
            continue
        key = _undirected_pair(link["source"], link["target"])
        existing = deduplicated.get(key)
        if existing is None:
            deduplicated[key] = copy.deepcopy(link)
            continue
        existing_attributes = existing.setdefault("attributes", {})
        existing_attributes.update(_safe_attributes(link.get("attributes", {})))
        if existing.get("confidence") is None and link.get("confidence") is not None:
            existing["confidence"] = link["confidence"]
    return list(deduplicated.values())


def _unlocated_object(model_id: str, model_node: Mapping[str, Any]) -> dict[str, Any]:
    attributes = _safe_attributes(model_node)
    node_type = _text(model_node.get("type"), "network_device", 100)
    is_virtual = bool(model_node.get("virtual")) or "bus" in node_type.casefold()
    attributes.update(
        {
            "fusion_status": "model_only",
            "evidence_sources": ["multimodal_model"],
            "geometry_status": "unlocated",
            "virtual": is_virtual,
        }
    )
    return {
        "business_id": model_id,
        "type": node_type,
        "label": _text(model_node.get("label"), model_id, 500),
        "confidence": _confidence(model_node.get("confidence"), DEFAULT_MODEL_CONFIDENCE),
        "attributes": attributes,
    }


def _infer_canvas_id(source: Mapping[str, Any]) -> str:
    coordinate_space = source.get("coordinate_space")
    if isinstance(coordinate_space, Mapping):
        frames = coordinate_space.get("frames")
        if isinstance(frames, list) and frames and isinstance(frames[0], Mapping):
            value = str(frames[0].get("canvas_id", "")).strip()
            if value:
                return value[:200]
    input_payload = source.get("input")
    if isinstance(input_payload, Mapping):
        canvases = input_payload.get("canvases")
        if isinstance(canvases, list) and canvases and isinstance(canvases[0], Mapping):
            value = str(canvases[0].get("canvas_id", "")).strip()
            if value:
                return value[:200]
    return "uploaded_topology"


def _preferred_relation_type(
    cv_link: Mapping[str, Any], model_link: Mapping[str, Any]
) -> str:
    cv_type = str(cv_link.get("type", "topology_link"))
    model_type = str(model_link.get("type", "topology_link"))
    if cv_type in {"", "relation", "topology_link"} and model_type:
        return model_type
    return cv_type


def _status_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        attributes = item.get("attributes", {})
        status = str(attributes.get("fusion_status", "unknown"))
        counts[status] = counts.get(status, 0) + 1
    return counts


def _bbox(value: Any, context: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise TopologyFusionError(f"{context} requires bbox=[x,y,width,height]")
    result: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise TopologyFusionError(f"{context} bbox must contain finite numbers")
        numeric = float(item)
        if not math.isfinite(numeric):
            raise TopologyFusionError(f"{context} bbox must contain finite numbers")
        result.append(numeric)
    if result[0] < 0 or result[1] < 0 or result[2] <= 0 or result[3] <= 0:
        raise TopologyFusionError(f"{context} bbox is invalid")
    return result


def _confidence(value: Any, default: float) -> float:
    if value is None:
        return round(default, 4)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return round(default, 4)
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0 or numeric > 1:
        return round(default, 4)
    return round(numeric, 4)


def _optional_confidence(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0 or numeric > 1:
        return None
    return round(numeric, 4)


def _required_identifier(value: Any, context: str) -> str:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise TopologyFusionError(f"{context} requires an identifier")
    result = unicodedata.normalize("NFKC", str(value)).strip()
    if not result or len(result) > 200 or any(ord(char) < 32 for char in result):
        raise TopologyFusionError(f"{context} identifier is invalid")
    return result


def _text(value: Any, default: str, maximum: int) -> str:
    if value is None:
        return default
    result = str(value).strip()
    return result[:maximum] if result else default


def _exact_identifier_key(value: str) -> str:
    return unicodedata.normalize("NFKC", str(value)).strip().casefold()


def _compact_identifier_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value)).upper()
    compact = re.sub(r"[^0-9A-Z\u4e00-\u9fff]+", "", normalized)
    return compact or normalized.strip()


def _identifier_sort_key(value: str) -> tuple[str, str]:
    return _compact_identifier_key(value), _exact_identifier_key(value)


def _undirected_pair(source: str, target: str) -> tuple[str, str]:
    ordered = sorted((str(source), str(target)), key=_identifier_sort_key)
    return ordered[0], ordered[1]


def _safe_attributes(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): _safe_json(item)
        for key, item in value.items()
        if str(key).strip().lower() not in _RESERVED_ATTRIBUTE_KEYS
    }


def _safe_json(value: Any, depth: int = 0) -> Any:
    if depth >= 7:
        return str(value)[:1000]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {
            str(key): _safe_json(item, depth + 1)
            for key, item in value.items()
            if str(key).strip().lower() not in _RESERVED_ATTRIBUTE_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_safe_json(item, depth + 1) for item in value]
    return str(value)


__all__ = [
    "DEFAULT_MODEL_CONFIDENCE",
    "FUSION_SCHEMA_VERSION",
    "TopologyFusionError",
    "fuse_topology_payloads",
]
